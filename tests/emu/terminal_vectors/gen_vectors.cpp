// Golden vectors for nextion_tools.emu.terminal: byte streams from the real protocol-8 sender, and
// the framebuffer the real display-side decoder draws from them. Built and run by generate.sh.
//
// The sender is nspanel-terminal's StreamEngine (components/nspanel_terminal/stream_engine.h), or
// with -DNT_FIRMWARE the NSPanel firmware's copy of it (lib/NextionTerminal/TerminalStreamEngine.hpp,
// which differs only in its namespace and include names). The receiver is nspanel-terminal's
// display firmware decoder (firmware/src/stream_protocol.h), compiled for the host with
// STREAM_WINDOW/STREAM_BYTE writing into a framebuffer, and driven as firmware/src/terminal_receiver.c
// drives it: a report on every event and after every 256 consumed bytes, each fed straight back
// into the sender, so the stream respects the sender's 512-byte credit window. Both are MIT
// licensed, Copyright (c) 2026 NSPanel public contributors; neither is copied here, only included.
//
// The panel geometry is a compile-time choice of the decoder (panel_profile.h), so this is built
// once per profile: -DPROFILE_US (320x480) or not (EU, 454x320). Each binary writes its profile's
// cases into OUT_DIR: <case>.stream (the forward bytes, as the receiver saw them), <case>.fb (the
// final framebuffer, RGB565 high byte first, row-major), and one JSON object per line on stdout
// with the case's parameters and every report the receiver sent.
//
//   gen_vectors OUT_DIR

#ifdef PROFILE_US
#define NSPANEL_PANEL_WIDTH 320
#define NSPANEL_PANEL_HEIGHT 480
#define PROFILE "us"
#else
#define PROFILE "eu"
#endif
#include "panel_profile.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <string>
#include <vector>

#ifdef NT_FIRMWARE
#include "TerminalStreamEngine.hpp"
namespace nt = nspanel_terminal;
#define SENDER "firmware lib/NextionTerminal"
#else
#include "stream_engine.h"
namespace nt = esphome::nspanel_terminal;
#define SENDER "nspanel-terminal components/nspanel_terminal"
#endif
namespace protocol = nt::protocol;

// ---- the display-side decoder, writing into a framebuffer ------------------------------------

static const unsigned W = NSPANEL_PANEL_WIDTH, H = NSPANEL_PANEL_HEIGHT;
static std::vector<uint8_t> fb(2 * W * H);
static unsigned win_x, win_y, win_w, win_h, win_at, win_phase;
static uint8_t win_hi;

static void lcd_window(unsigned x, unsigned y, unsigned w, unsigned h) {
  win_x = x, win_y = y, win_w = w, win_h = h, win_at = 0, win_phase = 0;
}
static void lcd_byte(uint8_t b) {
  if (!win_phase) {
    win_hi = b;
    win_phase = 1;
    return;
  }
  win_phase = 0;
  unsigned at = win_at++ % (win_w * win_h);  // the LCD wraps within its window
  size_t i = 2 * (size_t(win_y + at / win_w) * W + win_x + at % win_w);
  fb[i] = win_hi;
  fb[i + 1] = b;
}
#define STREAM_WINDOW(x, y, w, h) lcd_window(x, y, w, h)
#define STREAM_BYTE(b) lcd_byte(b)
#include "stream_protocol.h"

// ---- one closed-loop session -------------------------------------------------------------------

struct Report {
  unsigned status, consumed, seq, rectangles;
};

struct Rect {
  unsigned x, y, w, h;
  std::function<uint16_t(unsigned, unsigned)> color;  // of the pixel at panel x, y
  bool final;                                         // ends its burst (END x=1)
  unsigned idle_ms;                                   // idle time before it (PINGs)
};

struct Corruption {
  size_t offset;  // in the forward stream
  uint8_t xor_mask;
};

struct Case {
  std::string name;
  unsigned session;
  std::vector<Rect> rects;
  std::vector<Corruption> corrupt;
};

class Link {
 public:
  explicit Link(const Case &c) : case_(c) {
    stream_init(&rx_, c.session);
    engine_.begin(c.session, now_, W, H);
    report(STREAM_READY_BRIGHTNESS);
  }

