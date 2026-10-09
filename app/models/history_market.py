"""A股证券与 ETF 主档模型（a-share-historical-data §8~§10；etf-data-module D1）。

四张小表使用 ORM（技术方案 §5.1）：
- ``cn_stock_basic``：Tushare stock_basic 完整主档（17 个业务字段 + 采集元数据），
  覆盖沪/深/北三市场与全部上市状态（含退市），不裁剪到 2010 年起；
- ``cn_stock_company``：公司资料（含 introduction/office/main_business/business_scope）；
- ``cn_stock_name_change``：历史名称，event_key 为
  SHA-256(ts_code|name|start_date) 稳定键（§10.1；在线验证后如需扩展为
  ts_code+name+start_date+end_date+ann_date 须经显式决策，不凭猜测改语义）；
- ``cn_etf_basic``：ETF 业务主档（etf-data-module，universe 刷新维护）。

与 ``instrument`` 的关系（§7）：instrument 继续作为全资产统一主档，
cn_stock_basic 保存 Tushare 原始证券信息，二者经 instrument_id 关联；
退市证券 is_active=false 但不删除（§7.2）。ETF 同理（etf-data-module）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Double, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CnStockBasic(Base):
    """Tushare stock_basic 完整证券主档（技术方案 §8.2）。"""

    __tablename__ = "cn_stock_basic"

    instrument_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("instrument.instrument_id"), primary_key=True
    )
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128))
    area: Mapped[str | None] = mapped_column(String(32))
    industry: Mapped[str | None] = mapped_column(String(64))
    fullname: Mapped[str | None] = mapped_column(String(128))
    enname: Mapped[str | None] = mapped_column(String(256))
    cnspell: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str | None] = mapped_column(String(16))
    exchange: Mapped[str | None] = mapped_column(String(16))
    curr_type: Mapped[str | None] = mapped_column(String(8))
    list_status: Mapped[str | None] = mapped_column(String(8))  # L/D/P/G/UN
    list_date: Mapped[date | None] = mapped_column(Date)
    delist_date: Mapped[date | None] = mapped_column(Date)
    is_hs: Mapped[str | None] = mapped_column(String(8))  # S/H/N/None
    act_name: Mapped[str | None] = mapped_column(String(256))
    act_ent_type: Mapped[str | None] = mapped_column(String(64))

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sync_run_id: Mapped[str | None] = mapped_column(String(64))


class CnStockCompany(Base):
    """公司资料（技术方案 §9.1；含默认不返回的长文本可选字段）。"""

    __tablename__ = "cn_stock_company"

    instrument_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("instrument.instrument_id"), primary_key=True
    )
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    com_name: Mapped[str | None] = mapped_column(String(256))
    com_id: Mapped[str | None] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(16))
    chairman: Mapped[str | None] = mapped_column(String(64))
    manager: Mapped[str | None] = mapped_column(String(64))
    secretary: Mapped[str | None] = mapped_column(String(64))
    reg_capital: Mapped[float | None] = mapped_column(Double)
    setup_date: Mapped[date | None] = mapped_column(Date)
    province: Mapped[str | None] = mapped_column(String(32))
    city: Mapped[str | None] = mapped_column(String(32))
    introduction: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(String(256))
    email: Mapped[str | None] = mapped_column(String(128))
    office: Mapped[str | None] = mapped_column(Text)
    employees: Mapped[int | None] = mapped_column(Integer)
    main_business: Mapped[str | None] = mapped_column(Text)
    business_scope: Mapped[str | None] = mapped_column(Text)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sync_run_id: Mapped[str | None] = mapped_column(String(64))


class CnStockNameChange(Base):
    """历史名称事件（技术方案 §10.1）；event_key 为应用生成的稳定键。"""

    __tablename__ = "cn_stock_name_change"

    event_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    instrument_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    ann_date: Mapped[date | None] = mapped_column(Date)
    change_reason: Mapped[str | None] = mapped_column(String(256))

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sync_run_id: Mapped[str | None] = mapped_column(String(64))


class CnEtfBasic(Base):
    """ETF 业务主档（etf-data-module，design D1/D2）。

    与 ``cn_stock_basic`` 同构的 ETF 主档：身份落 ``instrument``
    （``CN:ETF:<symbol>``），业务字段在此保存；universe 刷新时同事务 upsert。
    list_date/delist_date 均可空——universe 来源接口无上市/退市日期列时
    保存 NULL，由同步 planner 保守回退（design D8）；退市 ETF 仅置
    instrument.is_active=false，本表与历史事实数据不删除。
    """

    __tablename__ = "cn_etf_basic"

    instrument_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("instrument.instrument_id"), primary_key=True
    )
    # Tushare 口径（symbol + 交易所后缀，如 510300.SH），供 fund_adj 请求构造
    ts_code: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128))
    exchange: Mapped[str | None] = mapped_column(String(16))

    list_date: Mapped[date | None] = mapped_column(Date)
    delist_date: Mapped[date | None] = mapped_column(Date)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sync_run_id: Mapped[str | None] = mapped_column(String(64))
