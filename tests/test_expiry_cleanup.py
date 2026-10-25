"""清理器：过期、删对象、暂存清理、墓碑硬删、孤儿清理、幂等。"""

from __future__ import annotations

import os
import threading
import time
from datetime import timedelta
from pathlib import Path

from app.db import session_scope
from app.files_service import cleanup_temp_files, purge_orphans, purge_tombstones, sweep_once
from app.models import FileGrant, FileRecord, to_iso, utcnow
from app.worker import run_cleanup


def _sweep(client, settings=None) -> dict[str, int]:
    settings = settings or client.app.state.settings
    storages = client.app.state.storages
    with session_scope() as session:
        return sweep_once(session, settings, storages)


def _file(upload, client, headers, content: bytes = b"x") -> str:
    response = upload(client, headers, content=content)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_sweep_expires_due_file_and_deletes_object(client, auth, upload, expire_file, db_rows):
    headers = auth(sub="user_sweep")
    file_id = _file(upload, client, headers, b"expire-me")
    key = client.app.state.storages.primary.key_for(file_id)
    path = Path(client.app.state.settings.local_storage_root) / key
    assert path.exists()

    expire_file(file_id)
    counts = _sweep(client)
    assert counts["expired"] == 1
    assert not path.exists()

    rows = db_rows["files"](id=file_id)
    assert rows[0].status == "expired"
    assert rows[0].expired_at is not None
    assert db_rows["audits"](action="expire", target=file_id)


def test_sweep_keeps_active_files(client, auth, upload):
    headers = auth(sub="user_active")
    file_id = _file(upload, client, headers, b"keep")
    counts = _sweep(client)
    assert counts["expired"] == 0
    assert client.get(f"/v1/files/{file_id}/content", headers=headers).status_code == 200


def test_sweep_never_expires_permanent_public_file(client, auth, upload):
    headers = auth(sub="user_permanent")
    response = upload(client, headers, content=b"forever", ttl_seconds=0, visibility="public")
    assert response.status_code == 201
    file_id = response.json()["id"]
    counts = _sweep(client)
    assert counts["expired"] == 0
    assert client.get(f"/v1/files/{file_id}/content", headers=headers).status_code == 200
    assert client.get(f"/v1/files/{file_id}", headers=headers).json()["permanent"] is True


def test_sweep_is_idempotent(client, auth, upload, expire_file):
    headers = auth(sub="user_idem")
    file_id = _file(upload, client, headers, b"once")
    expire_file(file_id)
    assert _sweep(client)["expired"] == 1
    assert _sweep(client)["expired"] == 0


def test_sweep_respects_batch_size(client, auth, upload, expire_file):
    headers = auth(sub="user_batch")
    ids = [_file(upload, client, headers, f"f{i}".encode()) for i in range(3)]
    for file_id in ids:
        expire_file(file_id)

    settings = client.app.state.settings.model_copy(update={"cleanup_batch_size": 2})
    assert _sweep(client, settings)["expired"] == 2
    assert _sweep(client, settings)["expired"] == 1


def test_purge_tombstones_after_retention(client, auth, upload, expire_file, db_rows):
    headers = auth(sub="user_tomb")
    file_id = _file(upload, client, headers, b"tomb")
    client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_g"}, headers=headers)
    expire_file(file_id)
    _sweep(client)

    old = to_iso(utcnow() - timedelta(days=5))
    with session_scope() as session:
        session.get(FileRecord, file_id).expired_at = old
        session.commit()

    settings = client.app.state.settings.model_copy(update={"tombstone_retention_days": 1})
    with session_scope() as session:
        removed = purge_tombstones(session, settings)
    assert removed == 1
    assert db_rows["files"](id=file_id) == []
    assert db_rows["grants"](file_id=file_id) == []
    # 审计保留
    assert db_rows["audits"](action="expire", target=file_id)


def test_purge_tombstones_keeps_recent(client, auth, upload):
    headers = auth(sub="user_recent")
    file_id = _file(upload, client, headers, b"recent")
    assert client.delete(f"/v1/files/{file_id}", headers=headers).status_code == 200

    settings = client.app.state.settings.model_copy(update={"tombstone_retention_days": 30})
    with session_scope() as session:
        assert purge_tombstones(session, settings) == 0


def test_cleanup_temp_files(client):
    settings = client.app.state.settings
    temp_root = Path(settings.temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    old = temp_root / "tmp_old"
    fresh = temp_root / "tmp_fresh"
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    past = time.time() - settings.temp_file_max_age_seconds - 60
    os.utime(old, (past, past))

    assert cleanup_temp_files(settings) == 1
    assert not old.exists()
    assert fresh.exists()


def test_purge_orphans_deletes_only_old_objects(client):
    settings = client.app.state.settings
    storages = client.app.state.storages
    root = Path(settings.local_storage_root) / "blobs" / "zz"
    root.mkdir(parents=True, exist_ok=True)

    old = root / ("f_" + "O" * 22)
    young = root / ("f_" + "N" * 22)
    old.write_bytes(b"orphan")
    young.write_bytes(b"young")
    past = time.time() - settings.temp_file_max_age_seconds - 60
    os.utime(old, (past, past))

    with session_scope() as session:
        dry = purge_orphans(session, settings, storages, dry_run=True)
    assert dry["deleted"] == 1
    assert old.exists(), "dry-run 不能真的删"

    with session_scope() as session:
        result = purge_orphans(session, settings, storages, dry_run=False)
    assert result["scanned"] == 2
    assert result["deleted"] == 1
    assert result["kept"] == 1
    assert not old.exists()
    assert young.exists()


def test_sweep_reports_temp_and_orphans(client):
    settings = client.app.state.settings
    temp_root = Path(settings.temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    stale = temp_root / "tmp_stale"
    stale.write_bytes(b"x")
    past = time.time() - settings.temp_file_max_age_seconds - 60
    os.utime(stale, (past, past))

    counts = _sweep(client)
    assert counts["temp_files"] == 1
    assert not stale.exists()


def test_worker_stops_on_event(client):
    settings = client.app.state.settings.model_copy(update={"cleanup_interval_seconds": 1})
    storages = client.app.state.storages
    stop_event = threading.Event()
    stop_event.set()
    thread = threading.Thread(target=run_cleanup, args=(settings, storages, stop_event))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_grant_rows_disappear_with_tombstone(client, auth, upload, expire_file):
    headers = auth(sub="user_cascade")
    file_id = _file(upload, client, headers, b"cascade")
    client.post(f"/v1/files/{file_id}/grants", json={"grantee_sub": "user_g"}, headers=headers)
    expire_file(file_id)
    _sweep(client)

    with session_scope() as session:
        session.get(FileRecord, file_id).expired_at = to_iso(utcnow() - timedelta(days=90))
        session.commit()
    with session_scope() as session:
        purge_tombstones(session, client.app.state.settings)

    with session_scope() as session:
        from sqlalchemy import func, select

        assert session.scalar(select(func.count()).select_from(FileGrant).where(FileGrant.file_id == file_id)) == 0
