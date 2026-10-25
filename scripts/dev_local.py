"""本地开发启动器：假 SSO + 本地存储 + uvicorn（不需要 Docker、不需要真实 COS）。

    .\\.venv\\Scripts\\python.exe scripts\\dev_local.py
    .\\.venv\\Scripts\\python.exe scripts\\dev_local.py --port 8012
    .\\.venv\\Scripts\\python.exe scripts\\dev_local.py --real-sso   # 用环境里的真实 SSO

默认行为：

* 在 127.0.0.1 起一个**假 JWKS 服务**，并打印一枚用它签名的开发令牌；
* 数据放 `./data/dev/`（blobs / tmp / file.db），内容写本地磁盘；
* `DOWNLOAD_MODE=both`，`?direct=1` 在本地后端会回落代理（没有预签名能力）；
* uvicorn 监听 127.0.0.1:8002（8000=sso、8001=mc 已占）。

⚠️ 打印出来的令牌只对**这个假 JWKS** 有效，绝不能拿到线上用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

KID = "dev-kid"
AUDIENCE = "elicloud-services"
READ_SCOPE = "files:read"
WRITE_SCOPE = "files:write"
SCOPE = f"openid profile {READ_SCOPE} {WRITE_SCOPE}"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64url_uint(value: int) -> str:
    import base64

    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_jwk() -> dict:
    numbers = KEY.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": KID,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


class JwksServer:
    def __init__(self) -> None:
        body = json.dumps({"keys": [_public_jwk()]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.split("?")[0] != "/jwks.json":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "JwksServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])


def issue_token(issuer: str, sub: str) -> str:
    import time

    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": AUDIENCE,
            "sub": sub,
            "iat": now,
            "exp": now + 12 * 3600,
            "scope": SCOPE,
            "preferred_username": sub,
        },
        KEY,
        algorithm="RS256",
        headers={"kid": KID},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="file 服务本地开发启动器")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--sub", default="user_dev")
    parser.add_argument("--real-sso", action="store_true", help="使用环境变量里的 SSO 配置，不启假 JWKS")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    data_dir = ROOT / "data" / "dev"
    data_dir.mkdir(parents=True, exist_ok=True)

    jwks: JwksServer | None = None
    if args.real_sso:
        issuer = os.environ.get("SSO_ISSUER", "https://146.56.237.33/auth")
        print(f"[dev] 使用环境变量里的真实 SSO（issuer={issuer}），不会打印令牌")
    else:
        jwks = JwksServer().start()
        issuer = f"http://127.0.0.1:{jwks.port}/auth"
        os.environ.update(
            {
                "SSO_ISSUER": issuer,
                "SSO_JWKS_URL": f"http://127.0.0.1:{jwks.port}/jwks.json",
            }
        )

    os.environ.update(
        {
            "JWT_AUDIENCE": AUDIENCE,
            "REQUIRED_READ_SCOPE": READ_SCOPE,
            "REQUIRED_WRITE_SCOPE": WRITE_SCOPE,
            "DATABASE_URL": f"sqlite:///{(data_dir / 'file.db').as_posix()}",
            "STORAGE_BACKEND": "local",
            "LOCAL_STORAGE_ROOT": str(data_dir / "blobs"),
            "TEMP_ROOT": str(data_dir / "tmp"),
            "DOWNLOAD_MODE": "both",
            "CLEANUP_ENABLED": "true",
            "LOG_LEVEL": args.log_level,
        }
    )

    base = f"http://127.0.0.1:{args.port}"
    print(f"[dev] healthz : {base}/healthz")
    print(f"[dev] 数据目录: {data_dir}")
    if jwks is not None:
        token = issue_token(issuer, args.sub)
        token_file = data_dir / "dev_token.txt"
        token_file.write_text(token, encoding="ascii")
        print(f"[dev] 开发令牌（sub={args.sub}，仅对本地假 SSO 有效）:")
        print(token)
        print(f"[dev] 令牌已写入（gitignored）：{token_file}")
        print("")
        print("[dev] 试一下（PowerShell）：")
        print(f'  curl.exe {base}/healthz')
        print(f'  curl.exe -H "Authorization: Bearer {token}" {base}/v1/me')
        print(
            f'  curl.exe -H "Authorization: Bearer {token}" '
            f'-F "file=@README.md" -F "visibility=private" {base}/v1/files'
        )
        print("")

    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level=args.log_level)
    finally:
        if jwks is not None:
            jwks.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
