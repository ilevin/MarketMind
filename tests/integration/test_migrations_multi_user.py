"""0002_multi_user_auth 迁移集成测试（db-migration spec / tasks 6.1-6.2）。

覆盖旧单用户库升级到多用户 schema 的完整链路：
- 旧 schema（0001）+ 真实数据 → upgrade head → 数据全部归属 legacy owner，
  行数 / 排序 / 标签关联 / tag_id 完整保留，全局表不动；
- legacy owner 不可登录：占位哈希 + must_change_password=true（可由 `/setup`
  认领，停服后的 CLI 为后备路径）；
- 校验失败整体回滚：迁移中途抛异常 → alembic 事务回滚 → 库保持 0001 原样
  （DuckDB 事务性 DDL，迁移内 staging 方案的前提）；
- 降级 0002 -> 0001：仅保留 legacy owner 数据，其余用户数据丢弃（破坏性，
  生产回滚走备份恢复，db-migration spec）；
- 新表外键生效：悬空 (user_id, instrument_id) 关联被数据库层拦截。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "0001_duckdb_baseline"
HEAD_REVISION = "0002_multi_user_auth"
LEGACY_USERNAME = "admin"
PLACEHOLDER_HASH = "!unloginable-placeholder"


def _alembic_config(db_path: Path, script_location: Path = PROJECT_ROOT / "alembic") -> Config:
    """programmatic API：注入临时库 url 与 script_location，不依赖 CWD（同 test_migrations）。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location))
    cfg.set_main_option("sqlalchemy.url", f"duckdb:///{db_path}")
    return cfg


def _seed_baseline_data(db_path: Path) -> dict:
    """在 0001 schema 上灌入旧单用户数据（raw SQL：模型已是 v2 结构，不可用 ORM）。

    返回各表行数基准，供升级后比对。
    """
    command.upgrade(_alembic_config(db_path), BASELINE_REVISION)
    engine = sa.create_engine(f"duckdb:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO instrument (instrument_id, symbol, name, market, asset_type, "
                "is_active, created_at, updated_at) VALUES "
                "('CN:STOCK:600519', '600519', '贵州茅台', 'CN', 'STOCK', true, now(), now()), "
                "('CN:ETF:510300', '510300', '沪深300ETF', 'CN', 'ETF', true, now(), now()), "
                "('CN:INDEX:000001', '000001', '上证指数', 'CN', 'INDEX', true, now(), now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO watchlist (instrument_id, sort_order, created_at) VALUES "
                "('CN:STOCK:600519', 0, now()), ('CN:ETF:510300', 1, now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO index_watchlist (instrument_id, sort_order, created_at) "
                "VALUES ('CN:INDEX:000001', 0, now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO tag (tag_id, name, created_at, updated_at) VALUES "
                "(11, '高股息', now(), now()), (12, '白酒', now(), now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO watchlist_tag (instrument_id, tag_id) VALUES "
                "('CN:STOCK:600519', 11), ('CN:STOCK:600519', 12), ('CN:ETF:510300', 11)"
            ))
            conn.execute(sa.text(
                "INSERT INTO quote_snapshot (instrument_id, price, source, fetched_at, "
                "created_at) VALUES ('CN:STOCK:600519', 1700.5, 'test', now(), now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO app_setting (key, value, updated_at) "
                "VALUES ('seeded', 'yes', now())"
            ))
    finally:
        engine.dispose()
    return {
        "instrument": 3,
        "watchlist": 2,
        "index_watchlist": 1,
        "tag": 2,
        "watchlist_tag": 3,
        "quote_snapshot": 1,
        "app_setting": 1,
    }


def _open_engine(db_path: Path):
    return sa.create_engine(f"duckdb:///{db_path}")


