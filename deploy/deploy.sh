#!/usr/bin/env bash
set -euo pipefail

# 远程部署入口。由 GitHub Actions（.github/workflows/deploy-prod.yml）通过 SSH 调用：
#
#   /srv/file/deploy.sh [ref]
#
# 约定：
#   - 参数：要部署的 Git ref，默认 prod（本脚本只把它打在日志里；镜像是 :prod 或 FILE_IMAGE_TAG）
#   - 幂等：可重复执行，重复跑同一 ref 不产生破坏性副作用
#   - 失败必须非 0 退出（set -e 已保证，但请勿自己吞掉错误）
#   - 本文件由仓库 deploy/deploy.sh 同步而来，不要在服务器上直接改；
#     改动请提交到仓库，再由 deploy-prod.yml 上传
#   - 运行时环境变量放在 /srv/file/app.env，不要写进本文件
#
# 本项目的部署形态：
#   - DEPLOY_MODE=image：Actions 构建并推 ghcr.io/elicloudorg/file:prod（+ sha-<short>）
#   - 本脚本拉镜像、生成 compose、重建容器、等 healthy、自检 /healthz
#   - 文件内容在**私有 COS 桶**（elicoudcos-1465229333）；/data 只放 file.db 与上传暂存，
#     落在 /home/docker-admin/elicoud/file/data，容器内 /data
#   - 本机 Docker 是 snap 装的，看不到 /srv；所以只把 /home 下的路径交给 docker，
#     /srv/file/app.env 由 bash 自己读，再写到 /home/deploy/elicoud-file/.env
#
# 安全护栏：
#   - 生产**必须** STORAGE_BACKEND=cos。配成 local 会直接把用户文件写进容器卷，
#     既没有对象存储的持久性，也吃宿主磁盘 —— 这里直接拒绝部署（fail closed）。
#   - COS 凭据只从 app.env 读，本脚本只判空，绝不回显。
#
# 回滚：Actions -> Deploy to production -> Run workflow -> ref 填旧版本；
#       或服务器上手工：
#         sudo -u deploy env FILE_IMAGE_TAG=sha-1a2b3c4 /srv/file/deploy.sh prod
# ============================

REF="${1:-prod}"
APP_DIR="/srv/file"
APP_ENV="${APP_DIR}/app.env"

echo "[deploy] ref=${REF} dir=${APP_DIR}"

cd "${APP_DIR}"

# ===== 项目自定义部署逻辑 =====
# 框架只负责「触发 + 调用 + 上传本脚本」；下面是本项目的真实部署实现：
#   DEPLOY_MODE=image 时：拉 ghcr 镜像 → 生成 compose → 重建容器 → 等 healthy → 自检。
# 为什么不能把 /srv 路径交给 docker：本机 docker 是 snap 装的，看不到 /srv
#（见 README-deploy.md「服务器侧前置检查」）。
PROJECT_NAME="file"
PROJECT_DIR="/home/deploy/elicoud-file"
DATA_PARENT="/home/docker-admin/elicoud/file"
DATA_DIR="${DATA_PARENT}/data"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
DEBUG_PORT="${FILE_DEBUG_PORT:-8002}"
HEALTH_TIMEOUT=240
PULL_TIMEOUT=180

# ---------- 0) 前置检查 ----------
command -v docker >/dev/null 2>&1 || { echo "[deploy] 找不到 docker" >&2; exit 1; }
[[ -f "${APP_ENV}" ]] || { echo "[deploy] 缺少运行时变量文件 ${APP_ENV}" >&2; exit 1; }

# 从 app.env 安全取值：不做 shell eval，避免值里的特殊字符被当作命令执行
env_get() { sed -n "s/^$1=//p" "${APP_ENV}" | tail -n 1; }

require_env() {
  local name="$1" value
  value="$(env_get "${name}")"
  if [[ -z "${value}" ]]; then
    echo "[deploy] app.env 缺少必填项 ${name}" >&2
    exit 1
  fi
}

