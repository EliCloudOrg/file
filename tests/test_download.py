"""下载：字节一致、ETag/304、Range/416、HEAD、过期/删除、危险类型、对象丢失自愈。"""

from __future__ import annotations

import hashlib
from pathlib import Path


def _etag(data: bytes) -> str:
    return f'"{hashlib.sha256(data).hexdigest()}"'


def _create(upload, client, headers, data: bytes = b"0123456789abcdef", **kwargs) -> str:
    response = upload(client, headers, content=data, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_download_roundtrip_and_headers(client, auth, upload):
    data = b"0123456789abcdef"
    headers = auth(sub="user_dl")
    file_id = _create(upload, client, headers, data)

    response = client.get(f"/v1/files/{file_id}/content", headers=headers)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["ETag"] == _etag(data)
    assert response.headers["Content-Length"] == str(len(data))
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Cache-Control"] == "private, max-age=0, must-revalidate"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert response.headers["Content-Security-Policy"] == "sandbox"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_download_304_when_etag_matches(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"cached")
    response = client.get(
        f"/v1/files/{file_id}/content",
        headers={**headers, "If-None-Match": _etag(b"cached")},
    )
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["ETag"] == _etag(b"cached")


def test_download_ignores_non_matching_etag(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"fresh")
    response = client.get(
        f"/v1/files/{file_id}/content",
        headers={**headers, "If-None-Match": '"deadbeef"'},
    )
    assert response.status_code == 200
    assert response.content == b"fresh"


# --------------------------------------------------------------------- Range


def test_range_prefix(client, auth, upload):
    headers = auth()
    data = b"0123456789"
    file_id = _create(upload, client, headers, data)
    response = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=0-4"})
    assert response.status_code == 206
    assert response.content == b"01234"
    assert response.headers["Content-Range"] == f"bytes 0-4/{len(data)}"
    assert response.headers["Content-Length"] == "5"


def test_range_open_ended(client, auth, upload):
    headers = auth()
    data = b"0123456789"
    file_id = _create(upload, client, headers, data)
    response = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=6-"})
    assert response.status_code == 206
    assert response.content == b"6789"
    assert response.headers["Content-Range"] == f"bytes 6-9/{len(data)}"


def test_range_suffix(client, auth, upload):
    headers = auth()
    data = b"0123456789"
    file_id = _create(upload, client, headers, data)
    response = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=-3"})
    assert response.status_code == 206
    assert response.content == b"789"


def test_range_end_beyond_size_is_clamped(client, auth, upload):
    headers = auth()
    data = b"0123456789"
    file_id = _create(upload, client, headers, data)
    response = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=8-9999"})
    assert response.status_code == 206
    assert response.content == b"89"
    assert response.headers["Content-Range"] == f"bytes 8-9/{len(data)}"


def test_range_start_beyond_size_is_416(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"0123456789")
    response = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=100-"})
    assert response.status_code == 416
    assert response.headers["Content-Range"] == "bytes */10"


def test_multi_range_is_416(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"0123456789")
    response = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=0-1,3-4"})
    assert response.status_code == 416


def test_malformed_range_is_416(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"0123456789")
    response = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "pages=0-1"})
    assert response.status_code == 416


def test_if_range_mismatch_returns_full_body(client, auth, upload):
    headers = auth()
    data = b"0123456789"
    file_id = _create(upload, client, headers, data)
    response = client.get(
        f"/v1/files/{file_id}/content",
        headers={**headers, "Range": "bytes=0-1", "If-Range": '"stale-etag"'},
    )
    assert response.status_code == 200
    assert response.content == data


def test_if_range_match_returns_partial(client, auth, upload):
    headers = auth()
    data = b"0123456789"
    file_id = _create(upload, client, headers, data)
    response = client.get(
        f"/v1/files/{file_id}/content",
        headers={**headers, "Range": "bytes=0-1", "If-Range": _etag(data)},
    )
    assert response.status_code == 206
    assert response.content == b"01"


# ---------------------------------------------------------------------- HEAD


def test_head_returns_headers_without_body(client, auth, upload):
    headers = auth()
    data = b"head-me"
    file_id = _create(upload, client, headers, data)
    response = client.head(f"/v1/files/{file_id}/content", headers=headers)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["Content-Length"] == str(len(data))
    assert response.headers["ETag"] == _etag(data)
    assert response.headers["Accept-Ranges"] == "bytes"


def test_head_ignores_range(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"0123456789")
    response = client.head(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=0-1"})
    assert response.status_code == 200
    assert response.headers["Content-Length"] == "10"


# ------------------------------------------------------------- 错误与边界


def test_unknown_file_is_404(client, auth):
    assert client.get("/v1/files/f_" + "A" * 22, headers=auth()).status_code == 404
    assert client.get("/v1/files/nope", headers=auth()).status_code == 404
    assert client.get("/v1/files/nope/content", headers=auth()).status_code == 404


def test_expired_file_is_410(client, auth, upload, expire_file):
    headers = auth()
    file_id = _create(upload, client, headers, b"gone")
    expire_file(file_id)
    assert client.get(f"/v1/files/{file_id}/content", headers=headers).status_code == 410
    assert client.get(f"/v1/files/{file_id}/content", headers=headers).json()["error"] == "file_expired"


def test_deleted_file_is_404(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"gone")
    assert client.delete(f"/v1/files/{file_id}", headers=headers).status_code == 200
    assert client.get(f"/v1/files/{file_id}/content", headers=headers).status_code == 404


def test_dangerous_content_type_is_downgraded_on_download(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"<html></html>", content_type="text/html")
    response = client.get(f"/v1/files/{file_id}/content", headers=headers)
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert "attachment" in response.headers["Content-Disposition"]


def test_utf8_filename_in_content_disposition(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"pdf", filename="报告 v2.pdf")
    response = client.get(f"/v1/files/{file_id}/content", headers=headers)
    disposition = response.headers["Content-Disposition"]
    assert "filename*=UTF-8''" in disposition
    assert "%E6%8A%A5%E5%91%8A" in disposition


def test_empty_file_download(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"")
    response = client.get(f"/v1/files/{file_id}/content", headers=headers)
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["Content-Length"] == "0"

    ranged = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": "bytes=0-0"})
    assert ranged.status_code == 416


def test_missing_object_self_heals(client, auth, upload, db_rows):
    headers = auth()
    file_id = _create(upload, client, headers, b"will-vanish")

    settings = client.app.state.settings
    key = client.app.state.storages.primary.key_for(file_id)
    path = Path(settings.local_storage_root) / key
    path.unlink()

    response = client.get(f"/v1/files/{file_id}/content", headers=headers)
    assert response.status_code == 404
    rows = db_rows["files"](id=file_id)
    assert rows and rows[0].status == "deleted"
    assert db_rows["audits"](action="missing_object", target=file_id)


def test_download_does_not_leak_storage_path(client, auth, upload):
    headers = auth()
    file_id = _create(upload, client, headers, b"x")
    response = client.get(f"/v1/files/{file_id}/content", headers=headers)
    serialized = repr(dict(response.headers))
    assert "blobs/" not in serialized
    assert client.app.state.settings.local_storage_root not in serialized
