"""上传：元数据、TTL、大小上限、配额、文件名净化、危险 MIME、失败回滚。"""

from __future__ import annotations

import hashlib
import json
import re

import pytest

from tests.helpers import make_token, bearer


def test_upload_returns_metadata(client, auth, upload):
    response = upload(client, auth(sub="user_u"), content=b"hello world", filename="note.txt")
    assert response.status_code == 201
    body = response.json()

    assert re.match(r"^f_[A-Za-z0-9_-]{22}$", body["id"])
    assert body["filename"] == "note.txt"
    assert body["content_type"] == "text/plain"
    assert body["size_bytes"] == len(b"hello world")
    assert body["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert body["visibility"] == "private"
    assert body["status"] == "active"
    assert body["expires_in"] == pytest.approx(3600, abs=5)
    assert body["expires_at"].endswith("Z")
    assert body["permanent"] is False
    assert body["content_path"] == f"/v1/files/{body['id']}/content"


def test_upload_custom_ttl(client, auth, upload):
    response = upload(client, auth(), content=b"x", ttl_seconds=60)
    assert response.status_code == 201
    assert response.json()["expires_in"] == pytest.approx(60, abs=5)


@pytest.mark.parametrize("ttl", ["-5", "86401", "not-a-number"])
def test_upload_invalid_ttl_is_400(client, auth, upload, ttl):
    response = upload(client, auth(), content=b"x", ttl_seconds=ttl)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_upload_permanent_public_file(client, auth, upload):
    response = upload(client, auth(), content=b"forever", ttl_seconds=0, visibility="public")
    assert response.status_code == 201
    body = response.json()
    assert body["visibility"] == "public"
    assert body["permanent"] is True
    assert body["expires_at"] is None
    assert body["expires_in"] is None


@pytest.mark.parametrize("visibility", ["private", "protected"])
def test_permanent_file_requires_public(client, auth, upload, visibility):
    response = upload(client, auth(), content=b"x", ttl_seconds=0, visibility=visibility)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_upload_too_large_is_413(client, auth, upload, db_rows):
    response = upload(client, auth(), content=b"x" * (65536 + 1))
    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"
    assert db_rows["files"]() == []


def test_upload_content_length_precheck(client, auth):
    """远超上限的请求体在解析 multipart 之前就该被拒。"""
    big = b"x" * (65536 + 65536 + 1024)
    response = client.post(
        "/v1/files",
        content=big,
        headers={**auth(), "Content-Type": "multipart/form-data; boundary=xyz"},
    )
    assert response.status_code == 413


def test_upload_empty_file_is_allowed(client, auth, upload):
    response = upload(client, auth(), content=b"")
    assert response.status_code == 201
    body = response.json()
    assert body["size_bytes"] == 0
    assert body["sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        (".hidden", "hidden"),
        ("a" * 300, "a" * 200),
    ],
)
def test_filename_is_sanitized(client, auth, upload, raw, expected):
    response = upload(client, auth(), content=b"x", filename=raw)
    assert response.status_code == 201
    assert response.json()["filename"] == expected


def test_sanitize_filename_strips_control_chars():
    """控制字符没法原样进 multipart 头（httpx 会百分号编码），这里直接测净化函数。"""
    from app.files_service import sanitize_filename

    assert sanitize_filename("bad\x00name\x1f.txt") == "badname.txt"
    assert sanitize_filename(None) == "file"


def test_empty_filename_falls_back(client, auth, upload):
    response = upload(client, auth(), content=b"x", filename="   ")
    assert response.status_code == 201
    assert response.json()["filename"] == "file"


@pytest.mark.parametrize(
    "dangerous", ["text/html", "image/svg+xml", "application/javascript", "application/xhtml+xml"]
)
def test_dangerous_content_type_is_downgraded(client, auth, upload, dangerous):
    response = upload(client, auth(), content=b"<script>alert(1)</script>", content_type=dangerous)
    assert response.status_code == 201
    assert response.json()["content_type"] == "application/octet-stream"


def test_content_type_with_charset_is_normalized(client, auth, upload):
    response = upload(client, auth(), content=b"x", content_type="text/plain; charset=utf-8")
    assert response.status_code == 201
    assert response.json()["content_type"] == "text/plain"


