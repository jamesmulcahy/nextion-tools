// A live protocol-8 sender for test_terminal_live.py: the real StreamEngine (nspanel-terminal's, or
// with -DNT_FIRMWARE the NSPanel firmware's copy), driven against a display over TCP in real time,
// as NextionTerminal::_session_once() / _stream_session() drive it: the command-mode setup, then
// rectangles within the credit window, PINGs while idle, touch from the reports, and EXIT.
//
//   live_sender PORT SESSION IDLE_MS OUT.fb
//
// Draws three rectangles on a 320x480 receiver, idles IDLE_MS (a test touches the display then),
// draws a fourth, and exits. Writes the framebuffer it drew (RGB565, high byte first) to OUT.fb.
// Prints one line per event: "comok <reply>", "ready", "done <n>", "touch <mask> <x> <y>",
// "stopped" (BYE) or "failed <reason>"; exits 0 only after BYE.

#include <arpa/inet.h>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <string>
#include <sys/socket.h>
#include <unistd.h>
#include <vector>

#ifdef NT_FIRMWARE
#include "TerminalStreamEngine.hpp"
namespace nt = nspanel_terminal;
#else
#include "stream_engine.h"
namespace nt = esphome::nspanel_terminal;
#endif
namespace protocol = nt::protocol;

static const unsigned W = 320, H = 480;
static int fd = -1;

static uint32_t now_ms() {
  using namespace std::chrono;
  return uint32_t(duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count());
}

static void send_all(const uint8_t *p, size_t n) {
  while (n) {
    ssize_t k = ::send(fd, p, n, 0);
    if (k <= 0) {
      std::printf("failed send\n");
      std::exit(1);
    }
    p += k;
    n -= size_t(k);
  }
}

static void command(const char *text) {
  std::string s = std::string(text) + "\xff\xff\xff";
  send_all(reinterpret_cast<const uint8_t *>(s.data()), s.size());
}

// Read what is there within `timeout_ms`.
static std::vector<uint8_t> read_some(int timeout_ms) {
  std::vector<uint8_t> out;
  pollfd p{fd, POLLIN, 0};
  if (::poll(&p, 1, timeout_ms) > 0) {
    uint8_t buf[4096];
    ssize_t k = ::recv(fd, buf, sizeof buf, 0);
    if (k > 0) out.assign(buf, buf + k);
  }
  return out;
}

