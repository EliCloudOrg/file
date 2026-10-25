"""假 JWKS 服务器：测试**不自建 SSO**，只验证「怎么验别人的令牌」。

真实 SSO 用 RS256 签发 access token，`kid` 指向 JWKS 里的公钥。这里用同一套
`cryptography` 生成测试密钥（见 `tests/helpers.py`），起一个最小 HTTP 服务发布公钥
（带 ``Cache-Control``），于是 `app/sso_auth.py` 走的是**真实网络路径 + 真实验签**。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import public_jwk


class FakeJwksServer:
    def __init__(self, key, kid: str, *, max_age: int = 300) -> None:
        self.kid = kid
        self.keys: dict[str, dict] = {kid: public_jwk(key, kid)}
        self.max_age = max_age
        self.requests = 0

        server = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
                if self.path.split("?")[0] != "/jwks.json":
                    self.send_error(404)
                    return
                server.requests += 1
                body = json.dumps({"keys": list(server.keys.values())}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", f"public, max-age={server.max_age}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "FakeJwksServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/jwks.json"

    def rotate(self, key, kid: str) -> None:
        """发布一把新公钥（旧的同时保留，模拟"轮换时新旧并存"）。"""
        self.keys[kid] = public_jwk(key, kid)
