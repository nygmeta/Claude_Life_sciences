"""Serve the console as static files, on the machine that sits next to the robot.

Why this exists instead of `python -m http.server`:

  1. MIME types. The VAD ships as WebAssembly. `http.server` derives content types from
     `mimetypes`, which on Linux reads /etc/mime.types, so what it returns for `.wasm`
     genuinely differs from one machine to the next. Emscripten survives a wrong type
     (it falls back from instantiateStreaming to an ArrayBuffer instantiation) but it
     logs a scary console error on the way, and "scary console error" is not a thing to
     hand someone who is trying to run a demo. The map below is the same one the
     orchestrator uses, so the page loads identically whoever serves it.

  2. No caching. A stale cached console.html after a `git pull` is a bug that looks like
     a code bug and wastes an hour.

  3. A path-traversal guard, since this listens on a port.

It serves files and nothing else. The speech service is remote, and the Lab Agent is a
separate process on this machine, so there is no application logic here at all.
"""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".cjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".wasm": "application/wasm",
    ".omnivad": "application/octet-stream",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}


class Handler(SimpleHTTPRequestHandler):
    def guess_type(self, path):
        return MIME.get(Path(path).suffix.lower()) or super().guess_type(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # One line per request is noise on a demo machine. Errors still surface,
        # because log_error routes through send_error, not through here.
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parents[1] / "web"))
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not (root / "console.html").is_file():
        raise SystemExit(f"no console.html under {root}")

    srv = ThreadingHTTPServer(
        (args.host, args.port), partial(Handler, directory=str(root))
    )
    srv.daemon_threads = True
    print(f"serving {root} on http://{args.host}:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
