# `file` 服务部署与运维手册

本仓库使用「GitHub Actions 触发 + 服务器自管」的自托管部署框架（与 `sso`、`mc`、`email`
同一套）。框架只负责**触发和调用**：Actions 构建镜像、把 `deploy.sh` 送上服务器、SSH 执行；
一切具体部署逻辑都在服务器的 `/srv/file/deploy.sh` 里。

> **当前状态（2026-10-31 已上线）**：`prod` 已推送并完成首次部署；服务器上有 `/srv/file`、
> 容器 `file`（healthy）与 Caddy 的 `/file/*` 路由。
>
> **首次部署踩到的两个坑（已修，留档）**：
> 1. `COS_BUCKET` 在文档里曾误写成 `elicoudcos-1465229333`（少了 `l`），导致健康检查
>    探活返回 `NoSuchResource` / `503`。账号下的真实桶名是 **`elicloudcos-1465229333`**。
> 2. 该 ghcr 包刚推上去时镜像加速源还没同步，`docker pull` 失败后由 `deploy.sh`
>    回退到服务器上的同 tag 本地镜像；等 Actions 把包推到 ghcr.io 后，后续部署即可正常拉取。

| 项 | 值 |
|---|---|
| 应用名 / 服务器目录 | `file` / `/srv/file` |
| 容器 / compose 项目 | `file` |
| 镜像 | `ghcr.io/elicloudorg/file:prod` / `:sha-<short>` |
| 技术栈 | Python 3.12 · FastAPI · SQLite（元数据）· 私有 COS 桶（内容） |
| 对外前缀 | `/file/*`（Caddy 剥前缀，容器内是 `/v1/...`） |
| 宿主调试端口 | `127.0.0.1:8002` |
| 运行时变量 | `/srv/file/app.env`（600，`deploy:deploy`） |
| 数据卷 | `/home/docker-admin/elicoud/file/data` → 容器 `/data`（`file.db` + 上传暂存） |
| 内容真源 | 私有 COS 桶 `elicloudcos-1465229333`，前缀 `elicloud-file/` |

## 1. 部署架构

```
      feature/* ──PR──▶ main ──PR──▶ prod
                         │            │
                    push │            │ push / workflow_dispatch(ref)
                         ▼            ▼
              ┌──────────────────┐  ┌───────────────────────────────────────┐
              │ CI  ci.yml       │  │ Deploy  deploy-prod.yml               │
              │  检出            │  │  ①（可选）构建镜像 → ghcr.io          │
              │  装依赖          │  │     仅当 vars.DEPLOY_MODE == image    │
              │  compileall      │  │  ② scp deploy/deploy.sh → /srv/file   │
              │  导入 app.main   │  │  ③ ssh 执行 /srv/file/deploy.sh REF   │
              │  pytest          │  │  environment: production（人工审批）  │
              └──────────────────┘  └──────────────────┬────────────────────┘
                                                       │ SSH（deploy 用户 + 私钥）
                                                       ▼
                            ┌───────────────────────────────────────────────┐
                            │ 服务器  /srv/file/                            │
                            │   deploy.sh   ← 随仓库版本管理（deploy/）      │
                            │   app.env     ← 运行时变量 + COS 密钥（600）   │
                            │ /home/deploy/elicoud-file/  ← docker 可见目录  │
                            │ /home/docker-admin/elicoud/file/data          │
                            └──────────────────┬────────────────────────────┘
                                               │ HTTPS（同地域内网域名）
                                               ▼
                            ┌───────────────────────────────────────────────┐
                            │ 私有 COS 桶 elicloudcos-1465229333 (ap-nanjing) │
                            │   elicloud-file/blobs/<xx>/f_<id>              │
                            └───────────────────────────────────────────────┘
```

