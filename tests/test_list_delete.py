"""列表、删除与配额汇总。"""

from __future__ import annotations

from pathlib import Path


def _file(upload, client, headers, content: bytes = b"x") -> str:
    response = upload(client, headers, content=content)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_list_only_returns_own_files(client, auth, upload):
    a = auth(sub="user_a")
    b = auth(sub="user_b")
    a1 = _file(upload, client, a, b"a1")
    a2 = _file(upload, client, a, b"a2")
    b1 = _file(upload, client, b, b"b1")

    body = client.get("/v1/files", headers=a).json()
    assert body["total"] == 2
    assert {item["id"] for item in body["files"]} == {a1, a2}
    assert b1 not in {item["id"] for item in body["files"]}


def test_list_pagination(client, auth, upload):
    headers = auth(sub="user_page")
    for index in range(5):
        _file(upload, client, headers, f"file-{index}".encode())

    first = client.get("/v1/files?limit=2&offset=0", headers=headers).json()
    second = client.get("/v1/files?limit=2&offset=2", headers=headers).json()
    assert first["total"] == 5
    assert len(first["files"]) == 2
    assert len(second["files"]) == 2
    assert {item["id"] for item in first["files"]}.isdisjoint({item["id"] for item in second["files"]})


def test_list_status_filters(client, auth, upload, expire_file):
    headers = auth(sub="user_status")
    active_id = _file(upload, client, headers, b"active")
    expired_id = _file(upload, client, headers, b"expired")
    deleted_id = _file(upload, client, headers, b"deleted")
    expire_file(expired_id)
    client.delete(f"/v1/files/{deleted_id}", headers=headers)

    active = client.get("/v1/files?status=active", headers=headers).json()
    assert [item["id"] for item in active["files"]] == [active_id]

    expired = client.get("/v1/files?status=expired", headers=headers).json()
    assert [item["id"] for item in expired["files"]] == [expired_id]
    assert expired["files"][0]["status"] == "expired"

    deleted = client.get("/v1/files?status=deleted", headers=headers).json()
    assert [item["id"] for item in deleted["files"]] == [deleted_id]

    everything = client.get("/v1/files?status=all", headers=headers).json()
    assert everything["total"] == 3


def test_list_rejects_unknown_status(client, auth):
    response = client.get("/v1/files?status=whatever", headers=auth())
    assert response.status_code == 400


def test_list_validates_limit(client, auth):
    assert client.get("/v1/files?limit=0", headers=auth()).status_code == 400
    assert client.get("/v1/files?limit=999", headers=auth()).status_code == 400


def test_list_includes_quota(client, auth, upload):
    headers = auth(sub="user_quota_list")
    _file(upload, client, headers, b"1234")
    body = client.get("/v1/files", headers=headers).json()
    quota = body["quota"]
    assert quota["files"] == 1
    assert quota["bytes"] == 4
    assert quota["max_files"] == 200
    assert quota["max_bytes"] == 1048576


def test_delete_removes_object_and_marks_deleted(client, auth, upload):
    headers = auth(sub="user_del")
    file_id = _file(upload, client, headers, b"to-remove")

    settings = client.app.state.settings
    key = client.app.state.storages.primary.key_for(file_id)
    path = Path(settings.local_storage_root) / key
    assert path.exists()

    response = client.delete(f"/v1/files/{file_id}", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"id": file_id, "status": "deleted", "changed": True}
    assert not path.exists()

    assert client.get(f"/v1/files/{file_id}", headers=headers).status_code == 404
    assert client.get(f"/v1/files/{file_id}/content", headers=headers).status_code == 404


def test_delete_is_idempotent(client, auth, upload):
    headers = auth(sub="user_del2")
    file_id = _file(upload, client, headers, b"once")
    assert client.delete(f"/v1/files/{file_id}", headers=headers).json()["changed"] is True
    second = client.delete(f"/v1/files/{file_id}", headers=headers)
    assert second.status_code == 200
    assert second.json()["changed"] is False


def test_delete_other_users_file_is_404(client, auth, upload):
    owner = auth(sub="user_owner")
    file_id = _file(upload, client, owner, b"mine")
    response = client.delete(f"/v1/files/{file_id}", headers=auth(sub="user_other"))
    assert response.status_code == 404


def test_delete_expired_is_410(client, auth, upload, expire_file):
    headers = auth(sub="user_exp")
    file_id = _file(upload, client, headers, b"old")
    expire_file(file_id)
    response = client.delete(f"/v1/files/{file_id}", headers=headers)
    assert response.status_code == 410


def test_delete_requires_write_scope(client, auth, upload, token):
    from tests.helpers import bearer

    headers = auth(sub="user_scope")
    file_id = _file(upload, client, headers, b"x")
    read_only = bearer(token(scope="openid files:read", sub="user_scope"))
    assert client.delete(f"/v1/files/{file_id}", headers=read_only).status_code == 403


def test_grant_count_in_detail_and_list(client, auth, upload):
    headers = auth(sub="user_g")
    response = upload(client, headers, content=b"g", visibility="protected")
    file_id = response.json()["id"]
    client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_x"}, headers=headers)
    client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_y"}, headers=headers)

    detail = client.get(f"/v1/files/{file_id}", headers=headers).json()
    assert detail["grant_count"] == 2

    listing = client.get("/v1/files", headers=headers).json()
    assert listing["files"][0]["grant_count"] == 2


def test_detail_requires_read_scope(client, auth, upload, token):
    from tests.helpers import bearer

    headers = auth(sub="user_d")
    file_id = _file(upload, client, headers, b"x")
    write_only = bearer(token(scope="openid files:write", sub="user_d"))
    assert client.get(f"/v1/files/{file_id}", headers=write_only).status_code == 403
