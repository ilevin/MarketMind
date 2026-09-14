"""管理 CLI users 子命令测试（multi-user-auth tasks 8.2）。

以 subprocess 真实运行 ``python -m app.cli``（sys.executable 即 .venv/bin/python）：
- CWD 指向临时目录，其 ``config.yaml`` 的 ``database.url`` 指向临时 DuckDB 文件，
  CLI 的 ``load_config()`` 按当前目录读到该库（与 conftest engine fixture 的库隔离，
  由本文件自行 ``init_db`` 建表）；
- 密码经 getpass 从 stdin 管道输入（``start_new_session=True`` 使子进程脱离
  控制终端，getpass 找不到 /dev/tty 时回退读 stdin，交互终端下跑 pytest 不挂起）；
- DuckDB 为单写者嵌入式库：测试进程在 CLI 子进程运行期间不得持有同一数据库
  文件的连接，故每次读写前后即时开 / 关 engine。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.auth.password import PLACEHOLDER_HASH, hash_password, verify_password
from app.db import create_db_engine, init_db, make_session_factory
from app.models.user import AppUser
from app.repositories.user import UserRepository

# pytest 运行解释器即项目 venv 的 python（等价 .venv/bin/python -m app.cli）
PROJECT_PYTHON = sys.executable

@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    """CLI 运行环境：临时目录（config.yaml + 独立 DuckDB 文件，已建表）。

    init_db 建表后立即 dispose，把数据库文件让给随后启动的 CLI 子进程
    （DuckDB 单写者约束，进程间不能同时打开同一文件）。
    """
    db_path = tmp_path / "cli.duckdb"
    db_url = f"duckdb:///{db_path}"
    (tmp_path / "config.yaml").write_text(
        f'database:\n  url: "duckdb:///{db_path}"\n', encoding="utf-8"
    )
    engine = create_db_engine(db_url)
    try:
        init_db(engine)
    finally:
        engine.dispose()
    monkeypatch.chdir(tmp_path)
    return {"cwd": tmp_path, "db_url": db_url}


def _run_cli(cwd, *args, input_text=""):
    """真实子进程运行 python -m app.cli，密码经 stdin 管道喂给 getpass。"""
    return subprocess.run(
        [PROJECT_PYTHON, "-m", "app.cli", *args],
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=120,
        start_new_session=True,
    )


def _read_user(db_url, username):
    """即时开 engine 读用户行（大小写不敏感查重语义与线上一致），读毕即关。"""
    engine = create_db_engine(db_url)
    try:
        with make_session_factory(engine)() as session:
            user = UserRepository(session).get_by_username(username)
            if user is None:
                return None
            return {
                "username": user.username,
                "role": user.role,
                "password_hash": user.password_hash,
            }
    finally:
        engine.dispose()


def _seed_user(db_url, username, *, password_hash, role="user"):
    """直接经 ORM 造用户（不依赖待测的 CLI create，用作前置状态）。"""
    engine = create_db_engine(db_url)
    try:
        with make_session_factory(engine)() as session:
            session.add(AppUser(username=username, password_hash=password_hash, role=role))
            session.commit()
    finally:
        engine.dispose()


# ---- users create ----


def test_create_admin_user(cli_env):
    """users create --admin：退出码 0，库里建出 admin 角色用户、密码可校验通过。"""
    result = _run_cli(
        cli_env["cwd"], "users", "create", "--admin", "bob",
        input_text="pw12345678\npw12345678\n",
    )
    assert result.returncode == 0
    assert "已创建用户" in result.stdout

    row = _read_user(cli_env["db_url"], "bob")
    assert row is not None
    assert row["role"] == "admin"
    assert verify_password("pw12345678", row["password_hash"]) is True


def test_create_password_mismatch_three_attempts(cli_env):
    """连续 3 次两次输入不一致：拒绝并退出非 0，且不产生任何用户。

    该路径只走到 _prompt_password（不触库），不受会话工厂缺陷影响。
    """
    result = _run_cli(
        cli_env["cwd"], "users", "create", "bob",
        input_text="password123\npassword456\n" * 3,
    )
    assert result.returncode != 0
    assert result.stdout.count("两次输入不一致") == 3
    assert _read_user(cli_env["db_url"], "bob") is None


def test_create_duplicate_username_case_insensitive(cli_env):
    """重复 create 同名（大小写变体）：退出码非 0、提示已存在、库中仍只有一个用户。"""
    ok = _run_cli(
        cli_env["cwd"], "users", "create", "bob",
        input_text="pw12345678\npw12345678\n",
    )
    assert ok.returncode == 0

    dup = _run_cli(
        cli_env["cwd"], "users", "create", "BOB",
        input_text="pw12345678\npw12345678\n",
    )
    assert dup.returncode != 0
    assert "用户名已存在" in dup.stdout

    # 用户名唯一性按小写比较：BOB 查到的仍是原 bob，且保留原始大小写
    row = _read_user(cli_env["db_url"], "BOB")
    assert row is not None
    assert row["username"] == "bob"


# ---- users set-password ----


def test_set_password_replaces_old_password(cli_env):
    """users set-password：退出码 0，新密码可校验、旧密码失效。"""
    _seed_user(cli_env["db_url"], "bob", password_hash=hash_password("password123"))
    old_hash = _read_user(cli_env["db_url"], "bob")["password_hash"]
    assert verify_password("password123", old_hash) is True

    result = _run_cli(
        cli_env["cwd"], "users", "set-password", "bob",
        input_text="newpassword456\nnewpassword456\n",
    )
    assert result.returncode == 0

    row = _read_user(cli_env["db_url"], "bob")
    assert verify_password("newpassword456", row["password_hash"]) is True
    assert verify_password("password123", row["password_hash"]) is False


def test_set_password_on_legacy_placeholder(cli_env):
    """legacy owner 占位哈希：设密码前任何明文都不可登录，经 CLI 设密码后可登录。

    对应"升级后必须先 set-password admin 才能登录"的部署要求
    （迁移写入 PLACEHOLDER_HASH，见 user-management spec / design D10）。
    """
    _seed_user(cli_env["db_url"], "admin", password_hash=PLACEHOLDER_HASH, role="admin")
    placeholder = _read_user(cli_env["db_url"], "admin")["password_hash"]
    assert verify_password("password123", placeholder) is False

    result = _run_cli(
        cli_env["cwd"], "users", "set-password", "admin",
        input_text="newpassword123\nnewpassword123\n",
    )
    assert result.returncode == 0

    row = _read_user(cli_env["db_url"], "admin")
    assert verify_password("newpassword123", row["password_hash"]) is True
    assert verify_password("password123", row["password_hash"]) is False


# ---- users promote ----


def test_promote_user_to_admin(cli_env):
    """users promote：退出码 0，角色变为 admin。"""
    _seed_user(cli_env["db_url"], "bob", password_hash=hash_password("password123"))
    result = _run_cli(cli_env["cwd"], "users", "promote", "bob")
    assert result.returncode == 0
    assert _read_user(cli_env["db_url"], "bob")["role"] == "admin"
