"""users 子命令实现：set-password / create / promote。

全部走 UserService（业务校验、写锁、Session 撤销逻辑与 API/页面一致）。
"""

from __future__ import annotations

import getpass
import logging
import sys
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.config import load_config
from app.db import create_db_engine, make_session_factory
from app.services.user_service import (
    DuplicateUsernameError,
    UserNotFoundError,
    UserService,
)

logger = logging.getLogger(__name__)


@contextmanager
def _session():
    """CLI 短命进程的数据库会话：退出时释放 engine（DuckDB 文件锁立即归还）。

    DuckDB 为单写者嵌入式库：应用服务进程在运行时独占数据库文件锁，
    CLI 作为另一进程此时无法打开数据库——连接前先探测并把该冲突
    翻译为可操作的指引（而非裸堆栈）。
    """
    config = load_config()
    engine = create_db_engine(config.database.url)
    try:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except OperationalError as exc:
            if "Could not set lock" in str(exc):
                print(
                    "错误: 数据库正被运行中的应用进程占用（DuckDB 单写者约束，"
                    "进程间不能同时打开同一数据库文件）。\n"
                    "请先停止 marketmind 服务（如 `docker compose down` 或 "
                    "Ctrl+C 停掉 uvicorn），再执行本命令；完成后重新启动服务。",
                    file=sys.stderr,
                )
                raise SystemExit(1) from exc
            raise
        factory = make_session_factory(engine)
        with factory() as session:
            yield session
    finally:
        engine.dispose()


def _prompt_password() -> str:
    """终端安全输入（getpass）：不回显、不进 shell history。"""
    for _ in range(3):
        first = getpass.getpass("新密码（至少 8 位）: ")
        second = getpass.getpass("再次输入新密码: ")
        if first != second:
            print("两次输入不一致，请重试")
            continue
        return first
    raise SystemExit("多次输入不一致，已退出")


def _set_password(args) -> int:
    password = _prompt_password()
    with _session() as session:
        service = UserService(session)
        try:
            user = service.get_by_username(args.username)
        except UserNotFoundError as exc:
            print(f"错误: {exc}")
            return 1
        service.reset_password(user.user_id, password)
    print(f"已设置用户「{args.username}」的密码（其全部登录会话已失效）")
    return 0


def _create(args) -> int:
    password = _prompt_password()
    role = "admin" if args.admin else "user"
    with _session() as session:
        try:
            UserService(session).create_user(
                username=args.username, password=password, role=role
            )
        except DuplicateUsernameError as exc:
            print(f"错误: {exc}")
            return 1
    print(f"已创建用户「{args.username}」(role={role})")
    return 0


def _promote(args) -> int:
    with _session() as session:
        service = UserService(session)
        try:
            user = service.get_by_username(args.username)
        except UserNotFoundError as exc:
            print(f"错误: {exc}")
            return 1
        service.set_role(user.user_id, "admin")
    print(f"已将用户「{args.username}」提升为管理员（其已有会话已失效）")
    return 0


def run(args) -> int:
    # 静默第三方库日志，保持 CLI 输出干净
    logging.basicConfig(level=logging.WARNING)
    if args.group == "users":
        if args.action == "set-password":
            return _set_password(args)
        if args.action == "create":
            return _create(args)
        if args.action == "promote":
            return _promote(args)
    print(f"未知命令: {args.group} {args.action}", file=sys.stderr)
    return 2