STORAGE_BACKEND_VALUE="$(env_get STORAGE_BACKEND)"
STORAGE_BACKEND_VALUE="${STORAGE_BACKEND_VALUE:-cos}"
if [[ "${STORAGE_BACKEND_VALUE}" != "cos" ]]; then
  echo "[deploy] 拒绝部署：STORAGE_BACKEND=${STORAGE_BACKEND_VALUE}（生产必须是 cos）" >&2
  echo "[deploy] 说明：local 后端会把用户文件写进容器卷，生产不允许（见 README-deploy.md）" >&2
  exit 1
fi

for required in STORAGE_BACKEND COS_REGION COS_BUCKET COS_SECRET_ID COS_SECRET_KEY; do
  require_env "${required}"
done

DOWNLOAD_MODE_VALUE="$(env_get DOWNLOAD_MODE)"; DOWNLOAD_MODE_VALUE="${DOWNLOAD_MODE_VALUE:-proxy}"
case "${DOWNLOAD_MODE_VALUE}" in
  proxy|presign|both) ;;
  *)
    echo "[deploy] DOWNLOAD_MODE 只允许 proxy / presign / both（当前 ${DOWNLOAD_MODE_VALUE}）" >&2
    exit 1
    ;;
esac

IMAGE_REPO="$(env_get FILE_IMAGE_REPO)";    IMAGE_REPO="${IMAGE_REPO:-ghcr.io/elicloudorg/file}"
IMAGE_TAG="${FILE_IMAGE_TAG:-$(env_get FILE_IMAGE_TAG)}"; IMAGE_TAG="${IMAGE_TAG:-prod}"
GATEWAY_NETWORK="$(env_get FILE_GATEWAY_NETWORK)"; GATEWAY_NETWORK="${GATEWAY_NETWORK:-dsh-nas_dsh-net}"
GHCR_USER="$(env_get FILE_GHCR_USER)"
GHCR_TOKEN="$(env_get FILE_GHCR_TOKEN)"
IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"

docker network inspect "${GATEWAY_NETWORK}" >/dev/null 2>&1 || {
  echo "[deploy] 网关网络不存在：${GATEWAY_NETWORK}" >&2
  exit 1
}

# ---------- 1) 准备 docker 可见的项目目录 ----------
install -d -m 0755 "${PROJECT_DIR}"

# 1a) app.env -> ${PROJECT_DIR}/.env；docker compose 用它做变量插值 + env_file
{
  cat "${APP_ENV}"
  echo ""
  echo "# ---- 以下由 deploy.sh 追加，不在 app.env 里维护 ----"
  echo "FILE_IMAGE=${IMAGE}"
  echo "FILE_DATA_DIR=${DATA_DIR}"
  echo "FILE_GATEWAY_NETWORK=${GATEWAY_NETWORK}"
  echo "FILE_DEBUG_PORT=${DEBUG_PORT}"
} > "${PROJECT_DIR}/.env"
chmod 0600 "${PROJECT_DIR}/.env"

# 1b) 生成编排文件（单一真源是仓库；本文件由 deploy.sh 生成，不要手改）
cat > "${COMPOSE_FILE}" <<'YAML'
# 本文件由 /srv/file/deploy.sh 生成；改部署行为请改仓库 deploy/deploy.sh。
name: file

services:
  file:
    image: ${FILE_IMAGE}
    container_name: file
    restart: unless-stopped
    # 以宿主机 docker-admin(1002:1003) 运行，数据卷才可写
    user: "1002:1003"
    env_file:
      - .env
    volumes:
      - ${FILE_DATA_DIR}:/data
    ports:
      # 只绑回环：本机 curl / SSH 隧道调试用，公网不可达
      - "127.0.0.1:${FILE_DEBUG_PORT}:8000"
    networks:
      - gateway
    healthcheck:
      # healthz 同时探数据库与 COS（结果缓存 15s），所以这一条能拦住“容器在跑但存储不可用”
      test:
        - "CMD"
        - "python"
        - "-c"
        - "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=10).status == 200 else 1)"
      interval: 30s
      timeout: 15s
      retries: 3
      start_period: 20s

networks:
  gateway:
    external: true
    name: ${FILE_GATEWAY_NETWORK}
YAML