def test_upgrade_from_0001_assigns_all_data_to_legacy_owner(tmp_path):
    """旧库升级：全部私有数据归属 legacy owner，关联与排序完整，全局表不动。"""
    db = tmp_path / "upgrade.duckdb"
    baseline_counts = _seed_baseline_data(db)

    command.upgrade(_alembic_config(db), "head")
    engine = _open_engine(db)
    try:
        with engine.connect() as conn:
            # 版本就位
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() \
                == HEAD_REVISION

            # legacy owner：唯一用户、admin、启用、占位哈希不可登录、强制改密
            users = conn.execute(sa.text(
                "SELECT user_id, username, role, is_active, must_change_password, password_hash "
                "FROM app_user"
            )).fetchall()
            assert len(users) == 1
            legacy_id, username, role, is_active, must_change, password_hash = users[0]
            assert username == LEGACY_USERNAME
            assert role == "admin"
            assert is_active is True
            assert must_change is True
            assert password_hash == PLACEHOLDER_HASH

            # 四张私有表行数不变，且全部归属 legacy owner
            for table, expected in (
                ("watchlist", baseline_counts["watchlist"]),
                ("index_watchlist", baseline_counts["index_watchlist"]),
                ("tag", baseline_counts["tag"]),
                ("watchlist_tag", baseline_counts["watchlist_tag"]),
            ):
                total, owned = conn.execute(sa.text(
                    f"SELECT COUNT(*), COUNT(*) FILTER (WHERE user_id = {legacy_id}) FROM {table}"
                )).one()
                assert (total, owned) == (expected, expected), table

            # 标签关联逐行保留（tag_id 不重排）
            links = {
                (row[0], row[1])
                for row in conn.execute(sa.text(
                    "SELECT wt.instrument_id, wt.tag_id FROM watchlist_tag wt "
                    f"WHERE wt.user_id = {legacy_id}"
                ))
            }
            assert links == {
                ("CN:STOCK:600519", 11),
                ("CN:STOCK:600519", 12),
                ("CN:ETF:510300", 11),
            }

            # 排序保留
            orders = dict(conn.execute(sa.text(
                "SELECT instrument_id, sort_order FROM watchlist "
                f"WHERE user_id = {legacy_id}"
            )).fetchall())
            assert orders == {"CN:STOCK:600519": 0, "CN:ETF:510300": 1}

            # 全局表不动
            for table, expected in (
                ("instrument", baseline_counts["instrument"]),
                ("quote_snapshot", baseline_counts["quote_snapshot"]),
                ("app_setting", baseline_counts["app_setting"]),
            ):
                assert conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() == expected

            # staging 表已清理
            tables = {
                row[0] for row in conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
                ))
            }
            assert not {"watchlist_stage", "index_watchlist_stage", "tag_stage",
                        "watchlist_tag_stage"} & tables
            assert {"app_user", "user_session"} <= tables
    finally:
        engine.dispose()


def test_upgrade_enforces_new_foreign_keys(tmp_path):
    """升级后新表外键生效：悬空 (user_id, instrument_id) 关联被数据库拦截。"""
    db = tmp_path / "fk.duckdb"
    _seed_baseline_data(db)
    command.upgrade(_alembic_config(db), "head")

    engine = _open_engine(db)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                # user_id=999 不存在 -> 复合外键拦截
                conn.execute(sa.text(
                    "INSERT INTO watchlist_tag (user_id, instrument_id, tag_id) "
                    "VALUES (999, 'CN:STOCK:600519', 11)"
                ))
    finally:
        engine.dispose()