> ⚠️ 本机 Docker 是 **snap** 装的，看不到 `/srv`。所以 `deploy.sh` 自己（bash）读
> `/srv/file/app.env`，把它写到 `/home/deploy/elicoud-file/.env`；交给 docker 的路径
> 全部在 `/home` 下。`docker build /srv/...`、`--env-file /srv/...`、`-v /srv/...` 都会失败。

## 2. 分支模型

| 分支 | 用途 | 合并规则 |
|---|---|---|
| `main` | 集成分支，日常 PR 合入，跑 CI | 需要 PR + CI 通过 |
| `prod` | 发布分支，受保护 | 只允许从 `main` 发 PR 合并；合并即触发部署 |
| `feature/*` | 功能分支 | 从 `main` 拉出，PR 回 `main` |

`prod` 只允许从 `main` 来的约束由 `ci.yml` 的 `Guard prod source` job 强制执行
（GitHub 分支保护无法原生表达），必须把它加进 `prod` 的必过检查。

## 3. 服务器侧前置检查

在服务器上以有 sudo 的用户执行（只读）：

| 检查 | 命令 | 期望 |
|---|---|---|
| sudo 可用 | `sudo -n true` | 通过（`bootstrap-server.sh` 要 root） |
| Docker 是否 snap | `readlink -f "$(command -v docker)"` | 落到 `/usr/lib/snapd/snapd` → **snap 版**，deploy.sh 不得把 `/srv` 交给 docker |
| docker 组 | `getent group docker` | 含 `deploy`（组变更后需重新登录） |
| deploy 用户 | `id deploy` | 存在，且 `groups` 含 docker |
| `/srv` | `ls -ld /srv /srv/file` | `/srv` 存在；`/srv/file` 由 bootstrap 创建 |
| 网关网络 | `docker network inspect dsh-nas_dsh-net` | 存在 |
| 调试端口 | `ss -ltn | grep ':8002 '` | 未被占用 |
| ghcr 可达 | `curl -s -o /dev/null -w '%{http_code}' https://ghcr.io/v2/` | **401 属正常**（匿名探测）；不等于拉不动 |
| 数据父目录 | `ls -ld /home/docker-admin/elicoud` | `deploy` 不可直接遍历属正常；deploy.sh 用一次性容器创建/校正 `data` |

本仓库初始化时的实测结果（2026-10-25）：

```
sudo-ok
docker      /snap/bin/docker -> /usr/lib/snapd/snapd   Docker version 29.8.0
docker 组   docker:x:1004:docker-admin,ubuntu,deploy
deploy      uid=1008(deploy) groups=...,1004(docker)
/srv        [/srv/file 尚不存在 → 需要跑 bootstrap]
网关网络    net-ok（dsh-nas_dsh-net）
端口 8002   port-free
ghcr        401（正常）
/home/docker-admin/elicoud  Permission denied（对 ubuntu/deploy 均不可遍历，符合预期）
```

## 4. 首次部署

1. **服务器初始化**（root，幂等）：
   ```bash
   scp scripts/bootstrap-server.sh <user>@146.56.237.33:/tmp/
   ssh <user>@146.56.237.33 'sudo bash /tmp/bootstrap-server.sh file'
   ```
   创建 `deploy` 用户、`/srv/file`（750）、空 `app.env`（600），并打印后续人工步骤。

2. **SSH 免密**：生成部署专用密钥对（不要复用个人密钥），公钥写入
   `/home/deploy/.ssh/authorized_keys`（600），私钥全文填进 GitHub Secret `SSH_KEY`；
   用 `ssh -i deploy_key deploy@146.56.237.33` 验证。

