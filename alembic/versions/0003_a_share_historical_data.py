"""A股历史数据（a-share-historical-data）：主档/事实/同步控制表 + trading_calendar 扩展。

结构变更（技术方案 §60.1，db-migration spec）：
- trading_calendar 增加 4 个可空列（exchange/pretrade_date/source/fetched_at），
  简单 ALTER ADD COLUMN——旧 CalendarRepository 写路径只写三列，行为不变（§60.2）；
- 新增主档表 cn_stock_basic / cn_stock_company / cn_stock_company 的
  cn_stock_name_change（ORM 小表，含 instrument FK）；
- 新增四张日级事实表（SQLAlchemy Core 风格：无物理主键/外键/索引，
  业务唯一键 (instrument_id, trade_date) 由整日替换 + 账本保证）；
- 新增四张同步控制表（state/day_status/run/run_dataset）。

约束：
- 不修改、不删除 fundamental_snapshot，不迁移旧 fundamental 数据；
- 纯增量 DDL：既有 12 张表行数迁移前后不变（显式校验）。

Revision ID: 0003_a_share_historical_data
Revises: 0002_multi_user_auth
Create Date: 2026-09-17
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_a_share_historical_data"
down_revision: Union[str, None] = "0002_multi_user_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 迁移前已存在的全部业务表（升级前后行数必须不变）
EXISTING_TABLES = (
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
)


class MigrationVerificationError(RuntimeError):
    """校验失败：抛出使 alembic 事务整体回滚。"""


def _count(conn, table: str) -> int:
    return conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise MigrationVerificationError(f"迁移校验失败: {message}")


def _fact_columns(extra: list[sa.Column]) -> list[sa.Column]:
    """事实表公共列 + 数据集专有列；公共尾列 source/fetched_at。"""
    return [
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("ts_code", sa.String(length=16), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        *extra,
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _master_meta() -> list[sa.Column]:
    return [
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sync_run_id", sa.String(length=64), nullable=True),
    ]


def upgrade() -> None:
    conn = op.get_bind()
    counts_before = {t: _count(conn, t) for t in EXISTING_TABLES}

    # --- 1. trading_calendar 扩展（可空加列，简单 ALTER） ---
    op.add_column("trading_calendar", sa.Column("exchange", sa.String(length=16), nullable=True))
    op.add_column("trading_calendar", sa.Column("pretrade_date", sa.Date(), nullable=True))
    op.add_column("trading_calendar", sa.Column("source", sa.String(length=32), nullable=True))
    op.add_column(
        "trading_calendar", sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True)
    )

    # --- 2. 主档表（技术方案 §8~§10） ---
    op.create_table(
        "cn_stock_basic",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("ts_code", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("area", sa.String(length=32), nullable=True),
        sa.Column("industry", sa.String(length=64), nullable=True),
        sa.Column("fullname", sa.String(length=128), nullable=True),
        sa.Column("enname", sa.String(length=256), nullable=True),
        sa.Column("cnspell", sa.String(length=64), nullable=True),
        sa.Column("market", sa.String(length=16), nullable=True),
        sa.Column("exchange", sa.String(length=16), nullable=True),
        sa.Column("curr_type", sa.String(length=8), nullable=True),
        sa.Column("list_status", sa.String(length=8), nullable=True),
        sa.Column("list_date", sa.Date(), nullable=True),
        sa.Column("delist_date", sa.Date(), nullable=True),
        sa.Column("is_hs", sa.String(length=8), nullable=True),
        sa.Column("act_name", sa.String(length=256), nullable=True),
        sa.Column("act_ent_type", sa.String(length=64), nullable=True),
        sa.Column("source_last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.instrument_id"], name="fk_cn_stock_basic_instrument"
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
        *_master_meta(),
    )
    op.create_table(
        "cn_stock_company",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("ts_code", sa.String(length=16), nullable=False),
        sa.Column("com_name", sa.String(length=256), nullable=True),
        sa.Column("com_id", sa.String(length=64), nullable=True),
        sa.Column("exchange", sa.String(length=16), nullable=True),
        sa.Column("chairman", sa.String(length=64), nullable=True),
        sa.Column("manager", sa.String(length=64), nullable=True),
        sa.Column("secretary", sa.String(length=64), nullable=True),
        sa.Column("reg_capital", sa.Double(), nullable=True),
        sa.Column("setup_date", sa.Date(), nullable=True),
        sa.Column("province", sa.String(length=32), nullable=True),
        sa.Column("city", sa.String(length=32), nullable=True),
        sa.Column("introduction", sa.Text(), nullable=True),
        sa.Column("website", sa.String(length=256), nullable=True),
        sa.Column("email", sa.String(length=128), nullable=True),
        sa.Column("office", sa.Text(), nullable=True),
        sa.Column("employees", sa.Integer(), nullable=True),
        sa.Column("main_business", sa.Text(), nullable=True),
        sa.Column("business_scope", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_cn_stock_company_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
        *_master_meta(),
    )
    op.create_table(
        "cn_stock_name_change",
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("ts_code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("ann_date", sa.Date(), nullable=True),
        sa.Column("change_reason", sa.String(length=256), nullable=True),
        sa.PrimaryKeyConstraint("event_key"),
        *_master_meta(),
    )

    # --- 3. 四张日级事实表（无 PK/FK/索引，技术方案 §13~§16） ---
    op.create_table(
        "market_daily_bar",
        *_fact_columns(
            [
                sa.Column("open", sa.Double(), nullable=True),
                sa.Column("high", sa.Double(), nullable=True),
                sa.Column("low", sa.Double(), nullable=True),
                sa.Column("close", sa.Double(), nullable=True),
                sa.Column("pre_close", sa.Double(), nullable=True),
                sa.Column("change", sa.Double(), nullable=True),
                sa.Column("pct_chg", sa.Double(), nullable=True),
                sa.Column("vol", sa.Double(), nullable=True),
                sa.Column("amount", sa.Double(), nullable=True),
                sa.Column("ah_vol", sa.Double(), nullable=True),
                sa.Column("ah_amount", sa.Double(), nullable=True),
            ]
        ),
    )
    op.create_table(
        "market_adj_factor",
        *_fact_columns([sa.Column("adj_factor", sa.Double(), nullable=False)]),
    )
    op.create_table(
        "market_daily_basic",
        *_fact_columns(
            [
                sa.Column("close", sa.Double(), nullable=True),
                sa.Column("turnover_rate", sa.Double(), nullable=True),
                sa.Column("turnover_rate_f", sa.Double(), nullable=True),
                sa.Column("volume_ratio", sa.Double(), nullable=True),
                sa.Column("pe", sa.Double(), nullable=True),
                sa.Column("pe_ttm", sa.Double(), nullable=True),
                sa.Column("pb", sa.Double(), nullable=True),
                sa.Column("ps", sa.Double(), nullable=True),
                sa.Column("ps_ttm", sa.Double(), nullable=True),
                sa.Column("dv_ratio", sa.Double(), nullable=True),
                sa.Column("dv_ttm", sa.Double(), nullable=True),
                sa.Column("total_share", sa.Double(), nullable=True),
                sa.Column("float_share", sa.Double(), nullable=True),
                sa.Column("free_share", sa.Double(), nullable=True),
                sa.Column("total_mv", sa.Double(), nullable=True),
                sa.Column("circ_mv", sa.Double(), nullable=True),
                sa.Column("limit_status", sa.SmallInteger(), nullable=True),
            ]
        ),
    )
    op.create_table(
        "market_moneyflow",
        *_fact_columns(
            [
                sa.Column("buy_sm_vol", sa.BigInteger(), nullable=True),
                sa.Column("buy_sm_amount", sa.Double(), nullable=True),
                sa.Column("sell_sm_vol", sa.BigInteger(), nullable=True),
                sa.Column("sell_sm_amount", sa.Double(), nullable=True),
                sa.Column("buy_md_vol", sa.BigInteger(), nullable=True),
                sa.Column("buy_md_amount", sa.Double(), nullable=True),
                sa.Column("sell_md_vol", sa.BigInteger(), nullable=True),
                sa.Column("sell_md_amount", sa.Double(), nullable=True),
                sa.Column("buy_lg_vol", sa.BigInteger(), nullable=True),
                sa.Column("buy_lg_amount", sa.Double(), nullable=True),
                sa.Column("sell_lg_vol", sa.BigInteger(), nullable=True),
                sa.Column("sell_lg_amount", sa.Double(), nullable=True),
                sa.Column("buy_elg_vol", sa.BigInteger(), nullable=True),
                sa.Column("buy_elg_amount", sa.Double(), nullable=True),
                sa.Column("sell_elg_vol", sa.BigInteger(), nullable=True),
                sa.Column("sell_elg_amount", sa.Double(), nullable=True),
                sa.Column("net_mf_vol", sa.BigInteger(), nullable=True),
                sa.Column("net_mf_amount", sa.Double(), nullable=True),
            ]
        ),
    )

    # --- 4. 同步控制表（技术方案 §17~§20） ---
    op.create_table(
        "history_sync_state",
        sa.Column("dataset", sa.String(length=32), nullable=False),
        sa.Column("dataset_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("history_start_date", sa.Date(), nullable=True),
        sa.Column("latest_complete_trade_date", sa.Date(), nullable=True),
        sa.Column("latest_expected_trade_date", sa.Date(), nullable=True),
        sa.Column("current_trade_date", sa.Date(), nullable=True),
        sa.Column(
            "current_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("master_cursor", sa.String(length=64), nullable=True),
        sa.Column(
            "bootstrap_complete", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "record_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("data_min_date", sa.Date(), nullable=True),
        sa.Column("data_max_date", sa.Date(), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("dataset"),
    )
    op.create_table(
        "history_day_status",
        sa.Column("dataset", sa.String(length=32), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_by_run_id", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("dataset", "trade_date"),
    )
    op.create_table(
        "history_sync_run",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_table(
        "history_sync_run_dataset",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("dataset", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("start_watermark", sa.Date(), nullable=True),
        sa.Column("target_trade_date", sa.Date(), nullable=True),
        sa.Column("end_watermark", sa.Date(), nullable=True),
        sa.Column("start_cursor", sa.String(length=64), nullable=True),
        sa.Column("end_cursor", sa.String(length=64), nullable=True),
        sa.Column(
            "dates_completed", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "rows_written", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "request_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("failed_trade_date", sa.Date(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("run_id", "dataset"),
    )

    # --- 5. 纯增量校验：既有表行数不变（db-migration spec） ---
    for table in EXISTING_TABLES:
        after = _count(conn, table)
        _require(
            after == counts_before[table],
            f"既有表 {table} 行数在迁移中发生变化: {counts_before[table]} -> {after}",
        )


def downgrade() -> None:
    """降级：删除 11 张新表并移除 trading_calendar 扩展列（数据丢弃，
    生产环境破坏性回滚应走数据库文件备份恢复，db-migration spec）。"""
    for table in (
        "history_sync_run_dataset",
        "history_sync_run",
        "history_day_status",
        "history_sync_state",
        "market_moneyflow",
        "market_daily_basic",
        "market_adj_factor",
        "market_daily_bar",
        "cn_stock_name_change",
        "cn_stock_company",
        "cn_stock_basic",
    ):
        op.drop_table(table)

    op.drop_column("trading_calendar", "fetched_at")
    op.drop_column("trading_calendar", "source")
    op.drop_column("trading_calendar", "pretrade_date")
    op.drop_column("trading_calendar", "exchange")
