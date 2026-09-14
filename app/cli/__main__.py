"""CLI 入口：python -m app.cli <子命令>。"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli", description="MarketMind 管理 CLI"
    )
    sub = parser.add_subparsers(dest="group", required=True)

    users = sub.add_parser("users", help="用户管理")
    users_sub = users.add_subparsers(dest="action", required=True)

    p_set = users_sub.add_parser("set-password", help="设置/重置用户密码")
    p_set.add_argument("username")

    p_create = users_sub.add_parser("create", help="创建用户")
    p_create.add_argument("username")
    p_create.add_argument("--admin", action="store_true", help="直接创建管理员")

    p_promote = users_sub.add_parser("promote", help="将用户提升为管理员")
    p_promote.add_argument("username")

    args = parser.parse_args(argv)

    from app.cli.users import run

    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