3. **填 `/srv/file/app.env`**（600）：
   ```ini
   STORAGE_BACKEND=cos
   COS_REGION=ap-nanjing
   COS_BUCKET=elicloudcos-1465229333
   COS_SECRET_ID=...
   COS_SECRET_KEY=...
   COS_KEY_PREFIX=elicloud-file
   DOWNLOAD_MODE=proxy
   SSO_ISSUER=https://146.56.237.33/auth
   SSO_JWKS_URL=https://146.56.237.33/auth/.well-known/jwks.json
   JWT_AUDIENCE=elicloud-services
   REQUIRED_READ_SCOPE=files:read
   REQUIRED_WRITE_SCOPE=files:write
   MAX_TOTAL_BYTES=96636764160
   CAPACITY_EXPIRES_AT=2027-08-11
   ```
   `deploy.sh` 会拒绝 `STORAGE_BACKEND != cos` 的部署（local 后端会把用户文件写进容器卷）。

4. **COS 子账号（最小权限）**：CAM 建一个只用于本服务的子账号，策略收敛到
   「一个桶 + 一个前缀」：
   ```json
   {
     "version": "2.0",
     "statement": [
       {
         "effect": "allow",
         "action": ["cos:GetObject", "cos:PutObject", "cos:DeleteObject",
                    "cos:HeadObject", "cos:HeadBucket", "cos:GetBucket"],
         "resource": ["qcs::cos:ap-nanjing:uid/1465229333:elicloudcos-1465229333/elicloud-file/*"]
       }
     ]
   }
   ```
   `cos:GetBucket` 只在 `cli sweep --orphans` 时用到。密钥只写 `app.env`，不进仓库、不进 Actions。

5. **GitHub 配置**：见下一节。

6. **网关**：把 `deploy/file-caddy-block.Caddyfile` 追加进 `dsh-nas` 的 Caddyfile
   （独立站点块，因为面板站点有不带 matcher 的 `basic_auth` 兜底），校验后 reload：
   ```bash
   docker run --rm --entrypoint caddy -v /home/docker-admin/dsh-nas/caddy:/etc/caddy:ro \
     caddy:2-alpine validate --config /etc/caddy/Caddyfile
   docker exec dsh-caddy caddy reload --config /etc/caddy/Caddyfile
   ```

7. **首次发布**：先让 `Build and push image` 跑一次（创建 ghcr 包），再
   Actions → *Deploy to production* → *Run workflow*，`ref=prod`。

## 5. GitHub 配置清单

### Secrets（Settings → Secrets and variables → Actions → Secrets）

| Secret | 值 | 说明 |
|---|---|---|
| `SSH_HOST` | `146.56.237.33` | 服务器地址 |
| `SSH_USER` | `deploy` | bootstrap 创建的系统用户 |
| `SSH_PORT` | `22` | SSH 端口 |
| `SSH_KEY` | 部署私钥**全文**（含 `BEGIN/END`） | 单独生成，不复用个人密钥 |
| `GHCR_TOKEN` | 一般不需要 | 同仓库推 ghcr 用内置 `GITHUB_TOKEN` |

```bash
gh secret set SSH_HOST --body '146.56.237.33'
gh secret set SSH_USER --body 'deploy'
gh secret set SSH_PORT --body '22'
gh secret set SSH_KEY < ./deploy_key      # 多行内容用 stdin 重定向
```

> PowerShell 不支持 `<`：用 `cmd /c "gh secret set SSH_KEY --repo <owner>/<repo> < key"`；
> **不要**用 `Get-Content -Raw | gh secret set`（实测可能写入空值/损坏值）。

### Variables（同一页面的 Variables 标签，**仓库级**）

| Variable | 值 | 说明 |
|---|---|---|
| `DEPLOY_MODE` | `image` | `image` → 阶段一构建并推镜像；留空 → 阶段一跳过 |

```bash
gh variable set DEPLOY_MODE --body 'image'
```

> ⚠️ 必须是**仓库级** Variable：`image` job 没有 `environment:`，读不到环境级变量。

### Environment：`production`

- Required reviewers：部署前必须有人 Approve；
- Deployment branches：建议限制为 `prod`；
- ⚠️ 限制部署分支后，用 `workflow_dispatch` 回滚时 UI 里**必须把分支选成 `prod`**，
  否则 job 会被策略直接拒绝（约 2 秒失败、无步骤日志）；
