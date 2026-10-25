"""数据库连接、会话与建表。

起步与 SSO / mc 一致：SQLite + 挂卷持久化，表结构保持可迁移（换 PostgreSQL 只改 DATABASE_URL）。
迁移策略同样是「不引入 Alembic」：``CREATE TABLE IF NOT EXISTS`` + 启动时的幂等补列。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

# 需要按需补到既有表上的列（表名 -> 列名集合）。加列时写在这里即可，重启任意次都安全。
_ADDED_COLUMNS: dict[str, tuple[str, ...]] = {}


def _sqlite_path(database_url: str) -> str | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return database_url[len(prefix) :]


def _add_missing_columns(engine: Engine) -> None:
    """给既有表补新列；先查再改，因此幂等。"""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, wanted in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in wanted:
            if column in existing:
                continue
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} TEXT"))
            logger.info("迁移：%s 增加列 %s", table, column)


def init_db(settings: Settings) -> Engine:
    """建引擎、补列、建表（含索引）。可重复调用。"""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    url = settings.database_url
    is_sqlite = url.startswith("sqlite")
    connect_args: dict[str, object] = {"check_same_thread": False} if is_sqlite else {}

    sqlite_path = _sqlite_path(url)
    if sqlite_path:
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, connect_args=connect_args, future=True)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    _add_missing_columns(engine)
    Base.metadata.create_all(engine)

    _engine = engine
    _session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    return engine


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    if _session_factory is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db()")
    session = _session_factory()
    try:
        yield session
    finally:
        session.close()


def get_session_factory() -> sessionmaker[Session]:
    """给后台 worker / CLI 用（它们自己管理会话生命周期）。"""
    if _session_factory is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db()")
    return _session_factory


def session_scope() -> Session:
    """给 CLI / 测试用的裸会话（调用方负责 commit/close）。"""
    return get_session_factory()()


def reset_db_state() -> None:
    """测试用：丢弃引擎与会话工厂。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
