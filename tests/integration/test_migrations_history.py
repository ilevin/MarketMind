"""0003_a_share_historical_data 迁移集成测试（db-migration spec / tasks 2.5）。

覆盖 v0.2.0 状态库升级到 0003 的完整链路：
- 0002 schema + 真实数据（用户/自选/估值/日历/任务状态）→ upgrade head →
  既有表行数与内容不变，11 张新表存在可查询，trading_calendar 新列为 NULL；
- 事实表建表形态：无主键/外键/二级索引，日级列类型抽查（DOUBLE/BIGINT/SMALLINT）；
- 控制表主键形态：history_day_status (dataset, trade_date)、
  history_sync_run_dataset (run_id, dataset)；
- 主档表外键生效：cn_stock_basic 悬空 instrument_id 被数据库层拦截；
- 模型 metadata 与迁移产物一致由 test_migrations.test_alembic_head_matches_models_schema
  防漂移测试覆盖（含四张 Core 事实表）；
- 降级 0003 -> 0002：新表与扩展列全部消失，既有数据不动。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_REVISION = "0002_multi_user_auth"
HEAD_REVISION = "0003_a_share_historical_data"

NEW_TABLES = (
    "cn_stock_basic",
    "cn_stock_company",
    "cn_stock_name_change",
    "market_daily_bar",
    "market_adj_factor",
    "market_daily_basic",
    "market_moneyflow",
    "history_sync_state",
    "history_day_status",
    "history_sync_run",
    "history_sync_run_dataset",
)

CALENDAR_NEW_COLUMNS = ("exchange", "pretrade_date", "source", "fetched_at")


def _alembic_config(db_path: Path) -> Config:
    """programmatic API：注入临时库 url 与 script_location，不依赖 CWD（同 test_migrations）。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"duckdb:////{db_path}")
    return cfg


def _seed_v020_data(db_path: Path) -> dict[str, int]:
    """在 0002 schema 上灌入 v0.2.0 真实数据（raw SQL，绕过 ORM 依赖）。

    返回各表行数基准，供升级后比对。
    """
    command.upgrade(_alembic_config(db_path), BASE_REVISION)
    engine = sa.create_engine(f"duckdb:////{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO app_user (username, password_hash, role, is_active, "
                "must_change_password, created_at, updated_at) VALUES "
                "('admin', 'x', 'admin', true, false, now(), now()), "
                "('alice', 'x', 'user', true, false, now(), now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO instrument (instrument_id, symbol, name, market, asset_type, "
                "currency, is_active, created_at, updated_at) VALUES "
                "('CN:STOCK:600519', '600519', '贵州茅台', 'CN', 'STOCK', 'CNY', true, now(), now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO watchlist (user_id, instrument_id, sort_order, created_at) "
                "SELECT user_id, 'CN:STOCK:600519', 0, now() FROM app_user WHERE username = 'alice'"
            ))
            conn.execute(sa.text(
                "INSERT INTO fundamental_snapshot (instrument_id, trade_date, pe_ttm, pb, "
                "dividend_yield_ttm, source, fetched_at, created_at) VALUES "
                "('CN:STOCK:600519', '2026-09-10', 25.5, 8.2, 3.1, 'tushare', now(), now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO trading_calendar (market, trade_date, is_open) VALUES "
                "('CN', '2026-09-10', true), ('CN', '2026-09-11', true), "
                "('CN', '2026-09-12', false), ('HK', '2026-09-10', true)"
            ))
            conn.execute(sa.text(
                "INSERT INTO job_status (job_name, last_started_at, last_success_at, "
                "last_duration_ms, consecutive_failures, updated_at) VALUES "
                "('fundamental_refresh', now(), now(), 120, 0, now())"
            ))
        tables = [
            "app_user", "instrument", "watchlist", "fundamental_snapshot",
            "trading_calendar", "job_status",
        ]
        with engine.connect() as conn:
            return {
                t: conn.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
                for t in tables
            }
    finally:
        engine.dispose()


def _columns(engine, table: str) -> dict[str, tuple]:
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = :t"
        ), {"t": table})
        return {r[0]: (r[1], r[2]) for r in rows}


def _constraints(engine, table: str) -> set[str]:
    """DuckDB 通过 duckdb_constraints() 列出表上全部约束名。"""
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT constraint_type FROM duckdb_constraints() "
            "WHERE schema_name = 'main' AND table_name = :t"
        ), {"t": table})
        return {r[0] for r in rows}


