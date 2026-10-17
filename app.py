"""Local web app: drop a score PDF in the browser, get it back with note names.

Run: .venv/bin/python app.py   then open http://localhost:8765
"""
import json
import tempfile
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import note_names

import importlib.util

omr_label = None
if importlib.util.find_spec("homr"):  # optional: scanned scores need the homr OMR package
    import omr_label

PORT = 8765
PAGE = (Path(__file__).parent / "app.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlparse(self.path).path != "/":
            return self.send_error(404)
        self.reply(200, "text/html; charset=utf-8", PAGE)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/annotate":
            return self.send_error(404)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        ext = q.get("ext", "pdf").lower()
        if ext not in ("pdf", "png", "jpg", "jpeg"):
            return self.error("only PDF, PNG and JPG files are supported")
        args = (q.get("style", "letters"), q.get("color", "red"), float(q.get("size", 5.4)))
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "in." + ext), Path(tmp, "out.pdf")
            src.write_bytes(data)
            try:
                if q.get("mode") == "omr":
                    if omr_label is None:
                        return self.error("the OMR model (homr) is not installed")
                    n, ok = omr_label.annotate(src, dst, *args, progress=lambda s: None)
                    headers = {"X-Notes": str(n), "X-Confirmed": str(ok), "X-Mode": "omr"}
                else:
                    n = note_names.annotate(src, dst, *args)
                    headers = {"X-Notes": str(n), "X-Mode": "exact"}
            except note_names.UnsupportedPDF as e:
                scanned = q.get("mode") != "omr" and omr_label is not None
                return self.error(str(e), scanned=scanned)
            except Exception as e:  # not a PDF, corrupt file, ...
                return self.error(f"could not read this file ({e})")
            self.reply(200, "application/pdf", dst.read_bytes(), headers)

    def error(self, message, **extra):
        self.reply(422, "application/json", json.dumps({"error": message, **extra}).encode())

    def reply(self, code, ctype, body, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Open http://localhost:{PORT}  (Ctrl+C to stop)")
    webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()
