#!/usr/bin/env bash
set -euo pipefail

# 服务器初始化：在目标服务器上以 root 运行一次，幂等，可重复执行。
#
#   sudo bash scripts/bootstrap-server.sh [app]
#
# 默认 app=file。做什么：
#   1. 创建 deploy 系统用户（若不存在）
#   2. 创建 /srv/<app>，归属 deploy:deploy，权限 750
#   3. 把 deploy 加入 docker 组（若本机存在 docker 组）
#   4. 创建空的运行时环境变量文件 /srv/<app>/app.env（600，deploy:deploy）
#   5. 打印仍需人工完成的步骤（authorized_keys、app.env、COS 凭据等）
#
# 不做什么：不安装 Docker、不拉代码、不构建、不写 systemd。

APP="${1:-file}"
APP_DIR="/srv/${APP}"
DEPLOY_USER="deploy"
DEPLOY_HOME="/home/${DEPLOY_USER}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "[bootstrap] 请用 root 运行：sudo bash $0 ${APP}" >&2
  exit 1
fi

echo "[bootstrap] app=${APP} dir=${APP_DIR}"

# 1) deploy 系统用户
if id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
  echo "[bootstrap] 用户 ${DEPLOY_USER} 已存在，跳过创建"
else
  useradd --system --create-home --home-dir "${DEPLOY_HOME}" --shell /bin/bash "${DEPLOY_USER}"
  echo "[bootstrap] 已创建系统用户 ${DEPLOY_USER}"
fi

# 2) 部署目录：/srv/<app>，deploy:deploy，750
install -d -m 0750 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${APP_DIR}"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${APP_DIR}"
chmod 0750 "${APP_DIR}"
echo "[bootstrap] 目录 ${APP_DIR} 就绪（${DEPLOY_USER}:${DEPLOY_USER} 0750）"

# 3) docker 组（仅当本机装了 Docker）
if getent group docker >/dev/null 2>&1; then
  if id -nG "${DEPLOY_USER}" | tr ' ' '\n' | grep -qx docker; then
    echo "[bootstrap] ${DEPLOY_USER} 已在 docker 组"
  else
    usermod -aG docker "${DEPLOY_USER}"
    echo "[bootstrap] 已把 ${DEPLOY_USER} 加入 docker 组（该用户重新登录后生效）"
  fi
else
  echo "[bootstrap] 未发现 docker 组，跳过（项目不用 Docker 时属正常）"
fi

# 4) 运行时环境变量文件（内容由运维填写，不进版本库）
if [[ -f "${APP_DIR}/app.env" ]]; then
  echo "[bootstrap] ${APP_DIR}/app.env 已存在，保持内容不变"
else
  install -m 0600 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" /dev/null "${APP_DIR}/app.env"
  echo "[bootstrap] 已创建空的 ${APP_DIR}/app.env（0600）"
fi

cat <<EOF

================ 仍需人工完成 ================
1. SSH 免密（GitHub Actions -> 服务器，使用 deploy 用户）
   - 在本机生成部署专用密钥对（不要复用个人密钥）：
       ssh-keygen -t ed25519 -C "github-actions-deploy@${APP}" -f ./deploy_key -N ""
   - 把公钥装到服务器（在服务器上执行）：
       install -d -m 0700 -o ${DEPLOY_USER} -g ${DEPLOY_USER} ${DEPLOY_HOME}/.ssh
       cat deploy_key.pub >> ${DEPLOY_HOME}/.ssh/authorized_keys
       chown ${DEPLOY_USER}:${DEPLOY_USER} ${DEPLOY_HOME}/.ssh/authorized_keys
       chmod 0600 ${DEPLOY_HOME}/.ssh/authorized_keys
   - 私钥全文写入 GitHub Secrets：SSH_KEY
   - 用 ssh -i deploy_key ${DEPLOY_USER}@<host> 验证能免密登录
2. 运行时环境变量：编辑 ${APP_DIR}/app.env（保持 0600、${DEPLOY_USER}:${DEPLOY_USER}）
   - 必须含：STORAGE_BACKEND=cos、COS_REGION、COS_BUCKET、COS_SECRET_ID、COS_SECRET_KEY
   - 说明：deploy.sh 会拒绝 STORAGE_BACKEND != cos 的部署
3. COS 凭据：在腾讯云 CAM 建子账号，只给桶 elicoudcos-1465229333 的
   elicloud-file/* 前缀 Put/Get/Delete/Head/List 权限，见 README-deploy.md
4. Docker 组生效：确认 deploy 用户重新登录后可以免 sudo 执行 docker ps
5. 防火墙 / 云安全组：只放行必要端口（22 / 80 / 443 ...）
6. 首次部署：GitHub -> Actions -> Deploy to production -> Run workflow（ref=prod）
7. 网关：把 deploy/file-caddy-block.Caddyfile 追加进 dsh-nas Caddyfile 并 reload
=============================================
EOF
