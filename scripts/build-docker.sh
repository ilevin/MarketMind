#!/bin/bash
# Docker 镜像构建脚本：生成离线依赖 wheels 并构建镜像
# 用法: ./scripts/build-docker.sh [TAG]
# 示例: ./scripts/build-docker.sh v0.4.0

set -e  # 遇到错误立即退出

TAG=${1:-latest}
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> 开始构建 marketmind:${TAG}"
cd "$PROJECT_ROOT"

# 1. 清理旧构建产物
echo "==> 清理旧构建产物"
rm -rf build/ dist/ marketmind.egg-info/

# 2. 构建 wheel 包
echo "==> 构建 marketmind wheel"
.venv/bin/python -m build --wheel

# 3. 下载所有依赖到 dist/wheels/
echo "==> 下载运行时依赖"
.venv/bin/pip download -d dist/wheels dist/marketmind-*.whl

# 4. 下载构建工具（jsonpath 等源码包需要）
echo "==> 下载构建工具"
.venv/bin/pip download -d dist/wheels setuptools wheel

# 5. 显示 wheels 统计
WHEEL_COUNT=$(ls -1 dist/wheels/ | wc -l)
WHEEL_SIZE=$(du -sh dist/wheels/ | cut -f1)
echo "==> 已准备 ${WHEEL_COUNT} 个包，总大小 ${WHEEL_SIZE}"

# 6. 构建 Docker 镜像
echo "==> 构建 Docker 镜像"
sudo docker build -t marketmind:${TAG} .

# 7. 显示结果
echo ""
echo "==> 构建完成"
sudo docker images marketmind:${TAG}
echo ""
echo "启动容器: docker run -d -p 8000:8000 -v \$(pwd)/data:/app/data -v \$(pwd)/config.yaml:/app/config.yaml marketmind:${TAG}"
