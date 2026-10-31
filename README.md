# EliCloud 文件服务（`file`）

用户的通用文件存储服务：上传后得到一个**不可猜测的 id** 与**过期时间**，属主是上传者的 SSO `sub`；
其他模块凭「文件 id + 该用户的 SSO access token」拿元数据与内容。

* 对外路径：`https://146.56.237.33/file/*`（网关剥掉 `/file`，容器内是 `/v1/...`）
* 容器 / 项目 / 镜像：`file` / `file` / `ghcr.io/elicloudorg/file:prod`
* 元数据：本地 SQLite（`/data/file.db`）
* 内容：**腾讯云私有 COS 桶** `elicloudcos-1465229333`（ap-nanjing，150 GiB）
* 运行手册：[README-deploy.md](README-deploy.md)

## 一、为什么不用 `/elicos` 挂载

服务器上 `/elicos` 是桶 `elicoud-cos-1465229333` 的 cosfs 挂载，但：

1. **Docker 是 snap 装的，容器里看不到宿主挂载**（实测容器内 `/elicos` 是空目录，inode 与宿主不同）；
2. 那个桶**匿名可列举、可读**（`GET /?prefix=elicos/` 返回 200 `ListBucketResult`），
   放 `private`/`protected` 文件等于公开；
3. 在容器里自挂 cosfs 需要 `SYS_ADMIN` + FUSE + 把密钥塞进容器，且 `rename` 退化为 copy+delete。

所以内容走 **COS SDK**（`cos-python-sdk-v5`）直连私有桶，服务器地域与桶同地域（ap-nanjing），
内网流量免费。

## 二、接口

| 方法 | 路径（容器内） | scope | 说明 |
|---|---|---|---|
| POST | `/v1/files` | `files:write` | multipart：`file` + 可选 `ttl_seconds`（0 = 永久，仅 public）、`visibility` |
| GET | `/v1/files` | `files:read` | 自己的文件列表 + 配额；`?limit&offset&status=active\|expired\|deleted\|all` |
| GET | `/v1/files/{id}` | `files:read` | 元数据 |
| GET | `/v1/files/{id}/content` | `files:read` | 内容（200/206/304/302） |
| PATCH | `/v1/files/{id}` | `files:write` | `{"visibility": "public\|private\|protected"}`（仅属主） |
| DELETE | `/v1/files/{id}` | `files:write` | 仅属主；幂等 |
| GET/POST | `/v1/files/{id}/grants` | `files:write` | 查看/新增授权（仅属主，`{"grantee_sub": "..."}`）|
| DELETE | `/v1/files/{id}/grants/{sub}` | `files:write` | 撤销授权（仅属主） |
| GET | `/v1/me` | `files:read` | 当前身份（sub/scope/issuer） |
| GET | `/healthz` | 无 | 数据库 + 存储探活、容量占比 |

### 可见性矩阵

| visibility | 属主 | 被授权 `sub` | 其他已登录用户 |
|---|---|---|---|
| `private`（默认） | ✅ | ❌ 404 | ❌ 404 |
| `protected` | ✅ | ✅ | ❌ 404 |
| `public` | ✅ | ✅ | ✅ |

* **`public` 不是匿名**：仍要有效令牌且含 `files:read`。
* 越权一律 **404**（不泄露存在性）；过期 **410**；已删除 **404**。
* `protected` 的授权对象是 SSO `sub`（永不变更），并按文件授权，不按角色/用户名。
* `protected → private` 立即对授权者关闭，但**保留**授权行；切回 `protected` 自动恢复。

### 永久文件（`ttl_seconds=0`）

* `ttl_seconds=0` 表示**永久**，但**只能与 `visibility=public` 一起使用**；
  `private` / `protected` + 永久会返回 400（避免一条私有记录永远不过期）。
* 永久文件对外返回 `expires_at=null`、`expires_in=null`、`permanent=true`；
  普通文件返回 `permanent=false`。
* 数据库仍用 `expires_at NOT NULL`，内部存哨兵值 `9999-12-31T23:59:59Z`；
  清理器按 `expires_at <= now` 扫描，哨兵永远不会命中，因此永久文件不会被回收。
* 这是为 `userinfo` 的头像 / banner 增加的路径；旧版本 file 不认识 `ttl=0`，升级顺序必须先 file 后 userinfo。

### 下载模式（`DOWNLOAD_MODE`）

| 值 | 无 `?direct` | `?direct=0` | `?direct=1` |
|---|---|---|---|
| `proxy`（默认） | 服务端流式转发 | 代理 | 代理 |
| `presign` | 302 到 COS 预签名 URL | 代理 | 302 |
| `both` | 代理 | 代理 | 302 |

