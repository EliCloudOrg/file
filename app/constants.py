"""服务级常量：状态机、可见性、审计动作、输入正则、危险 MIME。

集中在这里是为了让「值只写一次」：路由、服务层、清理器、CLI 与测试都引用同一份，
避免各处拼字符串拼出不一致（`status='expried'` 这种问题在 SQL 里不会报错）。
"""

from __future__ import annotations

import re

# ------------------------------------------------------------------ 文件 id
# f_ + 22 个 URL-safe 随机字符（secrets.token_urlsafe(16) 正好 22 个）。
FILE_ID_ALPHABET_LENGTH = 22
FILE_ID_RE = re.compile(r"^f_[A-Za-z0-9_-]{22}$")

# ------------------------------------------------------------------ 可见性
# public    —— 任何持有效令牌且带 files:read 的用户都能取（仍**不是**匿名）
# private   —— 只有属主能取
# protected —— 属主 + 被属主显式授权的 SSO sub 能取
VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"
VISIBILITY_PROTECTED = "protected"
VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_PRIVATE, VISIBILITY_PROTECTED)

# ------------------------------------------------------------------ TTL
# ttl_seconds=0 表示永久；仅允许与 visibility=public 一起使用。
# 数据库 expires_at 是 NOT NULL 的历史设计，这里用哨兵值表达永久，
# 避免为了一个字段做整表迁移；API 对外仍返回 expires_at=null / permanent=true。
PERMANENT_TTL_SECONDS = 0
PERMANENT_EXPIRES_AT = "9999-12-31T23:59:59Z"

# ------------------------------------------------------------------ 状态机
STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"
STATUS_EXPIRED = "expired"

# ------------------------------------------------------------------ 审计
ACTION_UPLOAD = "upload"
ACTION_VISIBILITY = "visibility_change"
ACTION_DELETE = "delete"
ACTION_GRANT_ADD = "grant_add"
ACTION_GRANT_REMOVE = "grant_remove"
ACTION_EXPIRE = "expire"
ACTION_SWEEP = "sweep"
ACTION_DENY = "deny"
ACTION_MISSING_OBJECT = "missing_object"

RESULT_OK = "ok"
RESULT_IDEMPOTENT = "idempotent"
RESULT_NOT_FOUND = "not_found"
RESULT_EXPIRED = "expired"
RESULT_DENIED = "denied"
RESULT_QUOTA_EXCEEDED = "quota_exceeded"
RESULT_RATE_LIMITED = "rate_limited"
RESULT_UNAVAILABLE = "unavailable"
RESULT_TOO_LARGE = "too_large"
RESULT_INVALID = "invalid"

# ------------------------------------------------------- 危险 Content-Type
# 这些类型在浏览器里可以执行脚本/内联渲染。下载一律 `attachment`，但它们仍被
# 降级成 application/octet-stream：预签名 302 模式下我们无法追加 CSP/nosniff，
# 只能靠「不给出可执行的媒体类型」这一层兜底。
DANGEROUS_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/xml",
        "text/xml",
        "application/javascript",
        "text/javascript",
    }
)

# 授权对象是 SSO 的 sub：只允许可打印的 URL-safe 片段，长度上限 128。
GRANTEE_SUB_RE = re.compile(r"^[A-Za-z0-9_.:@\-]{1,128}$")

# 流式读写块大小（上传落盘 / 下载转发 / COS 流）
CHUNK_SIZE = 64 * 1024

# 审计 detail 字段统一序列化（与 mc 同形）
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
