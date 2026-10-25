"""EliCloud 文件服务（对外 `/file/*`，容器内 `/v1/...`）。

用户上传文件；每个文件有自己的 id、有效期与可见性（public/private/protected），
属主是上传者的 SSO `sub`；其他模块凭「文件 id + 该用户的 SSO 令牌」取内容。

内容存放在私有 COS 桶（生产）或本地磁盘（测试/开发），元数据在本地 SQLite。
"""

from __future__ import annotations

__version__ = "0.1.0"
