"""健康检查：无令牌、DB/存储判活、容量告警。"""

from __future__ import annotations

from app.storage.base import StorageUnavailable
from tests.fake_storage import MemoryBlobStore


def test_healthz_reports_checks(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["download_mode"] == "proxy"
    checks = body["checks"]
    assert checks["database"]["ok"] is True
    assert checks["storage"]["ok"] is True
    assert checks["storage"]["backend"] == "local"
    assert checks["quota"]["limit_bytes"] == 8388608
    assert checks["quota"]["percent"] == 0.0


def test_healthz_fails_when_storage_fails(client_factory):
    store = MemoryBlobStore()

    def broken_health():
        raise StorageUnavailable("simulated storage outage")

    store.health = broken_health  # type: ignore[method-assign]
    broken = client_factory(storage=store)

    response = broken.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert body["checks"]["storage"]["ok"] is False
    assert body["checks"]["database"]["ok"] is True


def test_healthz_quota_warning(client_factory, upload, auth):
    small = client_factory(
        MAX_FILE_BYTES=8,
        QUOTA_MAX_BYTES=8,
        MAX_TOTAL_BYTES=8,
        STORAGE_WARN_PERCENT=50,
    )
    assert upload(small, auth(sub="user_h"), content=b"x" * 8).status_code == 201

    body = small.get("/healthz").json()
    quota = body["checks"]["quota"]
    assert quota["used_bytes"] == 8
    assert quota["percent"] == 100.0
    assert quota["warning"] is True
    assert body["status"] == "ok"


def test_healthz_reports_capacity_expiry(client):
    quota = client.get("/healthz").json()["checks"]["quota"]
    assert quota["capacity_expires_at"] == "2027-08-11"
    assert isinstance(quota["capacity_expires_in_days"], int)


def test_healthz_does_not_leak_paths_or_secrets(client):
    text = client.get("/healthz").text
    settings = client.app.state.settings
    assert settings.local_storage_root not in text
    assert "COS_SECRET" not in text
    assert "secret_key" not in text
