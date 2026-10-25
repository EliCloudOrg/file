"""全部配置从环境变量读取（风格与 `sso/app/config.py`、`mc/app/config.py` 一致）。

三处归属（deploy-framework.md §2.4）：

* 运行时变量 → 服务器 `/srv/file/app.env`（600），由 `deploy.sh` 读出后注入容器；
* 可版本化的默认值 → 本文件与 `docker-compose.yml`；
* **COS 凭据只从环境变量来，没有默认值** —— ``STORAGE_BACKEND=cos`` 时缺了就直接
  启动失败（fail closed）。绝不允许「忘了配密钥却把文件写进容器本地磁盘」。
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import VISIBILITIES

_BACKENDS = ("local", "cos")
_DOWNLOAD_MODES = ("proxy", "presign", "both")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # ---- SSO 接入（architecture.md §8）--------------------------------------
    # 必须与 SSO 的 PUBLIC_BASE_URL 逐字一致，否则所有令牌都验签失败。
    sso_issuer: str = "https://146.56.237.33/auth"
    sso_jwks_url: str = "https://146.56.237.33/auth/.well-known/jwks.json"
    jwt_audience: str = "elicloud-services"
    # 空串 = 关闭该校验（只允许本地调试；启动时打 WARNING）
    required_read_scope: str = "files:read"
    required_write_scope: str = "files:write"
    jwks_cache_seconds: int = 300
    jwt_leeway_seconds: int = 30  # exp/nbf 的时钟偏移容忍
    jwks_timeout_seconds: float = 5.0

    # ---- 元数据存储 ---------------------------------------------------------
    # 只存元数据，文件内容在对象存储（或本地磁盘）。SQLite + 挂卷持久化。
    database_url: str = "sqlite:////data/file.db"

    # ---- 内容存储 -----------------------------------------------------------
    # local：本地磁盘（测试/开发）；cos：腾讯云 COS（生产，必须显式配置凭据）
    storage_backend: str = "local"
    local_storage_root: str = "/data/blobs"
    # 上传暂存目录：必须落在真实磁盘上（COS 后端同样需要它来算 sha256）
    temp_root: str = "/data/tmp"

    cos_region: str = "ap-nanjing"
    cos_bucket: str = "elicoudcos-1465229333"
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    # 对象 key：<prefix>/blobs/<id 第 3-4 位>/<id>，前缀用来和其它系统隔离
    cos_key_prefix: str = "elicloud-file"
    cos_endpoint: str = ""  # 留空 = cos.<region>.myqcloud.com（同地域走内网）
    cos_scheme: str = "https"
    cos_timeout_seconds: float = 10.0
    cos_max_retries: int = 3

    # ---- 下载链路 -----------------------------------------------------------
    # proxy：一律由本服务流式转发；presign：默认 302 到预签名 URL；
    # both：默认代理，带 ?direct=1 时 302。local 后端没有预签名能力，会自动回落代理。
    download_mode: str = "proxy"
    presign_expires_seconds: int = 300

    # ---- 业务 ---------------------------------------------------------------
    max_file_bytes: int = 26214400  # 25 MiB
    default_ttl_seconds: int = 604800  # 7 天
    max_ttl_seconds: int = 2592000  # 30 天
    default_visibility: str = "private"
    max_grants_per_file: int = 100

    # 每用户配额
    quota_max_files: int = 200
    quota_max_bytes: int = 1073741824  # 1 GiB
    # 服务总容量（150 GiB 桶留余量：扣掉对象存储自身开销与包到期后的降档）
    max_total_bytes: int = 96636764160  # 90 GiB
    storage_warn_percent: int = 80
    # 容量包到期日（2027-08-11 的 50 GiB 先过期）；留空 = 不做到期提醒
    capacity_expires_at: str = "2027-08-11"

    # 本地暂存盘余量门槛：低于它就拒绝上传（避免把宿主磁盘写满）
    min_free_bytes: int = 536870912  # 512 MiB

    # 写操作限流（按 sub + IP）
    upload_attempts_per_window: int = 30
    upload_window_seconds: int = 600

    # 清理器
    cleanup_enabled: bool = True
    cleanup_interval_seconds: int = 60
    cleanup_batch_size: int = 200
    tombstone_retention_days: int = 30
    temp_file_max_age_seconds: int = 3600

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    log_level: str = "info"

    # ------------------------------------------------------------------ 校验
    @field_validator("sso_issuer")
    @classmethod
    def _check_issuer(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("SSO_ISSUER 必须以 http:// 或 https:// 开头")
        if cleaned.endswith("/v1") or "/.well-known" in cleaned:
            raise ValueError("SSO_ISSUER 只应到服务前缀（如 .../auth），不要带 /v1 或 /.well-known")
        return cleaned

    @field_validator("sso_jwks_url")
    @classmethod
    def _check_jwks_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("SSO_JWKS_URL 必须以 http:// 或 https:// 开头")
        return cleaned

    @field_validator("required_read_scope", "required_write_scope")
    @classmethod
    def _check_scope(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and any(ch.isspace() for ch in cleaned):
            raise ValueError("REQUIRED_*_SCOPE 只能是单个 scope（不能含空格）")
        return cleaned

    @field_validator("storage_backend")
    @classmethod
    def _check_backend(cls, value: str) -> str:
        cleaned = str(value).strip().lower()
        if cleaned not in _BACKENDS:
            raise ValueError(f"STORAGE_BACKEND 只允许 {'/'.join(_BACKENDS)}")
        return cleaned

    @field_validator("download_mode")
    @classmethod
    def _check_download_mode(cls, value: str) -> str:
        cleaned = str(value).strip().lower()
        if cleaned not in _DOWNLOAD_MODES:
            raise ValueError(f"DOWNLOAD_MODE 只允许 {'/'.join(_DOWNLOAD_MODES)}")
        return cleaned

    @field_validator("local_storage_root", "temp_root", "database_url")
    @classmethod
    def _check_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("该配置项不能为空")
        return cleaned

    @field_validator("cos_region", "cos_bucket", "cos_secret_id", "cos_secret_key", "cos_scheme")
    @classmethod
    def _check_cos_text(cls, value: str) -> str:
        return str(value).strip()

    @field_validator("cos_key_prefix")
    @classmethod
    def _check_cos_prefix(cls, value: str) -> str:
        cleaned = str(value).strip().strip("/")
        if any(part in ("", ".", "..") for part in cleaned.split("/")):
            raise ValueError("COS_KEY_PREFIX 不能含空段 / . / ..")
        return cleaned

    @field_validator("default_visibility")
    @classmethod
    def _check_visibility(cls, value: str) -> str:
        cleaned = str(value).strip().lower()
        if cleaned not in VISIBILITIES:
            raise ValueError(f"DEFAULT_VISIBILITY 只允许 {'/'.join(VISIBILITIES)}")
        return cleaned

    @field_validator("capacity_expires_at")
    @classmethod
    def _check_capacity_expiry(cls, value: str) -> str:
        cleaned = str(value).strip()
        if not cleaned:
            return ""
        try:
            datetime.strptime(cleaned, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("CAPACITY_EXPIRES_AT 必须是 YYYY-MM-DD 或留空") from exc
        return cleaned

    @field_validator("cos_timeout_seconds", "jwks_timeout_seconds")
    @classmethod
    def _check_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("超时必须是正数")
        return float(value)

    @field_validator(
        "max_file_bytes",
        "default_ttl_seconds",
        "max_ttl_seconds",
        "max_grants_per_file",
        "quota_max_files",
        "quota_max_bytes",
        "max_total_bytes",
        "min_free_bytes",
        "upload_attempts_per_window",
        "upload_window_seconds",
        "cleanup_interval_seconds",
        "cleanup_batch_size",
        "tombstone_retention_days",
        "temp_file_max_age_seconds",
        "cos_max_retries",
        "jwks_cache_seconds",
    )
    @classmethod
    def _check_positive(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("该配置项必须是正整数")
        return int(value)

    @field_validator("storage_warn_percent")
    @classmethod
    def _check_percent(cls, value: int) -> int:
        if not 1 <= int(value) <= 100:
            raise ValueError("STORAGE_WARN_PERCENT 必须在 1..100")
        return int(value)

    @field_validator("presign_expires_seconds")
    @classmethod
    def _check_presign_expiry(cls, value: int) -> int:
        if not 60 <= int(value) <= 900:
            raise ValueError("PRESIGN_EXPIRES_SECONDS 必须在 60..900（预签名 URL 泄露的风险窗口）")
        return int(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.default_ttl_seconds > self.max_ttl_seconds:
            raise ValueError("DEFAULT_TTL_SECONDS 不能大于 MAX_TTL_SECONDS")
        if self.max_file_bytes > self.quota_max_bytes:
            raise ValueError("MAX_FILE_BYTES 不能大于 QUOTA_MAX_BYTES")
        if self.quota_max_bytes > self.max_total_bytes:
            raise ValueError("QUOTA_MAX_BYTES 不能大于 MAX_TOTAL_BYTES")
        if self.storage_backend == "cos":
            missing = [
                name
                for name, value in (
                    ("COS_REGION", self.cos_region),
                    ("COS_BUCKET", self.cos_bucket),
                    ("COS_SECRET_ID", self.cos_secret_id),
                    ("COS_SECRET_KEY", self.cos_secret_key),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "STORAGE_BACKEND=cos 时缺少必填项：" + ", ".join(missing) + "（放在服务器 /srv/file/app.env）"
                )
        return self

    # ------------------------------------------------------------ 派生属性
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def primary_scopes_enforced(self) -> bool:
        return bool(self.required_read_scope or self.required_write_scope)

    @property
    def capacity_expires_date(self) -> date | None:
        if not self.capacity_expires_at:
            return None
        return datetime.strptime(self.capacity_expires_at, "%Y-%m-%d").date()

    def log_summary(self) -> dict[str, object]:
        """启动日志用的摘要 —— **绝不含 COS 密钥**。"""
        return {
            "issuer": self.sso_issuer,
            "jwks_url": self.sso_jwks_url,
            "audience": self.jwt_audience,
            "required_read_scope": self.required_read_scope or "(disabled)",
            "required_write_scope": self.required_write_scope or "(disabled)",
            "database_url": self.database_url,
            "storage_backend": self.storage_backend,
            "cos": {
                "region": self.cos_region,
                "bucket": self.cos_bucket,
                "key_prefix": self.cos_key_prefix,
                "secret_id_set": bool(self.cos_secret_id),
                "secret_key_set": bool(self.cos_secret_key),
            }
            if self.storage_backend == "cos"
            else {"local_root": self.local_storage_root},
            "download_mode": self.download_mode,
            "max_file_bytes": self.max_file_bytes,
            "default_ttl_seconds": self.default_ttl_seconds,
            "max_ttl_seconds": self.max_ttl_seconds,
            "default_visibility": self.default_visibility,
            "quota_max_files": self.quota_max_files,
            "quota_max_bytes": self.quota_max_bytes,
            "max_total_bytes": self.max_total_bytes,
            "capacity_expires_at": self.capacity_expires_at or "(unset)",
            "cleanup_enabled": self.cleanup_enabled,
            "cors_origins": self.cors_origin_list,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清掉缓存，让下一次 get_settings() 重新读环境变量。"""
    get_settings.cache_clear()