int main(int argc, char **argv) {
  if (argc != 5) {
    std::fprintf(stderr, "usage: %s PORT SESSION IDLE_MS OUT.fb\n", argv[0]);
    return 2;
  }
  setvbuf(stdout, nullptr, _IOLBF, 0);
  const unsigned session = unsigned(std::atoi(argv[2])) & 0xFFFF;
  const uint32_t idle_ms = uint32_t(std::atoi(argv[3]));
  fd = ::socket(AF_INET, SOCK_STREAM, 0);
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(uint16_t(std::atoi(argv[1])));
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  if (::connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof addr) != 0) {
    std::printf("failed connect\n");
    return 1;
  }
  int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);

  // The setup NextionTerminal::_session_once() sends.
  command("");
  command("connect");
  // Skip error replies (the bare terminator's 0x1A) until comok, as the firmware does.
  std::string reply;
  for (uint32_t start = now_ms(); now_ms() - start < 2000 && reply.find("comok") == std::string::npos;) {
    auto got = read_some(50);
    reply.append(got.begin(), got.end());
  }
  if (reply.find("comok") != std::string::npos) reply = reply.substr(reply.find("comok"));
  reply = reply.substr(0, reply.find("\xff\xff\xff"));
  std::printf("comok %s\n", reply.c_str());
  if (reply.find("NX4832F035_011C,94,61744,") == std::string::npos) {
    std::printf("failed identity\n");
    return 1;
  }
  char sys2[24];
  std::snprintf(sys2, sizeof sys2, "sys2=%u", session);
  for (const char *c : {"bkcmd=0", "sys0=4000000", "sys1=1", (const char *)sys2, "touch_j"}) command(c);

  // Four rectangles: a gradient band, noise, a solid block, and after the idle period, a stripe.
  std::vector<uint16_t> fb(W * H, 0);
  struct Job {
    unsigned x, y, w, h;
    std::vector<uint16_t> pixels;
    bool final;
  };
  std::vector<Job> jobs;
  auto add = [&](unsigned x, unsigned y, unsigned w, unsigned h, bool final, auto color) {
    Job j{x, y, w, h, std::vector<uint16_t>(w * h), final};
    for (unsigned r = 0; r < h; ++r)
      for (unsigned c = 0; c < w; ++c) j.pixels[r * w + c] = fb[(y + r) * W + x + c] = uint16_t(color(x + c, y + r));
    jobs.push_back(std::move(j));
  };
  add(0, 0, 320, 40, false, [](unsigned x, unsigned y) { return (x / 10) << 11 | (y & 63) << 5 | (31 - x / 11); });
  add(0, 40, 320, 100, false, [](unsigned x, unsigned y) { uint32_t v = x * 2654435761u ^ y * 40503u; return v ^ (v >> 16); });
  add(100, 300, 120, 60, true, [](unsigned, unsigned) { return 0x07E0; });
  add(0, 470, 320, 10, true, [](unsigned x, unsigned) { return (x & 8) ? 0xFFFF : 0x001F; });

  nt::StreamEngine engine;
  engine.begin(session, now_ms(), W, H);
  size_t next = 0;
  bool job_active = false, exit_sent = false, was_down = false;
  uint32_t idle_from = 0;
  unsigned completed_before = 0;
  for (;;) {
    for (uint8_t b : read_some(1)) {
      protocol::Reply r;
      if (!engine.feed(b, now_ms(), r)) continue;
      if ((r.status == protocol::READY || r.status == protocol::READY_BRIGHTNESS) && engine.stage() == nt::StreamEngine::Stage::IDLE)
        std::printf("ready\n");
      if (r.freshness == 1) {
        bool down = r.mask & 1;
        if (down || was_down) std::printf("touch %u %d %d\n", r.mask, int16_t(r.x), int16_t(r.y));
        was_down = down;
      }
    }
    if (engine.failed()) {
      std::printf("failed %s\n", engine.error());
      return 1;
    }
    if (engine.stage() == nt::StreamEngine::Stage::STOPPED) {
      std::printf("stopped\n");
      break;
    }
    if (job_active) {
      const Job &j = jobs[next - 1];
      bool done = j.final ? engine.completed() != completed_before : engine.stage() == nt::StreamEngine::Stage::BETWEEN;
      if (done) {
        job_active = false;
        if (j.final) std::printf("done %u\n", engine.completed());
        if (next == 3) idle_from = now_ms();
      }
    }
    const bool idle_over = next != 3 || now_ms() - idle_from >= idle_ms;
    if (!job_active && next < jobs.size() && idle_over && engine.can_submit()) {
      const Job &j = jobs[next++];
      completed_before = engine.completed();
      if (!engine.submit(j.x, j.y, j.w, j.h, reinterpret_cast<const uint8_t *>(j.pixels.data()), now_ms(), j.final)) {
        std::printf("failed submit\n");
        return 1;
      }
      job_active = true;
    }
    if (next == jobs.size() && !job_active && !exit_sent && engine.idle()) {
      engine.request_exit();
      exit_sent = true;
    }
    for (;;) {
      engine.prepare(now_ms());
      if (engine.failed()) break;
      size_t allowed = engine.allowance();
      if (!allowed) break;
      send_all(engine.data(), allowed);
      engine.sent(allowed, now_ms());
    }
  }
  FILE *f = std::fopen(argv[4], "wb");
  for (uint16_t v : fb) {
    uint8_t be[2] = {uint8_t(v >> 8), uint8_t(v)};
    std::fwrite(be, 1, 2, f);
  }
  std::fclose(f);
  ::close(fd);
  return 0;
}
