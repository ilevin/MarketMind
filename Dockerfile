FROM python:3.12-slim

# 时区双保险：代码内部统一使用 Asia/Shanghai（不依赖容器时区，见 design.md D5.1）
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 不 COPY README：pyproject.toml 未声明 readme 字段，pip 构建不读它
COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY alembic ./alembic

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data

EXPOSE 8000

# DuckDB 为单写者数据库：必须单 worker 进程（design D2 / README 部署约束）；
# 先执行数据库迁移，成功后才启动应用；迁移失败容器退出（design D5 / 技术方案 §20）
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
