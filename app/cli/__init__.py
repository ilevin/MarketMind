"""管理 CLI（multi-user-auth design D10）。

用法：
    python -m app.cli users set-password <username>   # 设置/重置密码（撤销该用户 Session）
    python -m app.cli users create <username> [--admin]
    python -m app.cli users promote <username>

密码通过终端安全输入（getpass），不出现在 shell history 与仓库文件中。
CLI 假定数据库已完成 Alembic 迁移（容器启动 / 手动 alembic upgrade head）。
"""