# ---------- 2) 迁移：清掉属于其它 compose 项目的同名容器 ----------
if docker inspect "${PROJECT_NAME}" >/dev/null 2>&1; then
  existing_project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "${PROJECT_NAME}" 2>/dev/null || true)"
  if [[ "${existing_project}" != "${PROJECT_NAME}" ]]; then
    echo "[deploy] 迁移：现有 ${PROJECT_NAME} 容器属于旧部署（project='${existing_project:-非 compose}'），先移除"
    docker rm -f "${PROJECT_NAME}" >/dev/null
  fi
fi

# ---------- 3) 拉取本次要部署的镜像 ----------
if [[ -n "${GHCR_USER}" && -n "${GHCR_TOKEN}" ]]; then
  echo "[deploy] docker login ${IMAGE_REPO%%/*}（用户 ${GHCR_USER}）"
  printf '%s' "${GHCR_TOKEN}" | docker login "${IMAGE_REPO%%/*}" -u "${GHCR_USER}" --password-stdin >/dev/null
fi

echo "[deploy] docker pull ${IMAGE}（上限 ${PULL_TIMEOUT}s）"
if timeout "${PULL_TIMEOUT}" docker pull "${IMAGE}"; then
  echo "[deploy] 已拉取 ${IMAGE}"
elif docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  echo "[deploy] ⚠️ pull 失败/超时，但本机已有 ${IMAGE}，改用它" >&2
else
  echo "[deploy] pull 失败且本机没有 ${IMAGE}" >&2
  exit 1
fi

# ---------- 3b) 数据目录：用一次性容器创建/校正（deploy 用户不可遍历 /home/docker-admin） ----------
echo "[deploy] 准备数据目录 ${DATA_DIR}"
docker run --rm --user 0:0 \
  -v "${DATA_PARENT}:/parent" \
  --entrypoint sh "${IMAGE}" -c '
    set -e
    mkdir -p /parent/data
    chown 1002:1003 /parent/data
    chmod 0750 /parent/data
  '
docker run --rm --user 1002:1003 \
  -v "${DATA_DIR}:/data" \
  --entrypoint sh "${IMAGE}" -c 'test -w /data' || {
    echo "[deploy] 数据目录对 uid 1002:1003 不可写：${DATA_DIR}" >&2
    exit 1
  }

# ---------- 4) 重建容器 ----------
docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" up -d

# ---------- 5) 等健康检查通过 ----------
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
while :; do
  status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${PROJECT_NAME}" 2>/dev/null || echo missing)"
  case "${status}" in
    healthy)
      echo "[deploy] 容器健康：${status}"
      break
      ;;
    unhealthy)
      echo "[deploy] 容器健康检查失败，最近日志：" >&2
      docker logs --tail 60 "${PROJECT_NAME}" >&2 2>&1 || true
      exit 1
      ;;
    missing|exited|dead)
      echo "[deploy] 容器状态异常：${status}，最近日志：" >&2
      docker logs --tail 60 "${PROJECT_NAME}" >&2 2>&1 || true
      exit 1
      ;;
  esac
  if [[ "$(date +%s)" -ge "${deadline}" ]]; then
    echo "[deploy] 等待健康检查超时（${HEALTH_TIMEOUT}s），当前状态：${status}" >&2
    docker logs --tail 60 "${PROJECT_NAME}" >&2 2>&1 || true
    exit 1
  fi
  sleep 5
done

# ---------- 6) 存储探活（只读；写探针由运维按需手工跑） ----------
if ! docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" exec -T file \
      python -m app.cli check-storage >/dev/null 2>&1; then
  echo "[deploy] 存储探活失败：请检查 COS 凭据/桶名/网络（容器内 python -m app.cli check-storage）" >&2
  docker compose --project-directory "${PROJECT_DIR}" -f "${COMPOSE_FILE}" exec -T file \
    python -m app.cli check-storage >&2 2>&1 || true
  exit 1
fi

# ---------- 7) 容器外自检 ----------
if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "http://127.0.0.1:${DEBUG_PORT}/healthz" || true)"
  echo "[deploy] 自检 GET http://127.0.0.1:${DEBUG_PORT}/healthz -> ${code}"
  if [[ "${code}" != "200" ]]; then
    echo "[deploy] 自检失败：期望 200" >&2
    exit 1
  fi
fi

# ============================

echo "[deploy] 镜像：${IMAGE}"
echo "[deploy] 下载模式：${DOWNLOAD_MODE_VALUE}"
echo "[deploy] done"
