"""运维命令行（容器内执行）：

    docker compose exec file python -m app.cli show-config
    docker compose exec file python -m app.cli stats
    docker compose exec file python -m app.cli sweep [--orphans] [--dry-run]
    docker compose exec file python -m app.cli check-storage [--write-test]

与 HTTP 侧走**同一套**业务函数（`files_service`），不存在两套逻辑。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import secrets
import sys
from typing import Any

from .config import get_settings
from .db import init_db, session_scope
from .files_service import purge_orphans, stats_snapshot, sweep_once
from .storage import StorageRegistry
from .storage.base import StorageError


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _probe_key(backend: Any) -> str:
    prefix = str(getattr(backend, "objects_prefix", "") or "").strip("/")
    suffix = f"_probe/{secrets.token_hex(8)}"
    return f"{prefix}/{suffix}" if prefix else suffix


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="file 服务运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show-config", help="打印生效配置（不含任何密钥）")
    sub.add_parser("stats", help="统计文件数/容量/授权数")

    sweep = sub.add_parser("sweep", help="手动跑一轮清理")
    sweep.add_argument("--orphans", action="store_true", help="额外扫描并删除孤儿对象（COS 会有列举开销）")
    sweep.add_argument("--dry-run", action="store_true", help="只统计，不真的删除孤儿")

    check = sub.add_parser("check-storage", help="探活存储后端")
    check.add_argument("--write-test", action="store_true", help="额外做一次 put/head/get/delete 探针")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()

    if args.command == "show-config":
        _print(settings.log_summary())
        return 0

    storages = StorageRegistry(settings)

    if args.command == "stats":
        init_db(settings)
        with session_scope() as session:
            _print(stats_snapshot(session, settings))
        return 0

    if args.command == "sweep":
        init_db(settings)
        with session_scope() as session:
            try:
                report: dict[str, Any] = {"sweep": sweep_once(session, settings, storages)}
                if args.orphans:
                    report["orphans"] = purge_orphans(
                        session, settings, storages, dry_run=args.dry_run
                    )
            except Exception as exc:  # noqa: BLE001 - CLI 要把错误打给人看
                print(f"清理失败：{exc}", file=sys.stderr)
                return 1
            _print(report)
        return 0

    if args.command == "check-storage":
        backend = storages.primary
        try:
            health = backend.health()
        except Exception as exc:  # noqa: BLE001
            print(f"存储探活失败：{exc}", file=sys.stderr)
            return 1
        report: dict[str, Any] = {"health": health, "backend": backend.name}
        if args.write_test:
            key = _probe_key(backend)
            body = b"elicloud-file storage probe\n"
            digest = hashlib.sha256(body).hexdigest()
            try:
                backend.write(
                    io.BytesIO(body),
                    key,
                    content_type="text/plain",
                    filename="probe.txt",
                    sha256=digest,
                )
                head = backend.head(key)
                opened = backend.open_read(key)
                echo = b"".join(opened.chunks)
                backend.delete(key)
            except StorageError as exc:
                print(f"存储写探针失败：{exc}", file=sys.stderr)
                return 1
            report["probe"] = {
                "written": True,
                "head_size": None if head is None else head.size,
                "roundtrip_ok": echo == body,
                "sha256_ok": hashlib.sha256(echo).hexdigest() == digest,
            }
            if not report["probe"]["roundtrip_ok"]:
                print("存储写探针失败：回读内容不一致", file=sys.stderr)
                return 1
        _print(report)
        return 0

    print(f"未知命令：{args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
