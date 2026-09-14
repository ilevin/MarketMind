"""Alembic 迁移集成测试（DuckDB 版，技术方案 §9 / design D2/D4/D5）。

覆盖：
- 空库全链建库：alembic programmatic API 跑 ``upgrade head``，断言 12 张表
  （含 multi-user-auth 的 app_user / user_session）与 sequence 全部就绪；
- 版本记录正确：``alembic_version.version_num == "0002_multi_user_auth"``；
- 回滚：``downgrade base`` 后核心表与 sequence 全部消失；
- 外键 RESTRICT：被引用的 instrument 行删除被数据库层拦截（design D4，
  无级联删除，数据库兜底）；
- 列精度抽查：``quote_snapshot.price`` 为 ``DECIMAL(20,6)``，防类型漂移。

multi-user-auth 升级/降级的旧数据归属与回滚用例见 test_migrations_multi_user.py。

注意：DuckDB 基线是一次性建表，不继承 stocksview 的 SQLite 迁移历史
（v0.02/v0.03 搬迁用例废弃——历史链已移除）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# head（0002）应创建的全部 12 张业务表（不含 alembic_version 系统表）
EXPECTED_TABLES = {
    "instrument",
    "watchlist",
    "index_watchlist",
    "tag",
    "watchlist_tag",
    "quote_snapshot",
    "fundamental_snapshot",
    "trading_calendar",
    "job_status",
    "app_setting",
    "app_user",
    "user_session",
}
HEAD_REVISION = "0002_multi_user_auth"


def _alembic_config(db_path: Path) -> Config:
    """programmatic API：注入临时库 url 与 script_location，不依赖 CWD。

    直接用 ``Config()`` 空构造，手动设置两项关键配置，避免
    ``%(here)s/alembic`` 在非项目目录 CWD 下解析错位。不加载 alembic.ini：
    env.py 的 fileConfig 会替换 root logger 的 handlers（并禁用已有
    logger），导致同进程后续测试的 caplog 失效。
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"duckdb:///{db_path}")
    return cfg


def _list_tables(engine) -> set[str]:
    """通过 DuckDB ``information_schema.tables`` 列出当前所有用户表名。"""
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
        ))
        return {r[0] for r in rows}


def test_empty_db_upgrade_head_creates_all_tables(tmp_path):
    """空库 upgrade head：12 张业务表 + alembic_version 系统表全部就位。

    验证 design D2（Alembic 管理 schema 演进）与迁移链（0001 -> 0002）的完整性；
    防止遗漏表或 sequence 导致运行时建表失败。
    """
    db = tmp_path / "mig.duckdb"
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:///{db}")
    try:
        tables = _list_tables(engine)
        # 业务表 12 张 + 版本表 1 张
        assert EXPECTED_TABLES.issubset(tables), f"缺失表: {EXPECTED_TABLES - tables}"
        assert "alembic_version" in tables

        # sequence 存在（tag_id / user_id 取号依赖，design D3：显式 sequence 取代自增主键）
        # 注：duckdb_sequences() 的列名是 schema_name（不是 information_schema 风格的 sequence_schema）
        seqs = engine.connect().execute(sa.text(
            "SELECT sequence_name FROM duckdb_sequences() WHERE schema_name = 'main'"
        )).fetchall()
        seq_names = {s[0] for s in seqs}
        assert {"seq_tag_id", "seq_user_id"} <= seq_names
    finally:
        engine.dispose()


def test_alembic_version_points_to_head_revision(tmp_path):
    """upgrade head 后 alembic_version 记录为当前 head 版本号。

    防止 revision 命名漂移或 head 指向错误；生产环境升级前会核对该值。
    """
    db = tmp_path / "mig.duckdb"
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:///{db}")
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
        assert version == HEAD_REVISION
    finally:
        engine.dispose()