def test_upgrade_from_0002_preserves_existing_data(tmp_path):
    """v0.2.0 状态库升级：既有数据行数不变、新表就绪、日历新列为 NULL。"""
    db = tmp_path / "hist.duckdb"
    baseline = _seed_v020_data(db)
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:////{db}")
    try:
        # 既有表行数不变
        with engine.connect() as conn:
            for table, expected in baseline.items():
                actual = conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table}")
                ).scalar_one()
                assert actual == expected, f"{table} 行数变化: {expected} -> {actual}"

        # 既有内容抽查：fundamental_snapshot 未被触碰
        with engine.connect() as conn:
            row = conn.execute(sa.text(
                "SELECT instrument_id, trade_date, pe_ttm FROM fundamental_snapshot"
            )).one()
        assert row[0] == "CN:STOCK:600519"
        assert row[1] == date(2026, 9, 10)
        assert row[2] == 25.5

        # 新表存在且为空、可查询
        with engine.connect() as conn:
            tables = {
                r[0] for r in conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
                ))
            }
        assert set(NEW_TABLES) <= tables
        for table in NEW_TABLES:
            with engine.connect() as conn:
                count = conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table}")
                ).scalar_one()
            assert count == 0

        # trading_calendar 新列存在且旧行全为 NULL
        cal = _columns(engine, "trading_calendar")
        for col in CALENDAR_NEW_COLUMNS:
            assert col in cal
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT exchange, pretrade_date, source, fetched_at FROM trading_calendar"
            )).fetchall()
        assert len(rows) == baseline["trading_calendar"]
        assert all(r == (None, None, None, None) for r in rows)
    finally:
        engine.dispose()


def test_fact_table_shape_no_pk_fk_index(tmp_path):
    """事实表建表形态：列齐全、无主键/外键/唯一约束（技术方案 §12~§16）。"""
    db = tmp_path / "fact.duckdb"
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:////{db}")
    try:
        expected_columns = {
            "market_daily_bar": {
                "instrument_id", "ts_code", "trade_date", "open", "high", "low", "close",
                "pre_close", "change", "pct_chg", "vol", "amount", "ah_vol", "ah_amount",
                "source", "fetched_at",
            },
            "market_adj_factor": {
                "instrument_id", "ts_code", "trade_date", "adj_factor", "source", "fetched_at",
            },
            "market_daily_basic": {
                "instrument_id", "ts_code", "trade_date", "close", "turnover_rate",
                "turnover_rate_f", "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm",
                "dv_ratio", "dv_ttm", "total_share", "float_share", "free_share", "total_mv",
                "circ_mv", "limit_status", "source", "fetched_at",
            },
            "market_moneyflow": {
                "instrument_id", "ts_code", "trade_date",
                "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount",
                "buy_md_vol", "buy_md_amount", "sell_md_vol", "sell_md_amount",
                "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount",
                "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
                "net_mf_vol", "net_mf_amount", "source", "fetched_at",
            },
        }
        for table, columns in expected_columns.items():
            actual = set(_columns(engine, table))
            assert actual == columns, f"{table} 列差异: 缺 {columns - actual} 多 {actual - columns}"
            constraints = _constraints(engine, table)
            assert constraints == {"NOT NULL"}, (
                f"{table} 不应有主键/外键/唯一约束，实际: {constraints}"
            )

        # 类型抽查（技术方案 §5.3）：DOUBLE / BIGINT / SMALLINT / DATE
        daily_types = {c: _columns(engine, "market_daily_bar")[c][1] for c in ("open", "vol")}
        assert all(t.upper() == "DOUBLE" for t in daily_types.values())
        mf_types = {c: _columns(engine, "market_moneyflow")[c][1] for c in ("buy_sm_vol", "net_mf_amount")}
        assert mf_types["buy_sm_vol"].upper() == "BIGINT"
        assert mf_types["net_mf_amount"].upper() == "DOUBLE"
        db_types = _columns(engine, "market_daily_basic")
        assert db_types["limit_status"][1].upper() == "SMALLINT"
        assert db_types["trade_date"][1].upper() == "DATE"
        assert db_types["total_mv"][1].upper() == "DOUBLE"
    finally:
        engine.dispose()


