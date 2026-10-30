import argparse
import http.client
import json
import mimetypes
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..paths import POSE_ASSET_ROOT


ASSET_ROOT = Path(os.environ.get("IMAGE_SERVICE_POSE_ASSET_ROOT", str(POSE_ASSET_ROOT)))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/":
            self.reply(200, "text/html; charset=utf-8", Path(__file__).with_name("index.html").read_bytes())
        elif self.path == "/pose-preview":
            self.reply(200, "text/html; charset=utf-8", Path(__file__).resolve().parents[1].joinpath("pose_render.html").read_bytes())
        elif self.path in ("/health", "/ready"):
            self.proxy()
        else:
            path = unquote(urlsplit(self.path).path)
            for prefix in ("/pose_studio/", "/shared/", "/vendor/three/"):
                if path.startswith(prefix):
                    root = (ASSET_ROOT / prefix.strip("/")).resolve()
                    asset = (root / path[len(prefix):]).resolve()
                    if asset.is_relative_to(root) and asset.is_file():
                        content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                        self.reply(200, content_type, asset.read_bytes())
                        return
            self.reply(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path not in ("/api/image/edit", "/api/pose/search"):
            self.reply(404, "text/plain", b"Not found")
            return
        self.proxy()

    def proxy(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.reply(400, "text/plain", b"Invalid Content-Length")
            return
        if self.headers.get("Transfer-Encoding") or not 0 <= length <= 128 * 1024 * 1024:
            self.reply(413, "text/plain", b"Request too large or unsupported transfer encoding")
            return

        connection = http.client.HTTPSConnection(self.server.api_host, self.server.api_port, context=self.server.tls_context, timeout=1800)
        response_started = False
        try:
            headers = {"Content-Length": str(length)}
            for name in ("Authorization", "Content-Type"):
                if name in self.headers:
                    headers[name] = self.headers[name]
            connection.putrequest(self.command, self.path)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    return
                connection.send(chunk)
                remaining -= len(chunk)
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/octet-stream"))
            if response.getheader("Content-Length") is not None:
                self.send_header("Content-Length", response.getheader("Content-Length"))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            response_started = True
            while chunk := response.read(65536):
                self.wfile.write(chunk)
        except (OSError, http.client.HTTPException):
            if not response_started:
                body = json.dumps({"error": {"message": "连接 443 API 失败，请检查地址、CA 证书及服务状态"}}, ensure_ascii=False).encode()
                self.reply(502, "application/json; charset=utf-8", body)
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description="本地生图 API 测试页面，不修改现有服务")
    parser.add_argument("--api-base-url", required=True, help="例如 https://10.21.16.104")
    parser.add_argument("--ca-file", help="部署时生成的 lan-ca.crt 公共证书路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    api = urlsplit(args.api_base_url)
    if api.scheme != "https" or not api.hostname or api.path not in ("", "/") or api.query or api.fragment or api.username or api.password:
        parser.error("--api-base-url 必须是 HTTPS 服务地址，不含路径或凭据")
    context = ssl.create_default_context(cafile=args.ca_file)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.api_host = api.hostname
    server.api_port = api.port or 443
    server.tls_context = context
    print(f"测试页面：http://{args.host}:{args.port} → {args.api_base_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