def test_downgrade_base_removes_all_tables_and_sequence(tmp_path):
    """upgrade -> downgrade base：业务表、版本表、sequence 全部消失。

    验证 downgrade 脚本的完整性（破坏性回滚的最后手段）；
    生产中实际回滚走备份恢复，此处仅确保脚本逻辑自洽。
    """
    db = tmp_path / "mig.duckdb"
    cfg = _alembic_config(db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = sa.create_engine(f"duckdb:///{db}")
    try:
        tables = _list_tables(engine)
        # 业务表应全部清空；alembic_version 在 downgrade base 后也应无记录
        # （alembic 本身不会删版本表，但行数为 0；这里以无业务表为准）
        leftover = EXPECTED_TABLES & tables
        assert not leftover, f"downgrade 后残留表: {leftover}"

        # sequence 也应被删除（duckdb_sequences() 列名为 schema_name）
        seqs = engine.connect().execute(sa.text(
            "SELECT sequence_name FROM duckdb_sequences() WHERE schema_name = 'main'"
        )).fetchall()
        assert {"seq_tag_id", "seq_user_id"} & {s[0] for s in seqs} == set()
    finally:
        engine.dispose()


def test_foreign_key_restrict_blocks_deleting_referenced_instrument(tmp_path):
    """外键 RESTRICT：被 watchlist + watchlist_tag 引用的 instrument 无法直接删除。

    验证 design D4（无级联删除，数据库层 RESTRICT 兜底）；
    业务层会先做引用计数检查，此处确认数据库第二层保护有效。

    注：DuckDB 外键冲突抛出的具体异常类型可能随方言版本不同而变化
    （``IntegrityError`` 或底层 ``ConstraintException``）。Phase 0 环境
    就绪后校准精确断言；若失败可放宽为 ``pytest.raises(Exception)``。
    """
    from sqlalchemy.exc import IntegrityError

    from app.db import create_db_engine, init_db, make_session_factory
    from app.models import AppUser, Instrument, Tag, Watchlist, WatchlistTag

    db = tmp_path / "fk.duckdb"
    engine = create_db_engine(f"duckdb:///{db}")
    init_db(engine)
    factory = make_session_factory(engine)

    try:
        # 构造关联链：user -> instrument -> watchlist -> watchlist_tag <- tag
        with factory() as session:
            user = AppUser(username="alice", password_hash="x")
            session.add(user)
            session.add(
                Instrument(
                    instrument_id="CN:STOCK:600519",
                    symbol="600519",
                    name="贵州茅台",
                    market="CN",
                    asset_type="STOCK",
                    currency="CNY",
                )
            )
            session.flush()
            tag = Tag(user_id=user.user_id, name="高股息")
            session.add(tag)
            session.flush()
            session.add(Watchlist(user_id=user.user_id, instrument_id="CN:STOCK:600519", sort_order=0))
            session.flush()
            session.add(
                WatchlistTag(
                    user_id=user.user_id, instrument_id="CN:STOCK:600519", tag_id=tag.tag_id
                )
            )
            session.commit()

        # 直接删除被引用的 instrument 应被外键拦截
        with factory() as session:
            instr = session.get(Instrument, "CN:STOCK:600519")
            session.delete(instr)
            with pytest.raises(IntegrityError):
                session.flush()
    finally:
        engine.dispose()


def test_quote_snapshot_price_column_precision(tmp_path):
    """建表结构抽查：quote_snapshot.price 为 DECIMAL(20,6)。

    防止迁移脚本与模型在精度定义上漂移；价格字段精度直接影响
    高低价/涨跌幅的计算准确性（design D5：定点数替代浮点）。
    """
    db = tmp_path / "precision.duckdb"
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:///{db}")
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_schema = 'main' "
                "  AND table_name = 'quote_snapshot' "
                "  AND column_name = 'price'"
            )).fetchone()
        assert row is not None
        # DuckDB 的 information_schema.data_type 带精度后缀（如 'DECIMAL(20,6)'），
        # 精确标度以 numeric_precision / numeric_scale 为准
        assert row[0].upper().startswith("DECIMAL")
        assert row[1] == 20
        assert row[2] == 6
    finally:
        engine.dispose()


def test_alembic_head_matches_models_schema(tmp_path):
    """防漂移：Alembic head 与模型 create_all 建出的库全表全列一致。

    生产建表走 Alembic（容器 CMD `alembic upgrade head`），测试大多走
    init_db（create_all）——两路径 schema 若漂移，"测试全绿但生产缺列"。
    已知可接受差异（白名单，逐一说明而非忽略整列）：
    - alembic_version 表仅 Alembic 路径有；
    - app_user.user_id / tag.tag_id 的 column_default：Alembic 写入
      nextval('seq_*')，create_all 不写（ORM 端经 Sequence 客户端取号，
      两条路径运行时行为等价）。
    """
    from app.db import create_db_engine, init_db

    alembic_db = tmp_path / "alembic.duckdb"
    command.upgrade(_alembic_config(alembic_db), "head")

    model_db = tmp_path / "models.duckdb"
    engine = create_db_engine(f"duckdb:///{model_db}")
    try:
        init_db(engine)
    finally:
        engine.dispose()

    def columns_of(db_path) -> set[tuple]:
        e = sa.create_engine(f"duckdb:///{db_path}")
        try:
            with e.connect() as conn:
                return {
                    tuple(r) for r in conn.execute(sa.text(
                        "SELECT table_name, column_name, is_nullable, column_default, "
                        "data_type FROM information_schema.columns "
                        "WHERE table_schema = 'main'"
                    ))
                }
        finally:
            e.dispose()

    alembic_only = columns_of(alembic_db) - columns_of(model_db)
    models_only = columns_of(model_db) - columns_of(alembic_db)

    assert alembic_only == {
        ("alembic_version", "version_num", "NO", None, "VARCHAR"),
        ("app_user", "user_id", "NO", "nextval('seq_user_id')", "BIGINT"),
        ("tag", "tag_id", "NO", "nextval('seq_tag_id')", "BIGINT"),
    }
    assert models_only == {
        ("app_user", "user_id", "NO", None, "BIGINT"),
        ("tag", "tag_id", "NO", None, "BIGINT"),
    }