- ⚠️ 免费版账号的 **private** 仓库配不了分支保护与 Required reviewers（403/422）：
  要么把仓库设为 public，要么升级 GitHub Pro。

### 分支保护

| 分支 | 规则 |
|---|---|
| `main` | 需要 PR；必过检查 `Test and build`；Require branches to be up to date；禁 force push / 禁删除 |
| `prod` | 需要 PR；必过检查 `Test and build` + `Guard prod source`；禁 force push / 禁删除 |

> 单账号无法给自己的 PR 审批，所以 `required_approving_review_count` 先设 0
>（「必须走 PR」仍然生效）；加协作者后再调到 1（main）/ 2（prod）。

> 注：`SSH_USER=deploy` 时 Actions 会把日志里的 "deploy" 打码成 `***`，属正常。

## 6. 配置与密钥放置表

| 变量 / 配置 | 位置 | 用途 |
|---|---|---|
| `SSH_HOST` / `SSH_USER` / `SSH_PORT` | GitHub Secrets | Actions 连哪台机器、以谁登录 |
| `SSH_KEY` | GitHub Secrets | 部署私钥（Actions 侧唯一凭据） |
| 对应公钥 | 服务器 `/home/deploy/.ssh/authorized_keys`（600） | 校验上面的私钥 |
| `GITHUB_TOKEN` | Actions 内置 | 同仓库推 `ghcr.io`（靠 `packages: write`） |
| `GHCR_TOKEN` | GitHub Secrets（仅跨仓库/外部 registry） | 推镜像的替代凭据 |
| `DEPLOY_MODE` | GitHub Variables（**仓库级**） | `image` → 阶段一构建推镜像 |
| `FILE_IMAGE_REPO` / `FILE_IMAGE_TAG` / `FILE_DEBUG_PORT` | 服务器 `/srv/file/app.env`（可选） | 覆盖镜像仓库/tag、调试端口 |
| `STORAGE_BACKEND`、`COS_*` | 服务器 `/srv/file/app.env` | 对象存储后端与凭据（**运行时密钥**） |
| `SSO_ISSUER`、`SSO_JWKS_URL`、`JWT_AUDIENCE`、`REQUIRED_*_SCOPE` | 服务器 `/srv/file/app.env` | 验签与 scope |
| 业务限制（`MAX_FILE_BYTES`、TTL、配额、`DOWNLOAD_MODE`…） | 服务器 `/srv/file/app.env` | 应用进程读取 |
| 部署 ref（`prod` / tag / SHA） | 由 Actions 作为参数传给 `deploy.sh` | 决定本次部署哪个版本 |
| 人工运维私钥 | 本机 `~/.ssh/`（Windows：`C:\Users\<you>\.ssh\...`） | 仅人登录服务器，与 Actions 无关 |

规则：**部署凭据只走 GitHub Secrets；应用运行时变量只在服务器 `app.env`。**
两边都不进仓库，也不在 workflow 里 `echo`。

## 7. 日常发布流程

```bash
git switch main && git pull
git switch -c feature/xxx
# ... 开发 ...
git push -u origin feature/xxx     # 开 PR → main，等 CI 通过并合并
```

然后发 PR `main` → `prod`。合并到 `prod` 自动触发 `deploy-prod.yml`：先（可选）推镜像，
再上传并执行 `/srv/file/deploy.sh prod`。

`deploy.sh` 做的事（幂等）：校验 `app.env` → 生成 `/home/deploy/elicoud-file/.env` 与 compose
→ 拉镜像（180s 上限，失败回退本机镜像）→ 创建/校正数据目录 → `compose up -d` →
等 healthy（healthcheck 打 `/healthz`，含 COS 探活）→ `cli check-storage`（只读）→ 回环自检。

### 部署后验收

```bash
docker compose -p file exec file python -m app.cli check-storage --write-test
docker compose -p file exec file python -m app.cli stats
curl -s http://127.0.0.1:8002/healthz | jq
```

