"""版本号一致性防漂移测试：app/version.py、pyproject.toml、Dockerfile 与构建脚本。

守护构建链路不再因「发布时手工同步多处版本号」而漂移
（fix-docker-version-drift；openspec/specs/app-version 与 specs/deployment）。
纯文件读取：不依赖 fixture、数据库或网络。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from app.version import APP_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"
BUILD_SCRIPT_PATH = REPO_ROOT / "scripts" / "build-docker.sh"


def _pyproject_version() -> str:
    with PYPROJECT_PATH.open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_app_version_matches_pyproject_version():
    """APP_VERSION 须等于 pyproject.toml 的 version（带 v 前缀）。"""
    version = _pyproject_version()
    assert APP_VERSION == f"v{version}", (
        f"版本号漂移：app/version.py 的 APP_VERSION={APP_VERSION!r} 与 "
        f"pyproject.toml 的 version={version!r} 不一致，请同步更新两处"
    )


def test_dockerfile_installs_version_from_build_arg():
    """Dockerfile 不得硬编码安装版本，须以 ${APP_VERSION} 安装。"""
    text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    hardcoded = re.findall(r"marketmind==\d[^\s\"']*", text)
    assert not hardcoded, (
        f"Dockerfile 不得硬编码安装版本，发现字面量 {hardcoded}；"
        "版本须由 scripts/build-docker.sh 经 --build-arg APP_VERSION 注入"
    )
    assert "marketmind==${APP_VERSION}" in text, (
        "Dockerfile 须以 marketmind==${APP_VERSION} 安装应用"
    )


def test_build_script_derives_and_injects_version():
    """构建脚本须从 pyproject.toml 解析版本并注入 --build-arg。"""
    text = BUILD_SCRIPT_PATH.read_text(encoding="utf-8")
    assert "pyproject.toml" in text, (
        "构建脚本须从 pyproject.toml 解析版本（构建链路的唯一解析点）"
    )
    assert re.search(r"--build-arg\s+APP_VERSION", text), (
        "构建脚本须向 docker build 传入 --build-arg APP_VERSION"
    )