"""ETF 数据模块三张新表（etf-data-module，design D2/D9）。

结构变更（db-migration spec 0005 迁移）：
- 新增 ``cn_etf_basic``（ETF 业务主档，instrument_id 主键 +
  FK→instrument.instrument_id，约束命名 ``fk_cn_etf_basic_instrument``）；
- 新增 ``etf_daily``（东方财富原始日线，Core Table：无物理主键/FK/二级索引）；
- 新增 ``etf_adj_factor``（Tushare fund_adj 复权因子，同上）。

约束（design D9）：
- 不修改/删除任何既有表、列、sequence 与数据（含 ``stock_sync_state``——
  ETF 数据集按 (dataset, instrument_id) 键空间补建状态行属运行时行为）；
- 不写入任何业务数据行——新表为空可查询，universe 与状态行由首轮同步建立；
- 幂等可重放（对已迁移库跳过）；既有表行数不变的纯增量校验，
  校验失败整体回滚；downgrade 仅提供 DDL 逆操作。

Revision ID: 0005_etf_data_module
Revises: 0004_per_stock_history_sync
Create Date: 2026-10-08
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_etf_data_module"
down_revision: Union[str, None] = "0004_per_stock_history_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

# 迁移前已存在的全部业务表（升级前后行数必须不变，0004 后共 25 张）
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
    "stock_sync_state",
    "sync_task",
)


class MigrationVerificationError(RuntimeError):
    """校验失败：抛出使 alembic 事务整体回滚。"""


def _count(conn, table: str) -> int:
    return conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise MigrationVerificationError(f"迁移校验失败: {message}")


def _table_exists(conn, table: str) -> bool:
    """DuckDB information_schema 检查表是否存在。"""
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_name = :t"
        ),
        {"t": table},
    ).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()

    # --- 前置：记录既有表行数（纯增量校验用） ---
    counts_before = {t: _count(conn, t) for t in EXISTING_TABLES}

    # --- 1. cn_etf_basic（ETF 业务主档，幂等：已存在则跳过建表） ---
    if not _table_exists(conn, "cn_etf_basic"):
        op.create_table(
            "cn_etf_basic",
            sa.Column("instrument_id", sa.String(length=64), nullable=False),
            sa.Column("ts_code", sa.String(length=16), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=True),
            sa.Column("exchange", sa.String(length=16), nullable=True),
            sa.Column("list_date", sa.Date(), nullable=True),
            sa.Column("delist_date", sa.Date(), nullable=True),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "source_last_seen_at", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column("sync_run_id", sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint("instrument_id"),
            sa.ForeignKeyConstraint(
                ["instrument_id"],
                ["instrument.instrument_id"],
                name="fk_cn_etf_basic_instrument",
            ),
        )

    # --- 2. etf_daily（东财原始日线，Core 风格：无主键/FK/索引） ---
    if not _table_exists(conn, "etf_daily"):
        op.create_table(
            "etf_daily",
            sa.Column("instrument_id", sa.String(length=64), nullable=False),
            sa.Column("ts_code", sa.String(length=16), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("open", sa.Double()),
            sa.Column("high", sa.Double()),
            sa.Column("low", sa.Double()),
            sa.Column("close", sa.Double()),
            sa.Column("volume", sa.BigInteger()),
            sa.Column("amount", sa.Double()),
            sa.Column("turnover_rate", sa.Double()),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        )

    # --- 3. etf_adj_factor（Tushare fund_adj 复权因子） ---
    if not _table_exists(conn, "etf_adj_factor"):
        op.create_table(
            "etf_adj_factor",
            sa.Column("instrument_id", sa.String(length=64), nullable=False),
            sa.Column("ts_code", sa.String(length=16), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("adj_factor", sa.Double(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        )

    # --- 4. 结构校验 ---
    for table in ("cn_etf_basic", "etf_daily", "etf_adj_factor"):
        _require(_table_exists(conn, table), f"{table} 表未创建")

    # --- 5. 纯增量校验：既有表行数不变 ---
    for table in EXISTING_TABLES:
        after = _count(conn, table)
        _require(
            after == counts_before[table],
            f"既有表 {table} 行数在迁移中发生变化: {counts_before[table]} -> {after}",
        )

    # --- 6. 新表初始为空（迁移不写入任何业务数据行，design D9） ---
    for table in ("cn_etf_basic", "etf_daily", "etf_adj_factor"):
        _require(
            _count(conn, table) == 0,
            f"{table} 初始应为空（迁移不写入任何行）",
        )

    log.info(
        "[0005] ETF 数据模块迁移完成：cn_etf_basic / etf_daily / etf_adj_factor 三表建立，"
        "既有 %d 张表数据不变",
        len(EXISTING_TABLES),
    )


def downgrade() -> None:
    """降级：删除三张新表（数据丢弃，生产环境破坏性回滚应走数据库文件
    备份恢复，db-migration spec）。"""
    conn = op.get_bind()
    # 按依赖逆序：先事实表再主档表
    for table in ("etf_adj_factor", "etf_daily", "cn_etf_basic"):
        if _table_exists(conn, table):
            op.drop_table(table)
