"""A股日级历史事实表（a-share-historical-data，技术方案 §12~§16）。

四张表为千万行级，按技术方案 §5.1 使用 SQLAlchemy Core ``Table`` 定义：
- 无物理主键 / 外键 / 二级索引——业务唯一键 (instrument_id, trade_date) 由
  整日 DELETE+INSERT 替换、写入前查重与 ``history_day_status`` 账本共同保证（§12）；
- 注册进 ``Base.metadata``：create_all 与 Alembic 迁移两路径均建表，
  由防漂移测试逐列比对（test_migrations.test_alembic_head_matches_models_schema）；
- 原始单位保持 Tushare 官方口径（vol 手 / amount 千元 / total_mv 万元），
  单位换算只发生在查询/展示层；上游 NULL 原样保存，不填 0。

执行归属不在事实行上保存 run_id（§13）：经
``history_day_status.completed_by_run_id`` 查询。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    Double,
    Integer,
    SmallInteger,
    String,
    Table,
)

from app.db import Base

# 每表通用的采集元数据列（§13~§16）；Column 不可跨表复用，按表新建
def _meta_columns() -> tuple[Column, Column]:
    return (
        Column("source", String(32), nullable=False),
        Column("fetched_at", DateTime(timezone=True), nullable=False),
    )

market_daily_bar = Table(
    "market_daily_bar",
    Base.metadata,
    Column("instrument_id", String(64), nullable=False),
    Column("ts_code", String(16), nullable=False),
    Column("trade_date", Date, nullable=False),
    # 原始未复权行情；ah_* 历史为空合法（盘后固定价格交易时段 2021 年前不存在）
    Column("open", Double),
    Column("high", Double),
    Column("low", Double),
    Column("close", Double),
    Column("pre_close", Double),
    Column("change", Double),
    Column("pct_chg", Double),
    Column("vol", Double),  # 手
    Column("amount", Double),  # 千元
    Column("ah_vol", Double),
    Column("ah_amount", Double),
    *_meta_columns(),
)

market_adj_factor = Table(
    "market_adj_factor",
    Base.metadata,
    Column("instrument_id", String(64), nullable=False),
    Column("ts_code", String(16), nullable=False),
    Column("trade_date", Date, nullable=False),
    Column("adj_factor", Double, nullable=False),  # 必须 > 0（校验层保证）
    *_meta_columns(),
)

market_daily_basic = Table(
    "market_daily_basic",
    Base.metadata,
    Column("instrument_id", String(64), nullable=False),
    Column("ts_code", String(16), nullable=False),
    Column("trade_date", Date, nullable=False),
    # NULL 保留原义：亏损 PE、无股息率、历史阶段字段缺失（§15）
    Column("close", Double),
    Column("turnover_rate", Double),
    Column("turnover_rate_f", Double),
    Column("volume_ratio", Double),
    Column("pe", Double),
    Column("pe_ttm", Double),
    Column("pb", Double),
    Column("ps", Double),
    Column("ps_ttm", Double),
    Column("dv_ratio", Double),
    Column("dv_ttm", Double),
    Column("total_share", Double),
    Column("float_share", Double),
    Column("free_share", Double),
    Column("total_mv", Double),  # 万元
    Column("circ_mv", Double),  # 万元
    Column("limit_status", SmallInteger),  # 枚举见校验层（§38）
    *_meta_columns(),
)

market_moneyflow = Table(
    "market_moneyflow",
    Base.metadata,
    Column("instrument_id", String(64), nullable=False),
    Column("ts_code", String(16), nullable=False),
    Column("trade_date", Date, nullable=False),
    # 主动买/卖：非 NULL 时非负；net_mf_* 可正可负可为 0（§16）；
    # 不按主档证券数推断应有行数——接口覆盖范围本身可能小于主档
    Column("buy_sm_vol", BigInteger),
    Column("buy_sm_amount", Double),
    Column("sell_sm_vol", BigInteger),
    Column("sell_sm_amount", Double),
    Column("buy_md_vol", BigInteger),
    Column("buy_md_amount", Double),
    Column("sell_md_vol", BigInteger),
    Column("sell_md_amount", Double),
    Column("buy_lg_vol", BigInteger),
    Column("buy_lg_amount", Double),
    Column("sell_lg_vol", BigInteger),
    Column("sell_lg_amount", Double),
    Column("buy_elg_vol", BigInteger),
    Column("buy_elg_amount", Double),
    Column("sell_elg_vol", BigInteger),
    Column("sell_elg_amount", Double),
    Column("net_mf_vol", BigInteger),
    Column("net_mf_amount", Double),
    *_meta_columns(),
)

HISTORY_FACT_TABLES: dict[str, Table] = {
    "daily": market_daily_bar,
    "adj_factor": market_adj_factor,
    "daily_basic": market_daily_basic,
    "moneyflow": market_moneyflow,
}
"""dataset 名称 -> 事实表（供 Repository 与迁移测试引用）。"""
