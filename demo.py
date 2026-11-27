"""Start the credential-free workbench with python3 demo.py."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import webbrowser

from replay_engine import record
from workflow import MODES

DIST = Path(__file__).resolve().parent / "web" / "dist" / "replay"


def recordings():
    return json.dumps({mode: record(mode) for mode in MODES},
                      ensure_ascii=False, separators=(",", ":")).encode()


class DemoHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/recordings.json":
            data = self.server.recordings
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--record", type=Path, metavar="JSON")
    args = parser.parse_args()
    if args.record:
        args.record.write_bytes(recordings())
        return
    if not (DIST / "index.html").is_file():
        parser.exit(1, "缺少控制台构建，请在 web-src 中运行 npm ci && npm run build\n")
    with ThreadingHTTPServer(("127.0.0.1", args.port),
                             partial(DemoHandler, directory=str(DIST))) as server:
        server.recordings = recordings()
        url = f"http://127.0.0.1:{server.server_port}/"
        print(url, flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