def test_upgrade_failure_mid_migration_rolls_back(tmp_path):
    """校验失败整体回滚：迁移末尾注入异常 -> 库保持 0001 结构与数据。

    复制 alembic 脚本目录并篡改 0002（清理 staging 前抛异常——此时全部
    DDL 与数据拷贝已执行），验证 DuckDB 事务性 DDL 能把整段迁移回滚干净。
    """
    db = tmp_path / "rollback.duckdb"
    baseline_counts = _seed_baseline_data(db)

    tampered = tmp_path / "alembic_tampered"
    shutil.copytree(PROJECT_ROOT / "alembic", tampered)
    migration = tampered / "versions" / "0002_multi_user_auth.py"
    content = migration.read_text(encoding="utf-8")
    injected = content.replace(
        "    # --- 9. 清理 staging ---",
        '    raise RuntimeError("injected: 模拟校验后失败")\n'
        "    # --- 9. 清理 staging ---",
    )
    assert injected != content, "注入点未命中（迁移文件结构已变化）"
    migration.write_text(injected, encoding="utf-8")

    with pytest.raises(RuntimeError, match="injected"):
        command.upgrade(_alembic_config(db, tampered), "head")

    engine = _open_engine(db)
    try:
        with engine.connect() as conn:
            # 版本仍为 0001
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() \
                == BASELINE_REVISION

            # 旧结构表仍在，数据原样
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM watchlist"
            )).scalar() == baseline_counts["watchlist"]
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM watchlist_tag"
            )).scalar() == baseline_counts["watchlist_tag"]

            # 新表 / staging 表均不残留
            tables = {
                row[0] for row in conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
                ))
            }
            assert "app_user" not in tables
            assert not {"watchlist_stage", "watchlist_tag_stage"} & tables
    finally:
        engine.dispose()


def test_downgrade_to_0001_keeps_only_legacy_owner_data(tmp_path):
    """降级 0002 -> 0001：仅保留 legacy owner 的四张私有表数据，其余用户数据丢弃。"""
    db = tmp_path / "downgrade.duckdb"
    _seed_baseline_data(db)
    command.upgrade(_alembic_config(db), "head")

    # 升级后追加第二个用户及其自选与标签（验证降级丢弃非 legacy 数据）
    engine = _open_engine(db)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO app_user (username, password_hash, role, is_active, "
                "must_change_password, created_at, updated_at) "
                "VALUES ('bob', 'x', 'user', true, false, now(), now())"
            ))
            bob_id = conn.execute(sa.text(
                "SELECT user_id FROM app_user WHERE username = 'bob'"
            )).scalar_one()
            conn.execute(sa.text(
                "INSERT INTO watchlist (user_id, instrument_id, sort_order, created_at) "
                f"VALUES ({bob_id}, 'CN:STOCK:600519', 0, now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO tag (tag_id, user_id, name, created_at, updated_at) "
                f"VALUES (99, {bob_id}, 'bob专属', now(), now())"
            ))
    finally:
        engine.dispose()

    command.downgrade(_alembic_config(db), BASELINE_REVISION)

    engine = _open_engine(db)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() \
                == BASELINE_REVISION
            # 旧结构：单列主键，仅 legacy owner 的两只证券，排序逐行保留
            rows = dict(conn.execute(sa.text(
                "SELECT instrument_id, sort_order FROM watchlist"
            )).fetchall())
            assert rows == {"CN:STOCK:600519": 0, "CN:ETF:510300": 1}
            # legacy owner 的指数配置 / 标签 / 标签关联同样保留（非 bob 的）
            assert [r[0] for r in conn.execute(
                sa.text("SELECT instrument_id FROM index_watchlist")
            )] == ["CN:INDEX:000001"]
            tags = {r[0] for r in conn.execute(sa.text("SELECT tag_id FROM tag"))}
            assert tags == {11, 12}
            links = {
                (r[0], r[1])
                for r in conn.execute(sa.text(
                    "SELECT instrument_id, tag_id FROM watchlist_tag"
                ))
            }
            assert links == {
                ("CN:STOCK:600519", 11),
                ("CN:STOCK:600519", 12),
                ("CN:ETF:510300", 11),
            }
            # 身份域表与 downgrade staging 均已删除
            tables = {
                row[0] for row in conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
                ))
            }
            assert "app_user" not in tables and "user_session" not in tables
            assert not {"watchlist_down_stage", "tag_down_stage"} & tables
    finally:
        engine.dispose()
