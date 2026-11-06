"""Local preview of the web app against the deployed API, for when the public site is switched off.

    python3 tools/dev/serve.py            # then open http://127.0.0.1:8765

Serves apps/web and forwards /api/* to the HTTP API with CloudFront's private origin header (read from
infra/.demo-secrets.json), so the API accepts it. For testing only; it runs on this machine.
"""

import json
import subprocess
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "apps" / "web"
SECRET = json.loads((ROOT / "infra" / ".demo-secrets.json").read_text())["origin_verify"]
API = subprocess.run(
    ["aws", "cloudformation", "describe-stacks", "--region", "eu-west-2", "--stack-name", "ClaimScopeDemo",
     "--output", "text", "--query", "Stacks[0].Outputs[?OutputKey=='ApiEndpoint'].OutputValue"],
    capture_output=True, text=True, check=True,
).stdout.strip()


class Handler(SimpleHTTPRequestHandler):
    def _proxy(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(API + self.path, data=body, method=self.command,
                                     headers={"content-type": self.headers.get("content-type", "application/json"),
                                              "x-origin-verify": SECRET})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                status, headers, data = res.status, res.headers, res.read()
        except urllib.error.HTTPError as err:
            status, headers, data = err.code, err.headers, err.read()
        self.send_response(status)
        for k in ("content-type", "location", "cache-control"):
            if headers.get(k):
                self.send_header(k, headers[k])
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._proxy()
        if self.path.split("?")[0] == "/config.json":
            data = b'{"matterportSdkKey": ""}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            return self.wfile.write(data)
        return super().do_GET()

    do_POST = do_PATCH = do_DELETE = _proxy


PORT = 8765  # not 8080: other local projects (e.g. Docker containers) commonly publish that


if __name__ == "__main__":
    print(f"ClaimScope preview on http://127.0.0.1:{PORT} (API {API})")
    ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(WEB))).serve_forever()