  bool run() {
    for (const Rect &r : case_.rects) {
      for (unsigned t = 0; t < r.idle_ms; t += 20) {
        now_ += 20;
        if (!pump()) return false;
      }
      std::vector<uint16_t> pixels(r.w * r.h);  // little-endian in memory, as LVGL's buffers
      for (unsigned y = 0; y < r.h; ++y)
        for (unsigned x = 0; x < r.w; ++x) pixels[y * r.w + x] = r.color(r.x + x, r.y + y);
      if (!engine_.submit(r.x, r.y, r.w, r.h, reinterpret_cast<const uint8_t *>(pixels.data()), now_, r.final)) {
        std::fprintf(stderr, "%s: rectangle refused\n", case_.name.c_str());
        return false;
      }
      // The snapshot must outlive the engine's reads: until BETWEEN, or IDLE after DONE.
      for (int i = 0; i < 100000 && !engine_.can_submit(); ++i)
        if (!pump()) return false;
      if (!engine_.can_submit()) {
        std::fprintf(stderr, "%s: stuck in stage %d\n", case_.name.c_str(), int(engine_.stage()));
        return false;
      }
    }
    engine_.request_exit();
    for (int i = 0; i < 1000 && engine_.stage() != nt::StreamEngine::Stage::STOPPED; ++i)
      if (!pump()) return false;
    return engine_.stage() == nt::StreamEngine::Stage::STOPPED;
  }

  const std::vector<uint8_t> &wire() const { return wire_; }
  const std::vector<Report> &reports() const { return reports_; }

 private:
  // Send everything the credits allow, delivering each byte to the receiver as it goes.
  bool pump() {
    for (;;) {
      engine_.prepare(now_);
      if (engine_.failed()) {
        std::fprintf(stderr, "%s: sender failed: %s\n", case_.name.c_str(), engine_.error());
        return false;
      }
      size_t allowed = engine_.allowance();
      if (!allowed) return true;
      std::vector<uint8_t> chunk(engine_.data(), engine_.data() + allowed);
      engine_.sent(allowed, now_);
      for (uint8_t b : chunk) {
        for (const Corruption &c : case_.corrupt)
          if (c.offset == wire_.size()) b ^= c.xor_mask;
        wire_.push_back(b);
        unsigned event = stream_feed(&rx_, b);
        ++consumed_;
        if (event || consumed_ - published_ >= 256) {
          report(event ? event : STREAM_PROGRESS);
          published_ = consumed_;
        }
      }
    }
  }

  // A 32-byte reverse report as terminal_receiver.c's report() builds it, fed to the sender.
  void report(unsigned status) {
    uint8_t p[32] = {'S', 'R', 8, uint8_t(status)};
    auto put16 = [&](unsigned at, unsigned v) {
      p[at] = uint8_t(v);
      p[at + 1] = uint8_t(v >> 8);
    };
    put16(4, rx_.session);
    put16(6, rx_.seq);
    put16(8, consumed_);
    put16(10, consumed_ >> 16);
    put16(12, rx_.rectangles);
    put16(26, STREAM_WINDOW_BYTES);
    uint16_t c = 65535;
    for (unsigned i = 0; i < 30; i++) c = stream_crc(c, p[i]);
    put16(30, c);
    reports_.push_back({status, consumed_, rx_.seq, rx_.rectangles});
    protocol::Reply r;
    for (uint8_t b : p) engine_.feed(b, now_, r);
  }

  const Case &case_;
  nt::StreamEngine engine_;
  StreamPackBits rx_;
  uint32_t now_ = 1000, consumed_ = 0, published_ = 0;
  std::vector<uint8_t> wire_;
  std::vector<Report> reports_;
};

// ---- pixel patterns ----------------------------------------------------------------------------

static uint16_t rgb(unsigned r, unsigned g, unsigned b) { return uint16_t((r & 31) << 11 | (g & 63) << 5 | (b & 31)); }

static uint16_t noise(unsigned x, unsigned y) {
  uint32_t v = (x * 73856093u) ^ (y * 19349663u);
  v ^= v >> 13;
  v *= 0x5bd1e995u;
  v ^= v >> 15;
  return uint16_t(v);
}

// Runs of these lengths, alternating colours, wrapping across rows: around every PackBits limit
// (128-pixel tokens, the two-pixel repeat threshold).
static uint16_t runs(unsigned x, unsigned y, unsigned x0, unsigned y0, unsigned w) {
  static const unsigned lengths[] = {1, 2, 3, 1, 1, 2, 127, 128, 129, 130, 1, 255, 256, 257, 2, 1, 3, 384, 5};
  unsigned i = (y - y0) * w + (x - x0), run = 0;
  for (;;) {
    for (unsigned n : lengths) {
      if (i < n) return run % 2 ? rgb(run, 63 - run, run * 3) : rgb(31 - run, run * 2, 31);
      i -= n;
      ++run;
    }
  }
}

