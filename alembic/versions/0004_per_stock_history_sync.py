"""个股水位与任务流水（per-stock-history-sync，design D2/D3）。

结构变更（db-migration spec 0004 迁移）：
- 新增 sequence ``seq_sync_task_id``；
- 新增 ``stock_sync_state``（个股水位与状态，(dataset, instrument_id) 逻辑唯一键，
  无 UNIQUE/FK/二级索引）；
- 新增 ``sync_task``（单股同步任务流水，id 由 seq_sync_task_id 生成，
  无 UNIQUE/FK/二级索引）；
- ``history_sync_run_dataset`` 增加四列统计：processed_count / task_success_count
  / task_failed_count / skipped_count（server_default 0）。

约束（design D3）：
- 不写入任何 stock_sync_state 行——初始水位统一 NULL，首轮 run 时按 universe
  批量补建；
- 仅附只读诊断统计（各数据集事实行数、有数据股票数、最大交易日分布），
  写入迁移日志，不修改任何业务数据；
- 幂等可重放；结构校验失败整体回滚；downgrade 仅提供 DDL 逆操作。

Revision ID: 0004_per_stock_history_sync
Revises: 0003_a_share_historical_data
Create Date: 2026-09-24
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_per_stock_history_sync"
down_revision: Union[str, None] = "0003_a_share_historical_data"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")

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

# 四个日级事实表（用于只读诊断统计）
FACT_TABLES = (
    ("daily", "market_daily_bar"),
    ("adj_factor", "market_adj_factor"),
    ("daily_basic", "market_daily_basic"),
    ("moneyflow", "market_moneyflow"),
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


def _column_exists(conn, table: str, column: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'main' AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).fetchone()
    return row is not None


def _sequence_exists(conn, seq: str) -> bool:
    row = conn.execute(
        sa.text(
            "SELECT 1 FROM duckdb_sequences() "
            "WHERE schema_name = 'main' AND sequence_name = :s"
        ),
        {"s": seq},
    ).fetchone()
    return row is not None


def upgrade() -> None:
    conn = op.get_bind()

    # --- 前置：记录既有表行数（纯增量校验用） ---
    counts_before = {t: _count(conn, t) for t in EXISTING_TABLES}

    # --- 1. sequence seq_sync_task_id（幂等：已存在则跳过） ---
    if not _sequence_exists(conn, "seq_sync_task_id"):
        op.execute("CREATE SEQUENCE seq_sync_task_id START 1")

    # --- 2. stock_sync_state 表（幂等：已存在则跳过建表） ---
    if not _table_exists(conn, "stock_sync_state"):
        op.create_table(
            "stock_sync_state",
            sa.Column("dataset", sa.String(length=32), nullable=False),
            sa.Column("instrument_id", sa.String(length=64), nullable=False),
            sa.Column("ts_code", sa.String(length=16), nullable=True),
            sa.Column("watermark_date", sa.Date(), nullable=True),
            sa.Column("last_task_id", sa.BigInteger(), nullable=True),
            sa.Column("last_status", sa.String(length=16), nullable=True),
            sa.Column("last_error_code", sa.String(length=64), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("dataset", "instrument_id"),
            # 无 UNIQUE / FK / 二级索引（项目惯例，design D2）
        )

    # --- 3. sync_task 表（幂等：已存在则跳过建表） ---
    if not _table_exists(conn, "sync_task"):
        op.create_table(
            "sync_task",
            sa.Column(
                "id",
                sa.BigInteger(),
                server_default=sa.text("nextval('seq_sync_task_id')"),
                nullable=False,
            ),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("dataset", sa.String(length=32), nullable=False),
            sa.Column("instrument_id", sa.String(length=64), nullable=False),
            sa.Column("ts_code", sa.String(length=16), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column(
                "retry_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column(
                "attempt_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column(
                "records_fetched",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column(
                "records_written",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.Column("error_type", sa.String(length=64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.BigInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            # 无 UNIQUE / FK / 二级索引（项目惯例，design D2）
        )

    # --- 4. history_sync_run_dataset 加四列统计（幂等：已存在则跳过） ---
    # DuckDB 不支持 ADD COLUMN 同时指定 NOT NULL 约束，分两步：
    # 先加列（带 DEFAULT，可空），再 ALTER COLUMN SET NOT NULL。
    for col_name in (
        "processed_count",
        "task_success_count",
        "task_failed_count",
        "skipped_count",
    ):
        if not _column_exists(conn, "history_sync_run_dataset", col_name):
            op.add_column(
                "history_sync_run_dataset",
                sa.Column(
                    col_name,
                    sa.Integer(),
                    server_default=sa.text("0"),
                    nullable=True,
                ),
            )
            op.alter_column(
                "history_sync_run_dataset",
                col_name,
                nullable=False,
                existing_type=sa.Integer(),
                existing_server_default=sa.text("0"),
            )

    # --- 5. 结构校验 ---
    _require(
        _sequence_exists(conn, "seq_sync_task_id"),
        "seq_sync_task_id sequence 未创建",
    )
    _require(
        _table_exists(conn, "stock_sync_state"),
        "stock_sync_state 表未创建",
    )
    _require(_table_exists(conn, "sync_task"), "sync_task 表未创建")
    for col_name in (
        "processed_count",
        "task_success_count",
        "task_failed_count",
        "skipped_count",
    ):
        _require(
            _column_exists(conn, "history_sync_run_dataset", col_name),
            f"history_sync_run_dataset.{col_name} 列未创建",
        )

    # --- 6. 纯增量校验：既有表行数不变 ---
    for table in EXISTING_TABLES:
        after = _count(conn, table)
        _require(
            after == counts_before[table],
            f"既有表 {table} 行数在迁移中发生变化: {counts_before[table]} -> {after}",
        )

    # --- 7. 新表初始为空（不写入任何 stock_sync_state 行，design D3） ---
    _require(
        _count(conn, "stock_sync_state") == 0,
        "stock_sync_state 初始应为空（迁移不写入任何行）",
    )
    _require(
        _count(conn, "sync_task") == 0,
        "sync_task 初始应为空",
    )

    # --- 8. 只读诊断统计（SELECT only，不修改业务数据） ---
    log.info("[0004] ===== 历史数据诊断统计（只读） =====")
    for dataset, table in FACT_TABLES:
        total = _count(conn, table)
        stock_count = conn.execute(
            sa.text(f"SELECT COUNT(DISTINCT instrument_id) FROM {table}")
        ).scalar_one()
        max_date = conn.execute(
            sa.text(f"SELECT MAX(trade_date) FROM {table}")
        ).scalar_one()
        min_date = conn.execute(
            sa.text(f"SELECT MIN(trade_date) FROM {table}")
        ).scalar_one()
        log.info(
            "[0004] 数据集 %-12s 行数=%-10s 股票数=%-6s 区间=[%s, %s]",
            dataset, total, stock_count, min_date, max_date,
        )
    log.info("[0004] ===========================================")


def downgrade() -> None:
    """降级：删除两张新表、run_dataset 扩展列与 sequence（数据丢弃，
    生产环境破坏性回滚应走数据库文件备份恢复，db-migration spec）。"""
    # 删表（先 sync_task 再 stock_sync_state，虽无 FK 仍按依赖序）
    if _table_exists(op.get_bind(), "sync_task"):
        op.drop_table("sync_task")
    if _table_exists(op.get_bind(), "stock_sync_state"):
        op.drop_table("stock_sync_state")

    # 删 run_dataset 扩展列
    for col_name in (
        "skipped_count",
        "task_failed_count",
        "task_success_count",
        "processed_count",
    ):
        if _column_exists(op.get_bind(), "history_sync_run_dataset", col_name):
            op.drop_column("history_sync_run_dataset", col_name)

    # 删 sequence
    if _sequence_exists(op.get_bind(), "seq_sync_task_id"):
        op.execute("DROP SEQUENCE seq_sync_task_id")
