"""Look at an emulated display in a browser, and use it: clicks and drags become touches.

    viewer = Viewer(display)              # serves http://127.0.0.1:<viewer.port>/
    ...
    viewer.close()

Or on its own, with an MCU on the other end of the display's UART:

    python -m nextion_tools.emu.viewer --hmi gui.HMI --tcp 4000 --http 8080
    # then: qemu ... -serial tcp:127.0.0.1:4000, and open http://127.0.0.1:8080/

The page shows the screen (redrawn whenever the display's state changes), the current page,
the last instructions, rejected instructions, and, with the flow model on, how full the input
buffer is and how often it overflowed. A press on the screen is a finger going down at that
point; moving with the button held drags it (sliders follow, gestures are decoded by the
HMI's own code); letting go lifts it. The screen is drawn turned by the display's `rotation`,
and clicks are mapped back, so touches land where they appear to.

Standard library only, apart from the renderer's numpy and Pillow (nextion-tools[images]).
Serves on 127.0.0.1 unless told otherwise: anyone who can reach it can touch the panel.
"""

import argparse
import io
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

log = logging.getLogger("nextion_tools.emu.viewer")

RECENT_COMMANDS = 25


class Viewer:
    def __init__(self, display, host: str = "127.0.0.1", port: int = 0):
        self.display = display
        self._frame_lock = threading.Lock()
        self._frame_version = None
        self._frame_png = b""
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("http: " + fmt, *args)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/":
                    self._send(200, "text/html; charset=utf-8", PAGE.encode())
                elif path == "/frame.png":
                    try:
                        self._send(200, "image/png", viewer.frame())
                    except Exception as e:  # e.g. no page shown yet
                        self._send(503, "text/plain", str(e).encode())
                elif path == "/state":
                    self._json(200, viewer.state())
                else:
                    self._send(404, "text/plain", b"not found")

            def do_POST(self):
                if self.path.split("?", 1)[0] != "/touch":
                    self._send(404, "text/plain", b"not found")
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                    viewer.touch(body["kind"], int(body.get("x", 0)), int(body.get("y", 0)))
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(409, {"ok": False, "error": str(e)})

            def _json(self, code, obj):
                self._send(code, "application/json", json.dumps(obj).encode())

            def _send(self, code, kind, body):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.host, self.port = self.server.server_address[:2]
        threading.Thread(target=self.server.serve_forever, name="nextion-viewer", daemon=True).start()

    @property
    def url(self) -> str:
        return "http://%s:%d/" % (self.host, self.port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def frame(self) -> bytes:
        """The screen as a PNG, rendered again only when the display has changed."""
        from PIL import Image
        scene = self.display.scene()
        with self._frame_lock:
            if scene["version"] != self._frame_version:
                out = io.BytesIO()
                Image.fromarray(self.display.snapshot(scene)).save(out, "PNG")
                self._frame_png, self._frame_version = out.getvalue(), scene["version"]
            return self._frame_png

    def state(self) -> dict:
        d = self.display
        with d._cond:
            flow = d.flow
            page = d.screen.page(d.page) if d.screen is not None and d.page else None
            return {
                "version": d.version,
                "page": d.page,
                "page_id": page.id if page is not None else None,
                "rotation": d.rotation,
                "connected": d.connected,
                "bkcmd": d.bkcmd,
                "commands": d.commands[-RECENT_COMMANDS:],
                "command_count": len(d.commands),
                "errors": [[text, code] for text, code in d.errors[-10:]],
                "error_count": len(d.errors),
                "flow_model": d.flow_model,
                "buffered": flow.buffered,
                "capacity": flow.capacity,
                "max_buffered": flow.max_buffered,
                "overflows": flow.overflows,
                "lost": len(flow.lost),
                "touching": d._touch is not None,
                "asleep": d._asleep(),
            }

    def touch(self, kind: str, x: int, y: int) -> None:
        """A touch at (x, y) on the picture as shown, i.e. after rotation."""
        from ..render import unrotate_point
        d = self.display
        with d._cond:
            size = d._current_page().size
        px, py = unrotate_point(x, y, d.rotation, size)
        if kind == "down":
            if d._touch is not None:
                d.touch_up()  # a lift the browser never reported
            d.touch_down(px, py)
        elif kind == "move":
            d.touch_move(px, py)
        elif kind == "up":
            d.touch_up()
        else:
            raise ValueError("touch kind must be down, move or up, not %r" % kind)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nextion Viewer</title>
<style>
  :root { --bg:#f4f4f2; --fg:#1d1d1b; --muted:#6b6b66; --panel:#ffffff; --line:#d9d9d4; --accent:#0b6bcb; --bad:#b3261e; }
  @media (prefers-color-scheme: dark) { :root { --bg:#151514; --fg:#ececea; --muted:#9a9a94; --panel:#1f1f1d; --line:#33332f; --accent:#6aa9f0; --bad:#f2938c; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui, sans-serif; }
  main { display:flex; flex-wrap:wrap; gap:16px; padding:16px; align-items:flex-start; }
  #screen { background:#000; border-radius:10px; padding:10px; box-shadow:0 2px 12px rgba(0,0,0,.25); }
  #frame { display:block; image-rendering:pixelated; touch-action:none; cursor:crosshair; user-select:none; -webkit-user-drag:none; max-width:calc(100vw - 52px); }
  aside { flex:1 1 320px; min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  h1 { font-size:15px; margin:0 0 8px; }
  dl { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; margin:0 0 12px; }
  dt { color:var(--muted); } dd { margin:0; font-variant-numeric:tabular-nums; }
  .bad { color:var(--bad); }
  meter { width:100%; }
  pre { margin:0; max-height:40vh; overflow:auto; font:12px/1.35 ui-monospace, monospace; white-space:pre-wrap; word-break:break-all; }
  h2 { font-size:13px; margin:12px 0 4px; color:var(--muted); font-weight:600; }
</style></head>
<body><main>
  <div id="screen"><img id="frame" alt="the display" draggable="false"></div>
  <aside>
    <h1>Nextion display</h1>
    <dl>
      <dt>Page</dt><dd id="page">–</dd>
      <dt>MCU</dt><dd id="connected">–</dd>
      <dt>bkcmd</dt><dd id="bkcmd">–</dd>
      <dt>Instructions</dt><dd id="count">0</dd>
      <dt>Rejected (1a)</dt><dd id="errors">0</dd>
      <dt>Input buffer</dt><dd id="buffer">flow model off</dd>
      <dt>Overflows</dt><dd id="overflows">0</dd>
    </dl>
    <meter id="level" min="0" max="1" value="0" hidden></meter>
    <h2>Last instructions</h2><pre id="commands"></pre>
    <h2>Last rejected</h2><pre id="rejected" class="bad"></pre>
  </aside>
</main>
<script>
const frame = document.getElementById("frame");
let shown = -1, down = false, lastMove = 0;
function post(kind, e) {
  const r = frame.getBoundingClientRect();
  const x = Math.floor((e.clientX - r.left) * frame.naturalWidth / r.width);
  const y = Math.floor((e.clientY - r.top) * frame.naturalHeight / r.height);
  return fetch("/touch", {method: "POST", headers: {"Content-Type": "application/json"},
                          body: JSON.stringify({kind, x, y})});
}
frame.addEventListener("pointerdown", e => { down = true; frame.setPointerCapture(e.pointerId); post("down", e); e.preventDefault(); });
frame.addEventListener("pointermove", e => {
  if (!down) return;
  const now = performance.now();
  if (now - lastMove < 30) return;
  lastMove = now; post("move", e);
});
function lift(e) { if (!down) return; down = false; post("up", e); }
frame.addEventListener("pointerup", lift);
frame.addEventListener("pointercancel", lift);
async function poll() {
  try {
    const s = await (await fetch("/state")).json();
    document.getElementById("page").textContent = s.page === null ? "–" : s.page + " (id " + s.page_id + ")" + (s.asleep ? ", asleep" : "");
    document.getElementById("connected").textContent = s.connected ? "connected" : "not connected";
    document.getElementById("bkcmd").textContent = s.bkcmd;
    document.getElementById("count").textContent = s.command_count;
    const errs = document.getElementById("errors");
    errs.textContent = s.error_count; errs.className = s.error_count ? "bad" : "";
    const level = document.getElementById("level");
    if (s.flow_model) {
      document.getElementById("buffer").textContent = s.buffered + " / " + s.capacity + " B (max " + s.max_buffered + ")";
      level.hidden = false; level.value = s.buffered / s.capacity;
    } else { document.getElementById("buffer").textContent = "flow model off"; level.hidden = true; }
    const ov = document.getElementById("overflows");
    ov.textContent = s.overflows + (s.lost ? " (" + s.lost + " lost)" : ""); ov.className = s.overflows ? "bad" : "";
    document.getElementById("commands").textContent = s.commands.slice().reverse().join("\n");
    document.getElementById("rejected").textContent = s.errors.slice().reverse().map(e => e[0]).join("\n");
    if (s.version !== shown && s.page !== null) { shown = s.version; frame.src = "/frame.png?v=" + s.version; }
  } catch (err) { document.getElementById("connected").textContent = "viewer unreachable"; }
  setTimeout(poll, 200);
}
poll();
</script></body></html>
"""


def main(argv=None) -> None:
    from ..hmi import Project
    from .display import Display
    from .transport import Pty, Serial, TcpServer

    parser = argparse.ArgumentParser(prog="python -m nextion_tools.emu.viewer", description=__doc__.split("\n")[0])
    parser.add_argument("--hmi", required=True, help="the project's .HMI")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--tcp", type=int, metavar="PORT", help="the display's UART on 127.0.0.1:PORT (0 picks one)")
    where.add_argument("--pty", action="store_true", help="the display's UART on a pseudo-terminal")
    where.add_argument("--serial", metavar="DEVICE", help="the display's UART on a serial port (needs pyserial)")
    parser.add_argument("--http", type=int, default=8080, metavar="PORT", help="the viewer's port (default 8080)")
    parser.add_argument("--bind", default="127.0.0.1", help="the viewer's address (default 127.0.0.1)")
    parser.add_argument("--rotation", type=int, default=0, choices=(0, 90, 180, 270),
                        help="turn the screen this many degrees clockwise")
    parser.add_argument("--flow", action="store_true", help="model the input buffer and stalls")
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.tcp is not None:
        transport = TcpServer(port=args.tcp)
        print("display UART: tcp %s:%d" % (transport.host, transport.port), flush=True)
    elif args.pty:
        transport = Pty()
        print("display UART: %s" % transport.path, flush=True)
    else:
        transport = Serial(args.serial, args.baud)
    display = Display(transport, hmi=Project.load(args.hmi), flow_model=args.flow, baud=args.baud,
                      rotation=args.rotation)
    viewer = Viewer(display, args.bind, args.http)
    print("viewer: %s" % viewer.url, flush=True)
    try:
        while not display.connected:
            time.sleep(0.1)
        display.boot()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        viewer.close()
        display.close()


if __name__ == "__main__":
    main()