def test_control_table_primary_keys(tmp_path):
    """控制表主键形态：day_status (dataset, trade_date)、run_dataset (run_id, dataset)。"""
    db = tmp_path / "ctrl.duckdb"
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:////{db}")
    try:
        with engine.connect() as conn:
            for table, pk_cols in (
                ("history_day_status", {"dataset", "trade_date"}),
                ("history_sync_run_dataset", {"run_id", "dataset"}),
                ("history_sync_state", {"dataset"}),
                ("history_sync_run", {"run_id"}),
                ("cn_stock_name_change", {"event_key"}),
            ):
                rows = conn.execute(sa.text(
                    "SELECT constraint_column_names FROM duckdb_constraints() "
                    "WHERE schema_name = 'main' AND table_name = :t "
                    "AND constraint_type = 'PRIMARY KEY'"
                ), {"t": table}).fetchall()
                assert len(rows) == 1, f"{table} 应恰有一个主键约束"
                assert set(rows[0][0]) == pk_cols, f"{table} 主键列: {rows[0][0]}"
    finally:
        engine.dispose()


def test_master_table_fk_blocks_dangling_instrument(tmp_path):
    """主档外键：cn_stock_basic 引用不存在的 instrument 被数据库层拦截。"""
    from sqlalchemy.exc import IntegrityError as FKError

    db = tmp_path / "fk.duckdb"
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:////{db}")
    try:
        with pytest.raises(FKError):
            with engine.begin() as conn:
                conn.execute(sa.text(
                    "INSERT INTO cn_stock_basic (instrument_id, ts_code, symbol, source, "
                    "fetched_at, source_last_seen_at) VALUES "
                    "('CN:STOCK:999999', '999999.SH', '999999', 'tushare', now(), now())"
                ))
    finally:
        engine.dispose()


def test_downgrade_0003_to_0002_keeps_existing_data(tmp_path):
    """降级 0003 -> 0002：新表与扩展列消失，既有数据不动（破坏性回滚走备份）。"""
    db = tmp_path / "down.duckdb"
    baseline = _seed_v020_data(db)
    command.upgrade(_alembic_config(db), "head")
    command.downgrade(_alembic_config(db), BASE_REVISION)

    engine = sa.create_engine(f"duckdb:////{db}")
    try:
        with engine.connect() as conn:
            tables = {
                r[0] for r in conn.execute(sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
                ))
            }
        assert not (set(NEW_TABLES) & tables), f"降级后残留新表: {set(NEW_TABLES) & tables}"

        cal = _columns(engine, "trading_calendar")
        assert not (set(CALENDAR_NEW_COLUMNS) & set(cal)), "降级后日历扩展列残留"

        with engine.connect() as conn:
            for table, expected in baseline.items():
                actual = conn.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table}")
                ).scalar_one()
                assert actual == expected, f"降级后 {table} 行数变化: {expected} -> {actual}"
    finally:
        engine.dispose()


def test_sync_state_defaults_applied(tmp_path):
    """同步控制表 DEFAULT 列：插入最小行时不提供计数列亦为 0/false。"""
    db = tmp_path / "defaults.duckdb"
    command.upgrade(_alembic_config(db), "head")

    engine = sa.create_engine(f"duckdb:////{db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO history_sync_state (dataset, dataset_kind, status, updated_at) "
                "VALUES ('daily', 'DAILY_CONTIGUOUS', 'UNINITIALIZED', now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO history_sync_run (run_id, trigger_type, status, started_at, "
                "created_at) VALUES ('r1', 'MANUAL', 'RUNNING', now(), now())"
            ))
            conn.execute(sa.text(
                "INSERT INTO history_sync_run_dataset (run_id, dataset, status, started_at) "
                "VALUES ('r1', 'daily', 'RUNNING', now())"
            ))
        with engine.connect() as conn:
            state = conn.execute(sa.text(
                "SELECT current_attempt, bootstrap_complete, record_count "
                "FROM history_sync_state WHERE dataset = 'daily'"
            )).one()
            rd = conn.execute(sa.text(
                "SELECT dates_completed, rows_written, request_count, retry_count "
                "FROM history_sync_run_dataset WHERE run_id = 'r1' AND dataset = 'daily'"
            )).one()
        assert state == (0, False, 0)
        assert rd == (0, 0, 0, 0)
    finally:
        engine.dispose()
