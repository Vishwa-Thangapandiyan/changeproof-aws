"""Serve the frontend with a live local prediction API. No AWS, no cost.

    python scripts/serve.py            # opens http://127.0.0.1:8000
    python scripts/serve.py 8765 --no-open

Bound to 127.0.0.1 only, so it is not reachable from other machines.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend" / "lambda"), str(ROOT / "backend")]

from demo_api import DemoInputError, predict_for  # noqa: E402

FRONTEND = ROOT / "frontend"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/api/predict":
            return super().do_GET()
        raw = parse_qs(url.query).get("after", [""])[0]
        try:
            body, status = predict_for(int(raw)), 200
        except (ValueError, DemoInputError) as error:
            body, status = {"error": str(error) if isinstance(error, DemoInputError) else "concurrency must be a whole number"}, 422
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    candidates = [int(args[0])] if args else range(8000, 8020)
    server = None
    for port in candidates:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("no free port found; pass one explicitly, e.g. python scripts/serve.py 9000")
    url = f"http://127.0.0.1:{port}"
    print(f"ChangeProof live engine on {url}  (Ctrl+C to stop)", flush=True)
    if '--no-open' not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
