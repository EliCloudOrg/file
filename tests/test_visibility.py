"""可见性矩阵与授权：越权一律 404，不泄露文件是否存在。"""

from __future__ import annotations

import pytest


def _create(upload, client, owner_headers, *, visibility: str, content: bytes = b"data"):
    response = upload(client, owner_headers, content=content, visibility=visibility)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_private_is_owner_only(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="private")

    assert client.get(f"/v1/files/{file_id}", headers=owner).status_code == 200
    assert client.get(f"/v1/files/{file_id}", headers=auth(sub="user_other")).status_code == 404
    assert client.get(f"/v1/files/{file_id}/content", headers=auth(sub="user_other")).status_code == 404


def test_protected_requires_grant(client, auth, upload):
    owner = auth(sub="user_owner")
    friend = auth(sub="user_friend")
    stranger = auth(sub="user_stranger")
    file_id = _create(upload, client, owner, visibility="protected")

    assert client.get(f"/v1/files/{file_id}", headers=friend).status_code == 404
    assert client.get(f"/v1/files/{file_id}", headers=stranger).status_code == 404

    granted = client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_friend"}, headers=owner)
    assert granted.status_code == 201

    assert client.get(f"/v1/files/{file_id}", headers=friend).status_code == 200
    assert client.get(f"/v1/files/{file_id}/content", headers=friend).content == b"data"
    assert client.get(f"/v1/files/{file_id}", headers=stranger).status_code == 404

    revoked = client.delete(f"/v1/files/{file_id}/grants/user_friend", headers=owner)
    assert revoked.status_code == 200
    assert revoked.json()["removed"] is True

    assert client.get(f"/v1/files/{file_id}", headers=friend).status_code == 404


def test_public_is_readable_by_any_authenticated_user(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="public", content=b"public-bytes")

    stranger = auth(sub="user_stranger")
    response = client.get(f"/v1/files/{file_id}/content", headers=stranger)
    assert response.status_code == 200
    assert response.content == b"public-bytes"


def test_public_still_requires_token(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="public")
    assert client.get(f"/v1/files/{file_id}/content").status_code == 401


def test_public_still_requires_read_scope(client, auth, upload, token):
    from tests.helpers import bearer

    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="public")
    no_scope = bearer(token(scope="openid profile", sub="user_noscope"))
    assert client.get(f"/v1/files/{file_id}/content", headers=no_scope).status_code == 403


def test_public_expired_is_410_for_everyone(client, auth, upload, expire_file):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="public")
    expire_file(file_id)

    assert client.get(f"/v1/files/{file_id}", headers=owner).status_code == 410
    assert client.get(f"/v1/files/{file_id}", headers=auth(sub="user_stranger")).status_code == 410


def test_protected_expired_without_grant_is_404(client, auth, upload, expire_file):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="protected")
    expire_file(file_id)
    # 没有授权的局外人连「已过期」都不该知道
    assert client.get(f"/v1/files/{file_id}", headers=auth(sub="user_stranger")).status_code == 404


def test_patch_visibility_by_owner(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="private")

    response = client.patch(f"/v1/files/{file_id}", json={"visibility": "public"}, headers=owner)
    assert response.status_code == 200
    assert response.json()["visibility"] == "public"
    assert client.get(f"/v1/files/{file_id}/content", headers=auth(sub="user_stranger")).status_code == 200


def test_patch_visibility_rejects_invalid_value(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="private")
    response = client.patch(f"/v1/files/{file_id}", json={"visibility": "world-readable"}, headers=owner)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_patch_visibility_unknown_field_is_rejected(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="private")
    response = client.patch(
        f"/v1/files/{file_id}", json={"visibility": "public", "ttl_seconds": 1}, headers=owner
    )
    assert response.status_code == 400


def test_patch_visibility_by_other_user_is_404(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="private")
    response = client.patch(f"/v1/files/{file_id}", json={"visibility": "public"}, headers=auth(sub="user_other"))
    assert response.status_code == 404


def test_private_to_protected_keeps_and_restores_grant(client, auth, upload):
    owner = auth(sub="user_owner")
    friend = auth(sub="user_friend")
    file_id = _create(upload, client, owner, visibility="protected")
    client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_friend"}, headers=owner)
    assert client.get(f"/v1/files/{file_id}", headers=friend).status_code == 200

    client.patch(f"/v1/files/{file_id}", json={"visibility": "private"}, headers=owner)
    assert client.get(f"/v1/files/{file_id}", headers=friend).status_code == 404

    client.patch(f"/v1/files/{file_id}", json={"visibility": "protected"}, headers=owner)
    assert client.get(f"/v1/files/{file_id}", headers=friend).status_code == 200


@pytest.mark.parametrize("visibility", ["private", "protected", "public"])
def test_owner_always_reads_own_file(client, auth, upload, visibility):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility=visibility)
    assert client.get(f"/v1/files/{file_id}/content", headers=owner).status_code == 200


def test_deny_audit_written_for_unauthorized_read(client, auth, upload, db_rows):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="private")
    client.get(f"/v1/files/{file_id}/content", headers=auth(sub="user_other"))

    audits = [row for row in db_rows["audits"](action="deny") if row.target == file_id]
    assert audits, "越权访问必须留痕"


# ------------------------------------------------------------------- 授权管理


def test_grant_to_self_is_400(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="protected")
    response = client.post(
        f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_owner"}, headers=owner
    )
    assert response.status_code == 400


def test_grant_is_idempotent(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="protected")
    first = client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_friend"}, headers=owner)
    second = client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_friend"}, headers=owner)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["created"] is False


def test_grant_limit(client_factory, upload, auth):
    owner = auth(sub="user_owner")
    limited = client_factory(MAX_GRANTS_PER_FILE=1)
    file_id = _create(upload, limited, owner, visibility="protected")

    assert (
        limited.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_a"}, headers=owner).status_code
        == 201
    )
    overflow = limited.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_b"}, headers=owner)
    assert overflow.status_code == 409
    assert overflow.json()["error"] == "grant_limit_exceeded"


def test_grant_requires_owner(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="protected")
    response = client.post(
        f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_friend"}, headers=auth(sub="user_other")
    )
    assert response.status_code == 404


def test_list_grants(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="protected")
    client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_b"}, headers=owner)
    client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_a"}, headers=owner)

    response = client.get(f"/v1/files/{file_id}/grants", headers=owner)
    assert response.status_code == 200
    body = response.json()
    assert body["file_id"] == file_id
    assert body["visibility"] == "protected"
    assert [item["grantee_sub"] for item in body["grants"]] == sorted(["user_b", "user_a"])
    assert all(item["created_at"].endswith("Z") for item in body["grants"])


def test_invalid_grantee_sub_is_400(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="protected")
    response = client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "bad sub!"}, headers=owner)
    assert response.status_code == 400


def test_revoke_missing_grant_is_idempotent(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _create(upload, client, owner, visibility="protected")
    response = client.delete(f"/v1/files/{file_id}/grants/user_ghost", headers=owner)
    assert response.status_code == 200
    assert response.json()["removed"] is False
