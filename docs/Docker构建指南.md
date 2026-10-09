# Docker 构建指南

本文档说明如何构建 MarketMind 的 Docker 镜像。

## 前置条件

- 已安装 Docker（版本 20.10+）
- Python 虚拟环境已创建（`.venv/`）
- 项目依赖已安装（`pip install -e .`）

## 快速构建

使用自动化脚本一键构建：

```bash
./scripts/build-docker.sh v0.4.2
```

镜像标签中的版本须与 `pyproject.toml` 的 `[project].version` 一致（示例为当前版本 `0.4.2`，以 `pyproject.toml` 为准）；应用安装版本由脚本自动从 `pyproject.toml` 解析，无需手工维护。

脚本会自动完成：
0. 解析 `pyproject.toml` 中的项目版本，并校验镜像标签与之一致（形如版本号的标签不一致时中止）
1. 清理旧构建产物
2. 构建项目 wheel 包
3. 下载所有依赖到 `dist/wheels/`
4. 下载构建工具（setuptools、wheel）
5. 构建 Docker 镜像（经 `--build-arg APP_VERSION` 注入版本）

## 手动构建步骤

如果需要手动控制构建流程：

### 1. 读取项目版本

```bash
# 应用安装版本取自 pyproject.toml 的 [project].version（例：0.4.2）
grep -m1 '^version' pyproject.toml
```

### 2. 构建 wheel 包

```bash
.venv/bin/python -m build --wheel
```

生成 `dist/marketmind-<version>-py3-none-any.whl`

### 3. 下载依赖

```bash
# 下载运行时依赖（63 个包）
.venv/bin/pip download -d dist/wheels dist/marketmind-*.whl

# 下载构建工具（jsonpath 等源码包需要）
.venv/bin/pip download -d dist/wheels setuptools wheel
```

### 4. 构建镜像

将 `APP_VERSION` 替换为上一步读到的实际版本号（不带 `v` 前缀）：

```bash
sudo docker build --build-arg APP_VERSION=0.4.2 -t marketmind:v0.4.2 .
```

未传 `--build-arg APP_VERSION` 时构建会立即失败并给出提示（见「故障排查」）。

## 为什么需要离线 wheels？

Dockerfile 采用**离线安装**模式（`pip install --no-index --find-links`）：

- **解决 DNS 问题**：部分环境下 Docker 容器无法解析 pypi.org
- **提高构建速度**：预下载依赖，避免每次构建重新下载
- **确保可重复性**：锁定依赖版本，不受 PyPI 网络波动影响

离线安装的版本约束（`marketmind==${APP_VERSION}`）由构建参数提供，版本来源唯一为 `pyproject.toml`：镜像内的应用版本必然等于构建时 `pyproject.toml` 的版本，Dockerfile 中不含版本字面量。

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

将 `<TAG>` 替换为构建时使用的标签（如 `v0.4.2`）：

```bash
docker run -d \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config.yaml:/app/config.yaml \
  marketmind:<TAG>
```

### 使用 Docker Compose

```yaml
services:
  marketmind:
    image: marketmind:<TAG>
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
sudo docker images marketmind:<TAG>
```

### 2. 检查健康端点

```bash
curl http://localhost:8000/health
# {"status":"ok","database":"ok","version":"v0.4.2"}
```

返回的 `version` 取自代码常量 `app/version.py` 的 `APP_VERSION`（当前版本 `v0.4.2`，以实际发布版本为准），应与 `pyproject.toml` 一致。

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

### 构建失败：缺少 APP_VERSION

**原因**：未传 `--build-arg APP_VERSION`（直接 `docker build` 或 `docker compose build` 时容易发生）

**报错**：
```
ERROR: 缺少 APP_VERSION 构建参数（应用安装版本）。请使用 --build-arg APP_VERSION=<版本> 重新构建，版本取自 pyproject.toml；或直接运行 ./scripts/build-docker.sh <TAG>。
```

**解决**：
```bash
sudo docker build --build-arg APP_VERSION=0.4.2 -t marketmind:v0.4.2 .
# 或直接使用脚本（自动解析并注入版本）
./scripts/build-docker.sh v0.4.2
```

### 构建失败：tag 与项目版本不一致

**原因**：镜像标签看起来是版本号（`v` 后接数字），但与 `pyproject.toml` 的版本不符；脚本会中止以避免产出「标签声称的版本与镜像内容不符」的镜像

**报错**：
```
ERROR: 镜像标签 v9.9.9 与 pyproject.toml 的项目版本 0.4.2 不一致。
       请改为 ./scripts/build-docker.sh v0.4.2，或使用非版本标签（如 latest）。
```

**解决**：改用与 `pyproject.toml` 一致的标签，或使用非版本标签（`latest`、自定义名称）：
```bash
./scripts/build-docker.sh v0.4.2   # 与 pyproject.toml 一致
./scripts/build-docker.sh latest   # 非版本标签，不校验
```

### 构建失败：dist/wheels/ not found

**原因**：未运行构建脚本或手动步骤，`dist/wheels/` 目录不存在

**解决**：
```bash
./scripts/build-docker.sh v0.4.2
```

### 构建失败：Permission denied（清理旧构建产物）

**原因**：`build/`、`dist/`、`marketmind.egg-info/` 曾被以 root 身份创建（例如用 `sudo ./scripts/build-docker.sh` 运行过脚本），普通用户无法删除

**解决**：脚本只需普通用户权限运行（内部会自行调用 `sudo docker`）。先以 root 清理一次旧产物，之后改用普通用户运行：
```bash
sudo rm -rf build/ dist/ marketmind.egg-info/
./scripts/build-docker.sh v0.4.2
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