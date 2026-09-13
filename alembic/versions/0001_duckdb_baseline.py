"""DuckDB 基线：marketmind v1 全量建表（技术方案 §9-14）。

一次性创建 10 张核心表 + seq_tag_id sequence。不继承 stocksview 的 SQLite
迁移历史（0001_v002_baseline → 0003_v003b 均废弃）；旧 SQLite 数据走独立
导入工具（T14，后续版本）。

表结构要点（与 app/models 逐列一致，防漂移测试保障）：
- 业务主键取代自增代理 id：instrument_id 主键 / 复合主键 / tag 显式 sequence；
- quote_snapshot 一证券一行（主键 + 应用层 upsert）；
- 外键全部命名且不带 ON DELETE（DuckDB 不支持级联删除）；
- 时间列统一 TIMESTAMPTZ，交易日 DATE。

Revision ID: 0001_duckdb_baseline
Revises:
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_duckdb_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- sequence（tag 主键取号，不依赖任何数据库的自增行为） ---
    # alembic 1.20 的 Operations 无 create_sequence（Phase 0 结论），走原生 DDL；
    # DuckDB CREATE SEQUENCE 语义已由 spike_sequence 路线 A 验证。
    op.execute("CREATE SEQUENCE seq_tag_id START 1")

    # --- 证券主数据 ---
    op.create_table(
        "instrument",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("asset_type", sa.String(length=8), nullable=False),
        sa.Column("exchange", sa.String(length=16), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("instrument_id"),
    )

    # --- 自选列表（instrument_id 主键，一证券一行） ---
    op.create_table(
        "watchlist",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_watchlist_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
    )

    op.create_table(
        "index_watchlist",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_index_watchlist_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
    )

    # --- 标签（tag_id 由 seq_tag_id 取号） ---
    op.create_table(
        "tag",
        sa.Column("tag_id", sa.BigInteger(), server_default=sa.text("nextval('seq_tag_id')"), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tag_id"),
        # name 不设 UNIQUE：DuckDB 1.5.5 中被 FK 引用的父表 UNIQUE 列不可 UPDATE
        # （Phase 0 结论），唯一性由 TagService 写锁内查重保证
    )

    # --- 自选 × 标签 关联（复合主键，无级联删除） ---
    op.create_table(
        "watchlist_tag",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["watchlist.instrument_id"],
            name="fk_watchlist_tag_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tag.tag_id"],
            name="fk_watchlist_tag_tag",
        ),
        sa.PrimaryKeyConstraint("instrument_id", "tag_id"),
    )

    # --- 行情快照（当前行情，一证券一行，upsert 更新） ---
    op.create_table(
        "quote_snapshot",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("price", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("change_percent", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("volume_ratio", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("previous_close", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_quote_snapshot_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
    )

    # --- 估值快照（按交易日，复合主键） ---
    op.create_table(
        "fundamental_snapshot",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("pe_ttm", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("pb", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("dividend_yield_ttm", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_fundamental_snapshot_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id", "trade_date"),
    )

    # --- 交易日历（复合主键） ---
    op.create_table(
        "trading_calendar",
        sa.Column("market", sa.String(length=8), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("market", "trade_date"),
    )

    # --- 后台任务状态 ---
    op.create_table(
        "job_status",
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_name"),
    )

    # --- 系统设置 ---
    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """回空库：逆序 DROP 全部表与 sequence（不承诺任意 downgrade，破坏性回滚走备份恢复）。"""
    op.drop_table("app_setting")
    op.drop_table("job_status")
    op.drop_table("trading_calendar")
    op.drop_table("fundamental_snapshot")
    op.drop_table("quote_snapshot")
    op.drop_table("watchlist_tag")
    op.drop_table("tag")
    op.drop_table("index_watchlist")
    op.drop_table("watchlist")
    op.drop_table("instrument")
    op.execute("DROP SEQUENCE IF EXISTS seq_tag_id")
