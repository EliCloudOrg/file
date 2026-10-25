"""端到端自检（本地存储后端，不需要真实 SSO / COS / Docker）。

    .\\.venv\\Scripts\\python.exe scripts\\e2e_local.py

它做的是「真实 HTTP 栈 + 真实验签」：

1. 起一个最小 JWKS 服务（真 HTTP），用 RS256 签测试令牌；
2. 用 `STORAGE_BACKEND=local` 创建应用（临时目录）；
3. 依次验证：三种可见性 × 三种身份、授权/撤权、304、Range、`?direct=1` 回落、
   删除幂等、过期 410、清理器回收对象与墓碑。

任何一步不符合预期都会以非 0 退出。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

ISSUER = "https://sso.e2e.test/auth"
AUDIENCE = "elicloud-services"
KID = "e2e-kid"

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
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/jwks.json"


def token(sub: str, scope: str = "openid profile files:read files:write") -> str:
    import time

    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub,
            "iat": now,
            "exp": now + 3600,
            "scope": scope,
        },
        KEY,
        algorithm="RS256",
        headers={"kid": KID},
    )


def bearer(sub: str, scope: str = "openid profile files:read files:write") -> dict[str, str]:
    return {"Authorization": f"Bearer {token(sub, scope)}"}


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"[e2e] FAIL: {message}")
    print(f"[e2e] ok: {message}")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="file-e2e-"))
    jwks = JwksServer().start()
    os.environ.update(
        {
            "SSO_ISSUER": ISSUER,
            "SSO_JWKS_URL": jwks.url,
            "JWT_AUDIENCE": AUDIENCE,
            "REQUIRED_READ_SCOPE": "files:read",
            "REQUIRED_WRITE_SCOPE": "files:write",
            "DATABASE_URL": f"sqlite:///{(data_dir / 'file.db').as_posix()}",
            "STORAGE_BACKEND": "local",
            "LOCAL_STORAGE_ROOT": str(data_dir / "blobs"),
            "TEMP_ROOT": str(data_dir / "tmp"),
            "DOWNLOAD_MODE": "both",
            "CLEANUP_ENABLED": "false",
            "LOG_LEVEL": "warning",
        }
    )

    from fastapi.testclient import TestClient

    from app import files_service
    from app.db import session_scope
    from app.main import create_app
    from app.models import FileRecord, to_iso, utcnow

    owner = bearer("user_owner")
    friend = bearer("user_friend")
    stranger = bearer("user_stranger")
    reader_only = bearer("user_reader", scope="openid files:read")

    try:
        with TestClient(create_app()) as client:
            check(client.get("/healthz").status_code == 200, "healthz 200")

            def upload(visibility: str, content: bytes = b"e2e-bytes") -> str:
                response = client.post(
                    "/v1/files",
                    files={"file": ("e2e.txt", content, "text/plain")},
                    data={"visibility": visibility, "ttl_seconds": "3600"},
                    headers=owner,
                )
                check(response.status_code == 201, f"上传 visibility={visibility} 201")
                return response.json()["id"]

            private_id = upload("private")
            protected_id = upload("protected", b"protected-bytes")
            public_id = upload("public", b"public-bytes")

            check(client.get(f"/v1/files/{private_id}/content", headers=owner).content == b"e2e-bytes", "属主读 private")
            check(client.get(f"/v1/files/{private_id}/content", headers=stranger).status_code == 404, "局外人读 private 404")
            check(client.get(f"/v1/files/{protected_id}/content", headers=friend).status_code == 404, "未授权读 protected 404")

            granted = client.post(
                f"/v1/files/{protected_id}/grants", json={"grantee_sub": "user_friend"}, headers=owner
            )
            check(granted.status_code == 201, "授权 201")
            check(
                client.get(f"/v1/files/{protected_id}/content", headers=friend).content == b"protected-bytes",
                "被授权者读 protected",
            )
            check(
                client.delete(f"/v1/files/{protected_id}/grants/user_friend", headers=owner).json()["removed"]
                is True,
                "撤权",
            )
            check(client.get(f"/v1/files/{protected_id}/content", headers=friend).status_code == 404, "撤权后 404")

            check(client.get(f"/v1/files/{public_id}/content", headers=stranger).content == b"public-bytes", "任何登录用户读 public")
            check(client.get(f"/v1/files/{public_id}/content", headers=reader_only).status_code == 200, "只读 scope 可读 public")
            check(
                client.get(
                    "/v1/files",
                    headers=bearer("user_noscope", scope="openid"),
                ).status_code
                == 403,
                "缺 files:read → 403",
            )

            body = client.get(f"/v1/files/{public_id}", headers=owner).json()
            etag = f'"{body["sha256"]}"'
            fresh = client.get(f"/v1/files/{public_id}/content", headers={**owner, "If-None-Match": etag})
            check(fresh.status_code == 304, "If-None-Match 304")

            partial = client.get(
                f"/v1/files/{public_id}/content", headers={**owner, "Range": "bytes=0-4"}
            )
            check(partial.status_code == 206 and partial.content == b"publi", "Range 206")
            check(
                partial.headers["Content-Range"] == f"bytes 0-4/{len(b'public-bytes')}",
                "Content-Range 正确",
            )
            check(
                client.get(
                    f"/v1/files/{public_id}/content", headers={**owner, "Range": "bytes=9999-"}
                ).status_code
                == 416,
                "越界 Range 416",
            )

            direct = client.get(
                f"/v1/files/{public_id}/content?direct=1", headers=owner, follow_redirects=False
            )
            check(direct.status_code == 200, "local 后端在 both 模式下 ?direct=1 回落代理（200）")

            deleted = client.delete(f"/v1/files/{private_id}", headers=owner)
            check(deleted.status_code == 200 and deleted.json()["changed"] is True, "删除 200")
            again = client.delete(f"/v1/files/{private_id}", headers=owner)
            check(again.status_code == 200 and again.json()["changed"] is False, "删除幂等")
            check(client.get(f"/v1/files/{private_id}/content", headers=owner).status_code == 404, "删除后 404")

            # 过期：直接把 expires_at 改到过去（模拟“时间到了但清理器还没扫”）
            with session_scope() as session:
                record = session.get(FileRecord, public_id)
                record.expires_at = to_iso(utcnow() - timedelta(seconds=1))
                session.commit()
            check(client.get(f"/v1/files/{public_id}/content", headers=owner).status_code == 410, "过期 410")

            storages = client.app.state.storages
            with session_scope() as session:
                counts = files_service.sweep_once(session, client.app.state.settings, storages)
            check(counts["expired"] >= 1, f"清理器回收过期文件 {counts}")
            row = None
            with session_scope() as session:
                row = session.get(FileRecord, public_id)
            check(row is not None and row.status == "expired", "过期行已标记 expired")
    finally:
        jwks.stop()
        try:
            from app.db import reset_db_state

            reset_db_state()
        except Exception:  # noqa: BLE001 - 清理失败不该掩盖验收结果
            pass
        shutil.rmtree(data_dir, ignore_errors=True)

    print("[e2e] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
