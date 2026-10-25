"""测试环境准备。

关键点（与 `sso/tests/conftest.py`、`mc/tests/conftest.py` 同一手法）：环境变量必须在
**任何** app 模块被导入之前写入（`config` 在实例化 Settings 时读环境），所以这里在
conftest 顶层就：

1. 起一个**假 JWKS 服务**（真实 HTTP + 真实 RS256 验签路径）；
2. 把地址与本地存储/数据库路径写进环境变量；
3. 之后各用例用 `app.main.create_app()` 构造测试应用。

全程不依赖真实 SSO、不访问真实 COS。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fake_jwks import FakeJwksServer  # noqa: E402
from tests.fake_storage import MemoryBlobStore  # noqa: E402
from tests.helpers import (  # noqa: E402
    TEST_AUDIENCE,
    TEST_ISSUER,
    TEST_KID,
    TEST_KEY,
    TEST_OTHER_KEY,
    TEST_READ_SCOPE,
    TEST_SCOPE,
    TEST_WRITE_SCOPE,
    bearer,
    make_token,
)

TEST_DIR = Path(tempfile.mkdtemp(prefix="file-tests-"))
BLOB_ROOT = TEST_DIR / "blobs"
TEMP_ROOT = TEST_DIR / "tmp"

_JWKS = FakeJwksServer(TEST_KEY, TEST_KID, max_age=300).start()

os.environ.update(
    {
        "SSO_ISSUER": TEST_ISSUER,
        "SSO_JWKS_URL": _JWKS.url,
        "JWT_AUDIENCE": TEST_AUDIENCE,
        "REQUIRED_READ_SCOPE": TEST_READ_SCOPE,
        "REQUIRED_WRITE_SCOPE": TEST_WRITE_SCOPE,
        "DATABASE_URL": f"sqlite:///{(TEST_DIR / 'file-test.db').as_posix()}",
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_ROOT": BLOB_ROOT.as_posix(),
        "TEMP_ROOT": TEMP_ROOT.as_posix(),
        # 测试用的“小上限”，让 413/配额用例不必真的造 25 MiB
        "MAX_FILE_BYTES": "65536",
        "DEFAULT_TTL_SECONDS": "3600",
        "MAX_TTL_SECONDS": "86400",
        "DEFAULT_VISIBILITY": "private",
        "QUOTA_MAX_FILES": "200",
        "QUOTA_MAX_BYTES": "1048576",
        "MAX_TOTAL_BYTES": "8388608",
        "MIN_FREE_BYTES": "1",
        # 限流单独测（用 client_factory 调低阈值），别干扰其它用例
        "UPLOAD_ATTEMPTS_PER_WINDOW": "100000",
        "UPLOAD_WINDOW_SECONDS": "600",
        "CLEANUP_ENABLED": "false",
        "LOG_LEVEL": "warning",
    }
)


# --------------------------------------------------------------- 基础设施


@pytest.fixture(scope="session", autouse=True)
def _jwks_server():
    yield
    # 先释放 SQLite 引擎，否则 Windows 上文件仍被占用，临时目录删不干净
    from app.db import reset_db_state

    reset_db_state()
    _JWKS.stop()
    shutil.rmtree(TEST_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def jwks() -> FakeJwksServer:
    return _JWKS


@pytest.fixture(scope="session")
def signing_key():
    return TEST_KEY


@pytest.fixture(scope="session")
def other_signing_key():
    return TEST_OTHER_KEY


@pytest.fixture(scope="session")
def constants() -> dict[str, str]:
    return {
        "issuer": TEST_ISSUER,
        "audience": TEST_AUDIENCE,
        "kid": TEST_KID,
        "read_scope": TEST_READ_SCOPE,
        "write_scope": TEST_WRITE_SCOPE,
    }


# ------------------------------------------------------------------- 应用


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def client_factory():
    """构造「改了某几项配置」的应用实例（依赖覆盖，避免动全局 lru_cache）。"""
    from fastapi.testclient import TestClient

    from app.config import Settings, get_settings
    from app.main import create_app

    created = []

    def _make(*, storage=None, **overrides) -> TestClient:
        settings = Settings(**overrides)
        app = create_app(settings, storage=storage)
        app.dependency_overrides[get_settings] = lambda: settings
        test_client = TestClient(app)
        test_client.__enter__()
        created.append(test_client)
        return test_client

    yield _make
    for test_client in created:
        test_client.__exit__(None, None, None)


# ------------------------------------------------------------------ 令牌


@pytest.fixture
def token():
    def _make(**kwargs) -> str:
        return make_token(**kwargs)

    return _make


@pytest.fixture
def auth():
    def _make(sub: str = "user_0001", scope: str | None = TEST_SCOPE, **kwargs) -> dict[str, str]:
        return bearer(make_token(sub=sub, scope=scope, **kwargs))

    return _make


@pytest.fixture
def read_auth():
    def _make(sub: str = "user_0001", **kwargs) -> dict[str, str]:
        return bearer(make_token(sub=sub, scope=f"openid {TEST_READ_SCOPE}", **kwargs))

    return _make


# ------------------------------------------------------------------ 上传


@pytest.fixture
def upload():
    def _upload(
        client,
        headers,
        *,
        content: bytes = b"hello elicloud",
        filename: str = "hello.txt",
        content_type: str = "text/plain",
        ttl_seconds: int | str | None = None,
        visibility: str | None = None,
    ):
        data: dict[str, str] = {}
        if ttl_seconds is not None:
            data["ttl_seconds"] = str(ttl_seconds)
        if visibility is not None:
            data["visibility"] = visibility
        return client.post(
            "/v1/files",
            files={"file": (filename, content, content_type)},
            data=data,
            headers=headers,
        )

    return _upload


# --------------------------------------------------------------- 隔离与工具


@pytest.fixture(autouse=True)
def _clean_state():
    """数据库、限流、本地存储目录都是进程内状态，用例之间必须隔离。"""
    from sqlalchemy import delete

    from app.config import get_settings
    from app.db import init_db, session_scope
    from app.errors import limiter
    from app.models import AuditLog, FileGrant, FileRecord

    init_db(get_settings())
    with session_scope() as session:
        session.execute(delete(FileGrant))
        session.execute(delete(FileRecord))
        session.execute(delete(AuditLog))
        session.commit()

    limiter.clear()
    for root in (BLOB_ROOT, TEMP_ROOT):
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
    yield
    limiter.clear()


@pytest.fixture
def db_rows():
    """直接读库断言（审计、文件行、授权）。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import AuditLog, FileGrant, FileRecord

    def _rows(model, **filters):
        with session_scope() as session:
            statement = select(model)
            for column, value in filters.items():
                statement = statement.where(getattr(model, column) == value)
            return list(session.scalars(statement).all())

    return {
        "files": lambda **filters: _rows(FileRecord, **filters),
        "grants": lambda **filters: _rows(FileGrant, **filters),
        "audits": lambda **filters: _rows(AuditLog, **filters),
    }


@pytest.fixture
def memory_store():
    return MemoryBlobStore()


@pytest.fixture
def expire_file():
    """把某个文件的 expires_at 改到过去（模拟「已过期但清理器还没扫到」）。"""
    from datetime import timedelta

    from app.db import session_scope
    from app.models import FileRecord, to_iso, utcnow

    def _expire(file_id: str, *, seconds_ago: int = 10) -> None:
        with session_scope() as session:
            record = session.get(FileRecord, file_id)
            assert record is not None
            record.expires_at = to_iso(utcnow() - timedelta(seconds=seconds_ago))
            session.commit()

    return _expire