`local` 后端没有预签名能力，`presign`/`both` 会自动回落代理（启动日志有 WARNING）。
`HEAD` 永不 302；`If-None-Match` 命中的 304 优先于 302。

## 三、数据与存储

```
files           一行 = 一个文件（id/user_id/filename/content_type/size/sha256/
                storage_backend/storage_key/visibility/status/created_at/expires_at/...）
file_grants     (file_id, grantee_sub) —— protected 的授权
audit_logs      上传/删除/改可见性/授权/过期/拒绝/对象丢失（不含内容与令牌）
```

* 对象 key：`<COS_KEY_PREFIX>/blobs/<id 第 3-4 位>/<id>`（默认前缀 `elicloud-file`），
  由文件 id 派生，**不接受任何用户输入拼路径**。
* 每个对象显式 `ACL=private`，对象头带 `Content-Type` / `Content-Disposition` /
  `x-cos-meta-sha256`。
* ETag = 带引号的 sha256；支持单区间 `Range`（多区间 416）。
* `storage_backend` **按行记录**：换后端后旧文件仍能按原后端读出来。

## 四、配置

全部环境变量见 [`.env.example`](.env.example)。要点：

| 变量 | 默认 | 说明 |
|---|---|---|
| `STORAGE_BACKEND` | `local` | 生产必须 `cos`（`deploy.sh` 会拒绝其它值） |
| `COS_BUCKET` | `elicloudcos-1465229333` | 私有桶 |
| `COS_SECRET_ID` / `COS_SECRET_KEY` | 无 | `backend=cos` 时缺失即启动失败 |
| `MAX_FILE_BYTES` | 25 MiB | 单文件上限 |
| `MAX_TTL_SECONDS` | 30 天 | 有效期上限 |
| `QUOTA_MAX_FILES` / `QUOTA_MAX_BYTES` | 200 / 1 GiB | 每用户 |
| `MAX_TOTAL_BYTES` | 90 GiB | 服务总容量（对 150 GiB 留余量） |
| `DOWNLOAD_MODE` | `proxy` | 见上表 |
| `CLEANUP_INTERVAL_SECONDS` | 60 | 过期/墓碑/暂存清理 |

## 五、本地开发

最省事的方式（自带**假 SSO**，不需要真实令牌）：

```powershell
python -m venv .venv
$env:PYTHONUTF8 = '1'   # Windows：pip 以 UTF-8 读 requirements 里的中文注释
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

.\.venv\Scripts\python.exe scripts\dev_local.py
# -> http://127.0.0.1:8002/healthz
# -> 打印一枚开发令牌，并写到 data/dev/dev_token.txt（gitignored）
```

手工起服务 / 用真实 SSO：

```powershell
# 本地存储 + 临时目录，不需要 COS 凭据
$env:STORAGE_BACKEND = 'local'
$env:LOCAL_STORAGE_ROOT = './data/blobs'
$env:TEMP_ROOT = './data/tmp'
$env:DATABASE_URL = 'sqlite:///./data/file.db'
$env:SSO_ISSUER = 'https://146.56.237.33/auth'
$env:SSO_JWKS_URL = 'https://146.56.237.33/auth/.well-known/jwks.json'
$env:CLEANUP_ENABLED = 'false'

.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8002
.\.venv\Scripts\python.exe scripts\dev_local.py --real-sso   # 或者：用上面的真实 SSO 配置
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\e2e_local.py
```

测试全程不访问真实 SSO/COS：`tests/fake_jwks.py` 起一个真 HTTP 的假 JWKS，
`tests/fake_storage.py` 提供内存后端与 COS 客户端替身。
`scripts/dev_local.py` 只是把「假 JWKS + uvicorn」拼在一起，方便手工 curl。

## 六、运维

```bash
docker compose exec file python -m app.cli check-storage --write-test   # 存储探针（put/get/delete）
docker compose exec file python -m app.cli stats                        # 文件数/容量/容量包到期
docker compose exec file python -m app.cli sweep [--orphans] [--dry-run]
```

COS 的孤儿扫描只在手工执行 `sweep --orphans` 时进行（列举桶是要花钱的请求），
正常请求路径永不列举桶。

## 七、非目标

匿名访问、公共文件浏览/搜索、按用户名或角色授权、授权有效期、断点续传、秒传/去重、
病毒扫描、图片处理、CDN、容器内 cosfs 挂载、使用公开桶 `/elicos`。
