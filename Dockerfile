FROM python:3.12-slim

# 时区双保险：代码内部统一使用 Asia/Shanghai（不依赖容器时区，见 design.md D5.1）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 使用预构建 wheel + 离线依赖（避免容器内网络请求 PyPI）
COPY dist/wheels /tmp/wheels
COPY alembic.ini ./
COPY alembic ./alembic

# 安装版本由构建方注入：单一来源是 pyproject.toml，由 scripts/build-docker.sh 解析后
# 经 --build-arg 传入。本文件 SHALL NOT 硬编码版本字面量（deployment 规格「Dockerfile」要求）。
ARG APP_VERSION
RUN test -n "${APP_VERSION}" \
      || { echo "ERROR: 缺少 APP_VERSION 构建参数（应用安装版本）。请使用 --build-arg APP_VERSION=<版本> 重新构建，版本取自 pyproject.toml；或直接运行 ./scripts/build-docker.sh <TAG>。" >&2; exit 1; } \
    && pip install --no-cache-dir --no-index --find-links /tmp/wheels "marketmind==${APP_VERSION}" \
    && rm -rf /tmp/wheels

RUN mkdir -p /app/data

EXPOSE 8000

# DuckDB 为单写者数据库：必须单 worker 进程（design D2 / README 部署约束）；
# 先执行数据库迁移，成功后才启动应用；迁移失败容器退出（design D5 / 技术方案 §20）
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