契约验收：三种可见性 × 三种身份 → 200/200/404；`Range` 206；`If-None-Match` 304；
`?direct=1` 在 `presign`/`both` 下 302；撤权立即 404；删除后 404；过期后 410。

## 8. 回滚流程

两种方式，都只是把同一个 `deploy.sh` 用另一个 ref 再跑一次：

1. **Actions 回滚（推荐）**：Actions → *Deploy to production* → *Run workflow* →
   `ref` 填上一个可用版本（之前的 tag / commit SHA / 分支）。
   `DEPLOY_MODE=image` 时阶段一会按 `ref` 检出并构建**那个版本**，`sha-<short>` 取自该
   commit；阶段二仍用当前分支的 `deploy.sh`。
   - ⚠️ 若 Environment 限制了 Deployment branches，UI 的分支必须选 `prod`。

2. **服务器手工回滚**：
   ```bash
   sudo -u deploy env FILE_IMAGE_TAG=sha-1a2b3c4 /srv/file/deploy.sh prod
   ```
   适合 Actions 不可用时应急；绕过审批，操作后补记录。

镜像 tag 保留策略：`prod`（移动）+ `sha-<short>`（不可变，回滚锚点）。保留最近 ≥10 个；
清理前确认线上 `prod` 指向的 `sha-` tag 不在删除列表里。

## 9. 容量、到期与换桶迁移

当前 COS 额度（本地容量包，非按量）：

| 额度 | 到期 |
|---|---|
| 100 GiB | 2027-10-12 |
| 50 GiB | 2027-08-11 |
| 合计 150 GiB | —— |

- `MAX_TOTAL_BYTES` 默认 **90 GiB**（按「2027-08-11 后可能只剩 100 GiB」规划）；
- `CAPACITY_EXPIRES_AT=2027-08-11`：启动日志与 `cli stats` 在到期前 120 天提醒；
- 用量到 `STORAGE_WARN_PERCENT`（默认 80%）时 `/healthz` 的 `checks.quota.warning=true`；
- 用满后新上传返回 **409 `quota_exceeded`**（已有文件仍可读/删）。

到期前选择：**续费**（改 `CAPACITY_EXPIRES_AT` 重新部署）／**降档**（调低 `MAX_TOTAL_BYTES`
并清理）／**换桶**：

```bash
# 换桶不换 key：对象 key 由文件 id 派生，DB 不用改
coscli cp cos://elicloudcos-1465229333/elicloud-file \
           cos://<新桶>/elicloud-file -r
# 校验对象数/总字节一致后，只改 app.env 的 COS_BUCKET（+ 凭据若换账号），重新部署
```

⚠️ 复制完成前不要切 `COS_BUCKET`，否则会出现「新桶读不到旧对象」。

## 10. 下载模式与成本

| 模式 | 优点 | 代价 |
|---|---|---|
| `proxy`（默认） | 统一安全头（`nosniff`/CSP/`attachment`）、可审计、调用方不用跟随重定向 | 流量走服务器出口 |
| `presign` | 省服务器带宽，直连 COS | 302 后没有 `nosniff`/CSP；COS 外网下行可能计费；调用方必须跟随重定向 |
| `both` | 默认代理，需要直链时 `?direct=1` | 两套行为都要测 |

预签名 URL：有效期 `PRESIGN_EXPIRES_SECONDS`（默认 300s，60–900）、`Cache-Control: no-store`、
**绝不写日志**。危险 MIME 在上传时已降级为 `application/octet-stream`，所以走预签名直链也不会
被浏览器当页面执行。

## 11. 备份与恢复

| 数据 | 位置 | 备份建议 |
|---|---|---|
| 元数据（真源） | `/home/docker-admin/elicoud/file/data/file.db` | 每日 `sqlite3 file.db ".backup ..."`；WAL 模式下**不能**只拷 `.db` |
| 内容 | COS 桶 `elicloudcos-1465229333`/`elicloud-file/` | 桶本身多副本；异地备份用 `coscli cp -r` |
| 运行时变量 | `/srv/file/app.env` | 密码管理器；丢失会导致无法读写桶 |

