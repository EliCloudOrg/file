FROM python:3.12-slim

# EliCloud 文件服务 —— 运行时镜像
# 设计要点：
#   1. 应用以 uid 1002:1003（宿主机 docker-admin）运行，挂载 /data 才能写 SQLite 与上传暂存；
#   2. PIP_INDEX_URL 可覆盖：GitHub Actions 构建用官方 PyPI；
#      国内手工构建可传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   3. 文件内容本身**不进镜像、不进 /data**：生产写私有 COS 桶；
#      /data 只放元数据库（file.db）与上传暂存（tmp/），两者都是可丢弃/可备份的小数据。
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data && chown -R 1002:1003 /data

USER 1002:1003

EXPOSE 8000

# 访问日志由应用中间件输出（只记 path，不记 query），关闭 uvicorn 自带访问日志
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