static std::vector<Case> cases() {
  std::vector<Case> out;
#ifdef PROFILE_US
  out.push_back({"us_solid_full", 0x1234, {{0, 0, 320, 480, [](unsigned, unsigned) { return rgb(31, 0, 0); }, true, 0}}, {}});
  out.push_back({"us_gradient", 0x4321,
                 {{10, 20, 64, 48, [](unsigned x, unsigned y) { return rgb(x * 31 / 73, y * 63 / 67, (x + y) & 31); }, true, 0}},
                 {}});
  out.push_back({"us_noise_bottom", 7, {{0, 440, 320, 40, noise, true, 0}}, {}});
  out.push_back({"us_runs", 0xBEEF, {{5, 100, 300, 9, [](unsigned x, unsigned y) { return runs(x, y, 5, 100, 300); }, true, 0}}, {}});
  out.push_back({"us_burst", 0x0101,
                 {{0, 0, 320, 16, [](unsigned x, unsigned y) { return rgb(x / 10, y * 4, 31 - x / 10); }, false, 0},
                  {0, 16, 320, 16, noise, false, 0},
                  {100, 200, 1, 1, [](unsigned, unsigned) { return uint16_t(0xFFFF); }, false, 0},
                  {319, 479, 1, 1, [](unsigned, unsigned) { return uint16_t(0x07E0); }, false, 0},
                  {200, 300, 120, 180, [](unsigned x, unsigned y) { return (x / 8 + y / 8) % 2 ? rgb(0, 0, 31) : rgb(31, 63, 0); }, true, 0}},
                 {}});
  out.push_back({"us_idle_pings", 0x00FF,
                 {{50, 50, 40, 40, [](unsigned, unsigned) { return rgb(0, 63, 0); }, true, 0},
                  {60, 60, 20, 20, noise, true, 250}},
                 {}});
  // Offset 10 is in the first record's header: its CRC fails, the receiver faults and the sender
  // resynchronises (SYNC) and redraws the rectangle.
  out.push_back({"us_resync", 0x2222, {{30, 30, 100, 30, noise, true, 0}}, {{10, 0x40}}});
  // Offset 100 is in the first DATA record's body, which has no checksum: whatever the flipped
  // bit does (a wrong pixel, a broken token), both decoders must agree on it.
  out.push_back({"us_body_corruption", 0x2223, {{30, 30, 100, 30, noise, true, 0}}, {{100, 0x01}}});
#else
  out.push_back({"eu_solid_full", 0x5555, {{0, 0, 454, 320, [](unsigned, unsigned) { return rgb(0, 0, 31); }, true, 0}}, {}});
  out.push_back({"eu_right_edge", 0x0E0E,
                 {{440, 0, 14, 320, [](unsigned x, unsigned y) { return rgb(x - 440, y / 5, 31 - (x - 440)); }, false, 0},
                  {0, 304, 454, 16, [](unsigned x, unsigned y) { return (x + y) % 2 ? uint16_t(0xFFFF) : uint16_t(0); }, true, 0}},
                 {}});
  out.push_back({"eu_runs", 0x3333, {{0, 10, 454, 5, [](unsigned x, unsigned y) { return runs(x, y, 0, 10, 454); }, true, 0}}, {}});
#endif
  return out;
}

int main(int argc, char **argv) {
  if (argc != 2) {
    std::fprintf(stderr, "usage: %s OUT_DIR\n", argv[0]);
    return 2;
  }
  std::string dir = argv[1];
  for (const Case &c : cases()) {
    std::fill(fb.begin(), fb.end(), 0);
    Link link(c);
    if (!link.run()) return 1;
    FILE *f = std::fopen((dir + "/" + c.name + ".stream").c_str(), "wb");
    std::fwrite(link.wire().data(), 1, link.wire().size(), f);
    std::fclose(f);
    f = std::fopen((dir + "/" + c.name + ".fb").c_str(), "wb");
    std::fwrite(fb.data(), 1, fb.size(), f);
    std::fclose(f);
    std::printf("{\"name\": \"%s\", \"profile\": \"%s\", \"width\": %u, \"height\": %u, \"session\": %u, \"sender\": \"%s\", "
                "\"stream_bytes\": %zu, \"reports\": [",
                c.name.c_str(), PROFILE, W, H, c.session, SENDER, link.wire().size());
    const char *sep = "";
    for (const Report &r : link.reports()) {
      std::printf("%s[%u, %u, %u, %u]", sep, r.status, r.consumed, r.seq, r.rectangles);
      sep = ", ";
    }
    std::printf("]}\n");
  }
  return 0;
}
