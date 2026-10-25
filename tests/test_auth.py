"""令牌验签与 scope：401 / 403 的边界必须与平台其它服务一致。"""

from __future__ import annotations

import pytest

from tests.helpers import TEST_KID, TEST_OTHER_KEY, bearer, generate_key, make_token


def test_healthz_needs_no_token(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_missing_token_is_401(client):
    response = client.get("/v1/files")
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_non_bearer_scheme_is_401(client):
    response = client.get("/v1/files", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


def test_garbage_token_is_401(client):
    response = client.get("/v1/files", headers=bearer("not-a-jwt"))
    assert response.status_code == 401


def test_expired_token_is_401(client, auth):
    headers = auth(expires_in=-60)
    response = client.get("/v1/files", headers=headers)
    assert response.status_code == 401


def test_wrong_signature_is_401(client, auth):
    headers = auth(key=TEST_OTHER_KEY)
    response = client.get("/v1/files", headers=headers)
    assert response.status_code == 401


def test_wrong_issuer_is_401(client, auth):
    headers = auth(issuer="https://evil.example/auth")
    response = client.get("/v1/files", headers=headers)
    assert response.status_code == 401


def test_id_token_audience_is_401(client, auth):
    """id_token 的 aud 是 client_id，用它冒充 access token 必须被拒。"""
    headers = auth(audience="some-client-id")
    response = client.get("/v1/files", headers=headers)
    assert response.status_code == 401


def test_hs256_is_rejected(client):
    import jwt

    token = jwt.encode(
        {"iss": "https://sso.test/auth", "aud": "elicloud-services", "sub": "u", "exp": 9999999999, "scope": "files:read"},
        "secret",
        algorithm="HS256",
        headers={"kid": TEST_KID},
    )
    response = client.get("/v1/files", headers=bearer(token))
    assert response.status_code == 401


def test_alg_none_is_rejected(client):
    import jwt

    token = jwt.encode(
        {"iss": "https://sso.test/auth", "aud": "elicloud-services", "sub": "u", "exp": 9999999999, "scope": "files:read"},
        key=None,
        algorithm="none",
        headers={"kid": TEST_KID},
    )
    response = client.get("/v1/files", headers=bearer(token))
    assert response.status_code == 401


def test_unknown_kid_is_401(client):
    token = make_token(kid="unknown-kid")
    response = client.get("/v1/files", headers=bearer(token))
    assert response.status_code == 401


def test_jwks_rotation_is_picked_up(client, jwks):
    """SSO 轮换密钥后，新 kid 的令牌应通过（未知 kid 会强制刷新一次 JWKS）。"""
    new_key = generate_key()
    jwks.rotate(new_key, "rotated-kid")
    headers = bearer(make_token(key=new_key, kid="rotated-kid", scope="openid files:read"))
    response = client.get("/v1/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["sub"] == "user_0001"


def test_missing_read_scope_is_403(client, auth):
    headers = auth(scope="openid profile")
    response = client.get("/v1/files", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"
    assert "files:read" in response.headers["WWW-Authenticate"]


def test_missing_write_scope_is_403(client, upload, auth):
    headers = auth(scope="openid files:read")
    response = upload(client, headers, content=b"x")
    assert response.status_code == 403


def test_write_only_token_can_upload_but_not_read(client, upload, auth, token):
    write_only = bearer(make_token(scope="openid files:write", sub="user_writer"))
    created = upload(client, write_only, content=b"payload")
    assert created.status_code == 201
    file_id = created.json()["id"]

    # 同一个用户：上传可以，下载需要 files:read
    denied = client.get(f"/v1/files/{file_id}/content", headers=write_only)
    assert denied.status_code == 403


def test_deny_audit_written_for_bad_token(client, db_rows):
    client.get("/v1/files", headers=bearer("garbage"))
    audits = db_rows["audits"](action="deny")
    assert audits, "认证失败也应留审计"


def test_token_never_appears_in_logs(client, caplog, upload, auth):
    import logging

    headers = auth()
    raw = headers["Authorization"].split(" ", 1)[1]
    with caplog.at_level(logging.INFO, logger="file"):
        response = upload(client, headers, content=b"secret-bytes")
    assert response.status_code == 201
    assert raw not in caplog.text


def test_me_returns_identity(client, auth):
    response = client.get("/v1/me", headers=auth(sub="user_me"))
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "user_me"
    assert body["issuer"] == "https://sso.test/auth"
    assert body["audience"] == "elicloud-services"
    assert "files:read" in body["scope"]


def test_scope_check_can_be_disabled_for_local_debug(client_factory, upload):
    debug_client = client_factory(REQUIRED_READ_SCOPE="", REQUIRED_WRITE_SCOPE="")
    headers = bearer(make_token(scope="openid"))
    response = upload(debug_client, headers, content=b"debug")
    assert response.status_code == 201


@pytest.mark.parametrize("path", ["/v1/files", "/v1/me"])
def test_all_v1_endpoints_require_token(client, path):
    assert client.get(path).status_code == 401
