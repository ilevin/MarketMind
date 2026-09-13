"""Alembic 迁移环境：复用应用配置与模型 metadata。

url 解析优先级：
1. Config.set_main_option("sqlalchemy.url") —— programmatic API（迁移测试）注入；
2. app.config.load_config().database.url —— 与运行时同源（容器内 /app/config.yaml）。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from alembic.ddl.impl import DefaultImpl
from sqlalchemy import engine_from_config, pool

from app.config import load_config
from app.db import Base, ensure_duckdb_dir
import app.models  # noqa: F401  确保模型注册到 Base.metadata


class DuckDBImpl(DefaultImpl):
    """duckdb-sqlalchemy 方言未内建 Alembic 支持（Phase 0 spike_sequence 结论），
    注册默认实现：DuckDB 支持事务性 DDL（spike_pool_rebuild 验证），无需改写。"""

    __dialect__ = "duckdb"
    transactional_ddl = True

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False：不禁用进程内已有 logger（否则经
    # programmatic API 在同一进程运行迁移后，应用/测试的 logger 全部失效）
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    return load_config().database.url


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL，不连接数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    # 迁移路径不经 create_db_engine：此处补做父目录确保（首次部署 data/ 不存在）
    ensure_duckdb_dir(configuration["sqlalchemy.url"])
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