def test_invalid_content_type_is_400(client, auth, upload):
    response = upload(client, auth(), content=b"x", content_type="not a type")
    assert response.status_code == 400


def test_invalid_visibility_is_400(client, auth, upload):
    response = upload(client, auth(), content=b"x", visibility="world")
    assert response.status_code == 400


def test_file_count_quota(client_factory, upload, auth):
    limited = client_factory(QUOTA_MAX_FILES=1)
    headers = auth(sub="user_quota")
    assert upload(limited, headers, content=b"a").status_code == 201
    second = upload(limited, headers, content=b"b")
    assert second.status_code == 409
    assert second.json()["error"] == "quota_exceeded"


def test_byte_quota(client_factory, upload, auth):
    limited = client_factory(MAX_FILE_BYTES=10, QUOTA_MAX_BYTES=10)
    headers = auth(sub="user_bytes")
    assert upload(limited, headers, content=b"x" * 8).status_code == 201
    second = upload(limited, headers, content=b"y" * 8)
    assert second.status_code == 409
    assert second.json()["error"] == "quota_exceeded"


def test_global_capacity_is_enforced(client_factory, upload, auth):
    limited = client_factory(MAX_FILE_BYTES=10, QUOTA_MAX_BYTES=10, MAX_TOTAL_BYTES=10)
    assert upload(limited, auth(sub="user_a"), content=b"a" * 8).status_code == 201
    second = upload(limited, auth(sub="user_b"), content=b"b" * 8)
    assert second.status_code == 409
    assert second.json()["error"] == "quota_exceeded"


def test_storage_failure_is_503_and_leaves_nothing(client_factory, upload, auth, memory_store, db_rows):
    store = memory_store
    store.fail_write = True
    failing = client_factory(storage=store)

    response = upload(failing, auth(sub="user_s"), content=b"data")
    assert response.status_code == 503
    assert response.json()["error"] == "storage_unavailable"
    assert db_rows["files"]() == []

    from app.config import get_settings

    temp_root = get_settings().temp_root
    from pathlib import Path

    assert list(Path(temp_root).glob("tmp_*")) == []


def test_write_rate_limit_is_429(client_factory, upload, auth):
    limited = client_factory(UPLOAD_ATTEMPTS_PER_WINDOW=1, UPLOAD_WINDOW_SECONDS=600)
    headers = auth(sub="user_rl")
    assert upload(limited, headers, content=b"1").status_code == 201
    second = upload(limited, headers, content=b"2")
    assert second.status_code == 429
    assert second.json()["error"] == "rate_limited"
    assert "Retry-After" in second.headers


def test_upload_is_audited(client, auth, upload, db_rows):
    response = upload(client, auth(sub="user_audit"), content=b"audited")
    file_id = response.json()["id"]
    audits = db_rows["audits"](action="upload", target=file_id)
    assert len(audits) == 1
    detail = json.loads(audits[0].detail)
    assert detail["size_bytes"] == len(b"audited")
    assert the_detail_has_no_content(detail)


def the_detail_has_no_content(detail: dict) -> bool:
    serialized = json.dumps(detail)
    return "audited" not in serialized


def test_upload_requires_file_field(client, auth):
    response = client.post("/v1/files", data={"visibility": "private"}, headers=auth())
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_user_cannot_see_other_users_file_in_quota(client_factory, upload, auth, client):
    """配额按 sub 统计：别的用户上传不该影响我的名额。"""
    limited = client_factory(QUOTA_MAX_FILES=1)
    assert upload(limited, auth(sub="user_a"), content=b"a").status_code == 201
    assert upload(limited, auth(sub="user_b"), content=b"b").status_code == 201


def test_upload_ignores_extra_form_fields(client, auth, upload):
    response = client.post(
        "/v1/files",
        files={"file": ("x.txt", b"x", "text/plain")},
        data={"visibility": "private", "unexpected": "1"},
        headers=auth(),
    )
    assert response.status_code == 201


def test_write_token_from_other_issuer_rejected(client, upload):
    headers = bearer(make_token(scope="openid files:read files:write", issuer="https://other/auth"))
    assert upload(client, headers, content=b"x").status_code == 401
