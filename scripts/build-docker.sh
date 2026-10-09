#!/bin/bash
# Docker 镜像构建脚本：生成离线依赖 wheels 并构建镜像
# 用法: ./scripts/build-docker.sh [TAG]
# 示例: ./scripts/build-docker.sh v0.4.2
# 应用安装版本自动取自 pyproject.toml 的 [project].version 并经 --build-arg 注入；
# TAG 形如版本号（v 后接数字）时必须与之一致，否则脚本中止——避免产出「标签声称的版本
# 与镜像内容不符」的镜像；latest 及自定义非版本标签不受此限制。

set -e  # 遇到错误立即退出

TAG=${1:-latest}
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> 开始构建 marketmind:${TAG}"
cd "$PROJECT_ROOT"

# 1. 解析项目版本（构建链路中的唯一解析点）并校验标签
echo "==> 解析项目版本（pyproject.toml）"
APP_VERSION=$(awk -F'"' '/^\[project\]/{p=1;next} /^\[/{p=0} p&&/^version[[:space:]]*=/{print $2; exit}' pyproject.toml)

if [ -z "$APP_VERSION" ]; then
  echo "ERROR: 无法从 pyproject.toml 的 [project] 段解析 version（未找到形如 version = \"x.y.z\" 的行）。" >&2
  exit 1
fi

echo "==> 项目版本: ${APP_VERSION}"

if [[ "$TAG" =~ ^v[0-9] && "$TAG" != "v${APP_VERSION}" ]]; then
  echo "ERROR: 镜像标签 ${TAG} 与 pyproject.toml 的项目版本 ${APP_VERSION} 不一致。" >&2
  echo "       请改为 ./scripts/build-docker.sh v${APP_VERSION}，或使用非版本标签（如 latest）。" >&2
  exit 1
fi

# 2. 清理旧构建产物
echo "==> 清理旧构建产物"
rm -rf build/ dist/ marketmind.egg-info/

# 3. 构建 wheel 包
echo "==> 构建 marketmind wheel"
.venv/bin/python -m build --wheel

# 4. 下载所有依赖到 dist/wheels/
echo "==> 下载运行时依赖"
.venv/bin/pip download -d dist/wheels dist/marketmind-*.whl

# 5. 下载构建工具（jsonpath 等源码包需要）
echo "==> 下载构建工具"
.venv/bin/pip download -d dist/wheels setuptools wheel

# 6. 显示 wheels 统计
WHEEL_COUNT=$(ls -1 dist/wheels/ | wc -l)
WHEEL_SIZE=$(du -sh dist/wheels/ | cut -f1)
echo "==> 已准备 ${WHEEL_COUNT} 个包，总大小 ${WHEEL_SIZE}"

# 7. 构建 Docker 镜像（版本经 build-arg 注入，Dockerfile 不含版本字面量）
echo "==> 构建 Docker 镜像"
sudo docker build --build-arg APP_VERSION="${APP_VERSION}" -t marketmind:${TAG} .

# 8. 显示结果
echo ""
echo "==> 构建完成"
sudo docker images marketmind:${TAG}
echo ""
echo "启动容器: docker run -d -p 8000:8000 -v \$(pwd)/data:/app/data -v \$(pwd)/config.yaml:/app/config.yaml marketmind:${TAG}"