**两者必须一起恢复**：只有 DB 没有对象 → 下载 404（并会把行标记 deleted）；
只有对象没有 DB → 孤儿对象，所有 id 都不可达。

## 12. 排错

| 现象 | 原因 / 处理 |
|---|---|
| `/healthz` 503，`checks.storage.ok=false` | COS 不可达/凭据错/桶名错/CAM 策略不含前缀。先 `cli check-storage`，看日志 `status/code`（403 = 配置问题，不是文件不存在） |
| 上传 503 `storage_unavailable` | COS 超时/5xx；重试。**不要**改成 local 后端顶替 |
| 上传 409 `quota_exceeded` | 用户配额或服务总容量（`cli stats` 看 `capacity.percent`） |
| 上传 413 `file_too_large` | 超过 `MAX_FILE_BYTES`（默认 25 MiB） |
| 401 `invalid_token` | 令牌过期 / issuer / aud 不匹配（`id_token` 冒充最常见）→ 重新登录 |
| 403 `insufficient_scope` | 令牌缺 `files:read`/`files:write` → 重新登录；确认 SSO `DEFAULT_SCOPE` 已含两项 |
| 下载 404 但文件存在 | ①可见性 ②对象丢失（日志 `读取对象失败`）③`storage_backend` 与当前配置不符 |
| 下载 410 | 已过期；清理器会回收 |
| 302 后客户端拿不到字节 | 客户端没跟随重定向（curl 加 `-L`）；或改回 `DOWNLOAD_MODE=proxy` |
| 容器反复 unhealthy | `docker logs file`；healthz 同时反映 COS，凭据错会一直 503（故意的 fail closed） |
| `deploy.sh` 报「缺少必填项 COS_*」 | `app.env` 没填齐（脚本只判空、不回显） |
| `deploy.sh` 报「拒绝部署：STORAGE_BACKEND=local」 | 生产护栏，改 `app.env` 为 cos |
| Actions scp 失败 | `SSH_*` 写错 / 私钥不匹配 / `/srv/file` 不存在或不可写 |
| `deploy.sh: Permission denied` | 服务器文件没有可执行位；workflow 已 `chmod +x` |
| `bad interpreter: ...^M` | 提交时带了 CRLF；`.gitattributes` 已固定 `*.sh text eol=lf` |
| `docker pull` 报 `denied` | 该 ghcr 包还不存在（首次部署没跑过阶段一），不是权限问题；先跑一次 `Build and push image` |
| pull 停在 `Pulling fs layer` | 国内直连 ghcr 慢；deploy.sh 有 180s 超时并回退本机镜像，日志会明确写用的是哪个 |

## 13. 相关文件位置

| 文件 / 配置 | 位置 | 说明 |
|---|---|---|
| CI 工作流 | `.github/workflows/ci.yml` | PR → main/prod、push → main |
| 部署工作流 | `.github/workflows/deploy-prod.yml` | push → prod、workflow_dispatch |
| 部署脚本（版本管理） | `deploy/deploy.sh` | 上传到 `/srv/file/deploy.sh` 执行 |
| 网关片段 | `deploy/file-caddy-block.Caddyfile` | 追加进 `dsh-nas` Caddyfile |
| 服务器初始化脚本 | `scripts/bootstrap-server.sh` | 以 root 跑一次 |
| 部署密钥与服务器信息 | GitHub Secrets | `SSH_HOST`/`SSH_USER`/`SSH_KEY`/`SSH_PORT` |
| 部署模式开关 | GitHub Variables（仓库级） | `DEPLOY_MODE=image` |
| 运行时环境变量 | 服务器 `/srv/file/app.env` | 600，`deploy:deploy` |
