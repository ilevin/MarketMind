# Docker 构建指南

本文档说明如何构建 MarketMind 的 Docker 镜像。

## 前置条件

- 已安装 Docker（版本 20.10+）
- Python 虚拟环境已创建（`.venv/`）
- 项目依赖已安装（`pip install -e .`）

## 快速构建

使用自动化脚本一键构建：

```bash
./scripts/build-docker.sh v0.4.0
```

脚本会自动完成：
1. 清理旧构建产物
2. 构建项目 wheel 包
3. 下载所有依赖到 `dist/wheels/`
4. 下载构建工具（setuptools、wheel）
5. 构建 Docker 镜像

## 手动构建步骤

如果需要手动控制构建流程：

### 1. 构建 wheel 包

```bash
.venv/bin/python -m build --wheel
```

生成 `dist/marketmind-<version>-py3-none-any.whl`

### 2. 下载依赖

```bash
# 下载运行时依赖（63 个包）
.venv/bin/pip download -d dist/wheels dist/marketmind-*.whl

# 下载构建工具（jsonpath 等源码包需要）
.venv/bin/pip download -d dist/wheels setuptools wheel
```

### 3. 构建镜像

```bash
sudo docker build -t marketmind:v0.4.0 .
```

## 为什么需要离线 wheels？

Dockerfile 采用**离线安装**模式（`pip install --no-index --find-links`）：

- **解决 DNS 问题**：部分环境下 Docker 容器无法解析 pypi.org
- **提高构建速度**：预下载依赖，避免每次构建重新下载
- **确保可重复性**：锁定依赖版本，不受 PyPI 网络波动影响

## dist/wheels/ 目录说明

- **不提交到 Git**：已在 `.gitignore` 中排除（约 100MB）
- **构建时生成**：每次构建前重新生成，确保依赖最新
- **包含内容**：
  - 项目 wheel 包（marketmind-*.whl）
  - 所有运行时依赖（duckdb、fastapi、pandas 等 63 个）
  - 构建工具（setuptools、wheel，用于编译源码包）

## 镜像规格

- **基础镜像**：python:3.12-slim
- **大小**：约 950 MB
- **架构**：linux/amd64
- **工作目录**：`/app`
- **数据目录**：`/app/data`（建议挂载外部卷）
- **端口**：8000
- **启动命令**：先执行数据库迁移，再启动 uvicorn（单 worker）

## 启动容器

### 基本启动

```bash
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config.yaml:/app/config.yaml \
  marketmind:v0.4.0
```

### 使用 Docker Compose

```yaml
services:
  marketmind:
    image: marketmind:v0.4.0
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
      - ./config.yaml:/app/config.yaml
    restart: unless-stopped
```

## 验证

### 1. 查看镜像

```bash
sudo docker images marketmind:v0.4.0
```

### 2. 检查健康端点

```bash
curl http://localhost:8000/health
# {"status":"ok","database":"ok","version":"v0.4.0"}
```

### 3. 查看日志

```bash
docker logs -f <container_id>
```

预期日志：
```
INFO: Application startup complete.
INFO [app.jobs.quote_refresh] 行情刷新任务已启动
INFO [app.jobs.fundamental_refresh] 估值刷新任务已启动
INFO [app.jobs.history_sync] 历史同步任务已启动
```

## 部署约束

- **单 worker 必须**：DuckDB 为单写者数据库，多 worker 会导致锁冲突
- **停服前备份**：`.duckdb` 和 `.duckdb.wal` 必须成对备份
- **时区设置**：代码内部使用 Asia/Shanghai，不依赖容器时区环境变量

详细部署步骤见 [部署手册_v0.3.0.md](./部署手册_v0.3.0.md)。

## 故障排查

### 构建失败：dist/wheels/ not found

**原因**：未运行构建脚本或手动步骤，`dist/wheels/` 目录不存在

**解决**：
```bash
./scripts/build-docker.sh v0.4.0
```

### 容器启动失败：database locked

**原因**：多个容器或进程同时访问同一 DuckDB 文件

**解决**：
- 确保只有一个容器运行
- 检查宿主机是否有其他进程打开数据库文件
- 删除 `.duckdb.wal` 后重启（谨慎操作，确认无数据丢失）

### 依赖安装失败：setuptools not found

**原因**：`dist/wheels/` 中缺少构建工具

**解决**：
```bash
.venv/bin/pip download -d dist/wheels setuptools wheel
```
