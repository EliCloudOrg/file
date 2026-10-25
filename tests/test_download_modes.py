"""下载模式：proxy / presign / both × ?direct= 的矩阵，以及 302 的安全细节。"""

from __future__ import annotations

import pytest

from app.storage.base import StorageUnavailable
from tests.fake_storage import MemoryBlobStore


def _cos_store() -> MemoryBlobStore:
    # name="cos" 才是关键：写入行的 storage_backend 因此是 cos，读取时命中这个替身
    return MemoryBlobStore(name="cos", presign_base="https://cos.example")


def _upload(client, upload, auth, data: bytes = b"mode-bytes") -> str:
    response = upload(client, auth(), content=data)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_proxy_mode_streams(client, upload, auth):
    file_id = _upload(client, upload, auth)
    response = client.get(f"/v1/files/{file_id}/content", headers=auth())
    assert response.status_code == 200
    assert response.content == b"mode-bytes"
    assert "Location" not in response.headers
    assert response.headers.get("X-File-Download-Mode") is None


def test_presign_mode_with_local_backend_falls_back_to_proxy(client_factory, upload, auth):
    """local 后端没有预签名能力：配置成 presign 也必须能下载（回落代理）。"""
    proxy_client = client_factory(DOWNLOAD_MODE="presign")
    file_id = _upload(proxy_client, upload, auth)
    response = proxy_client.get(f"/v1/files/{file_id}/content", headers=auth(), follow_redirects=False)
    assert response.status_code == 200
    assert response.content == b"mode-bytes"


def test_presign_mode_returns_302(client_factory, upload, auth):
    store = _cos_store()
    presign_client = client_factory(DOWNLOAD_MODE="presign", storage=store)
    file_id = _upload(presign_client, upload, auth)

    response = presign_client.get(
        f"/v1/files/{file_id}/content", headers=auth(), follow_redirects=False
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("https://cos.example/")
    assert "expires=300" in location
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-File-Download-Mode"] == "presign"
    assert response.content == b""
    # 令牌绝不能出现在重定向目标里
    assert "Bearer" not in location


def test_presign_mode_honours_direct_zero(client_factory, upload, auth):
    store = _cos_store()
    presign_client = client_factory(DOWNLOAD_MODE="presign", storage=store)
    file_id = _upload(presign_client, upload, auth)

    response = presign_client.get(
        f"/v1/files/{file_id}/content?direct=0", headers=auth(), follow_redirects=False
    )
    assert response.status_code == 200
    assert response.content == b"mode-bytes"


def test_both_mode_defaults_to_proxy(client_factory, upload, auth):
    store = _cos_store()
    both_client = client_factory(DOWNLOAD_MODE="both", storage=store)
    file_id = _upload(both_client, upload, auth)

    response = both_client.get(f"/v1/files/{file_id}/content", headers=auth(), follow_redirects=False)
    assert response.status_code == 200


def test_both_mode_direct_one_redirects(client_factory, upload, auth):
    store = _cos_store()
    both_client = client_factory(DOWNLOAD_MODE="both", storage=store)
    file_id = _upload(both_client, upload, auth)

    response = both_client.get(
        f"/v1/files/{file_id}/content?direct=1", headers=auth(), follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://cos.example/")


def test_proxy_mode_ignores_direct_one(client_factory, upload, auth):
    store = _cos_store()
    proxy_client = client_factory(DOWNLOAD_MODE="proxy", storage=store)
    file_id = _upload(proxy_client, upload, auth)

    response = proxy_client.get(
        f"/v1/files/{file_id}/content?direct=1", headers=auth(), follow_redirects=False
    )
    assert response.status_code == 200


def test_304_takes_precedence_over_redirect(client_factory, upload, auth):
    store = _cos_store()
    presign_client = client_factory(DOWNLOAD_MODE="presign", storage=store)
    file_id = _upload(presign_client, upload, auth)
    body = presign_client.get(f"/v1/files/{file_id}", headers=auth()).json()

    response = presign_client.get(
        f"/v1/files/{file_id}/content",
        headers={**auth(), "If-None-Match": f'"{body["sha256"]}"'},
        follow_redirects=False,
    )
    assert response.status_code == 304
    assert "Location" not in response.headers


def test_head_never_redirects(client_factory, upload, auth):
    store = _cos_store()
    presign_client = client_factory(DOWNLOAD_MODE="presign", storage=store)
    file_id = _upload(presign_client, upload, auth)

    response = presign_client.head(f"/v1/files/{file_id}/content", headers=auth(), follow_redirects=False)
    assert response.status_code == 200
    assert "Location" not in response.headers
    assert response.headers["Content-Length"] == str(len(b"mode-bytes"))


def test_presign_failure_is_503(client_factory, upload, auth):
    class BrokenPresignStore(MemoryBlobStore):
        def presigned_url(self, key, expires_seconds):
            raise StorageUnavailable("simulated presign failure")

    store = BrokenPresignStore(name="cos", presign_base="https://cos.example")
    broken = client_factory(DOWNLOAD_MODE="presign", storage=store)
    file_id = _upload(broken, upload, auth)

    response = broken.get(f"/v1/files/{file_id}/content", headers=auth(), follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["error"] == "storage_unavailable"


@pytest.mark.parametrize("mode", ["proxy", "presign", "both"])
def test_all_modes_respect_visibility(client_factory, upload, auth, mode):
    store = _cos_store()
    scoped = client_factory(DOWNLOAD_MODE=mode, storage=store)
    response = upload(scoped, auth(sub="user_owner"), content=b"secret", visibility="private")
    file_id = response.json()["id"]

    denied = scoped.get(f"/v1/files/{file_id}/content?direct=1", headers=auth(sub="user_other"), follow_redirects=False)
    assert denied.status_code == 404
