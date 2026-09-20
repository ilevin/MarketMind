"""A 股主档仓储（a-share-historical-data，技术方案 §7~§10）。

- ``instrument`` + ``cn_stock_basic`` 同事务 upsert（§8.4：全 shard 内存
  校验后一次性提交；退市不删除，is_active=false，§7.2）；
- ``cn_stock_company`` upsert（§9）；
- ``cn_stock_name_change``：整证券替换（bootstrap，§10.2）与重叠窗口增量
  （保留更早历史，§10.3）；event_key 为 SHA-256(ts_code|name|start_date)
  稳定键（§10.1，扩展键须经在线验证显式决策）。

严格日历的读写见 ``TradingCalendarRepository``（save_days_strict /
get_days_between / has_strict_year）。事务边界与提交由调用方负责。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.history_market import (
    CnStockBasic,
    CnStockCompany,
    CnStockNameChange,
)
from app.models.instrument import Instrument
from app.providers.base import (
    StockBasicRecord,
    StockCompanyRecord,
    StockNameChangeRecord,
)

logger = logging.getLogger(__name__)


def namechange_event_key(ts_code: str, name: str | None, start_date: date | None) -> str:
    """历史名称事件稳定键：SHA-256(ts_code|name|start_date-or-empty)（§10.1）。"""
    payload = (
        f"{ts_code}|{name or ''}|{start_date.isoformat() if start_date else ''}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# StockBasicRecord 的 17 个业务字段（与 cn_stock_basic 列一致）
_STOCK_BASIC_FIELDS = (
    "ts_code", "symbol", "name", "area", "industry", "fullname", "enname",
    "cnspell", "market", "exchange", "curr_type", "list_status", "list_date",
    "delist_date", "is_hs", "act_name", "act_ent_type",
)

# StockCompanyRecord 的 18 个业务字段（与 cn_stock_company 列一致）
_STOCK_COMPANY_FIELDS = (
    "ts_code", "com_name", "com_id", "exchange", "chairman", "manager",
    "secretary", "reg_capital", "setup_date", "province", "city",
    "introduction", "website", "email", "office", "employees",
    "main_business", "business_scope",
)


class HistoryMasterRepository:
    """主档三表 + instrument 的 upsert/替换；全部方法在调用方事务内执行。"""

    def __init__(self, session: Session):
        self.session = session

    # ---- instrument + cn_stock_basic（§7/§8.4） ----

    def upsert_stock_basic(
        self,
        records: list[StockBasicRecord],
        *,
        source: str,
        run_id: str | None,
        fetched_at: datetime,
    ) -> int:
        """同事务逐条 upsert instrument 与 cn_stock_basic；退市不删除（§7.2）。"""
        for record in records:
            self._upsert_instrument(record)
            row = self.session.get(CnStockBasic, record.instrument_id)
            if row is None:
                row = CnStockBasic(
                    instrument_id=record.instrument_id,
                    ts_code=record.ts_code,
                    symbol=record.symbol,
                )
                self.session.add(row)
            for field in _STOCK_BASIC_FIELDS:
                setattr(row, field, getattr(record, field))
            row.source = source
            row.fetched_at = fetched_at
            row.source_last_seen_at = fetched_at
            row.sync_run_id = run_id
        self.session.flush()
        return len(records)

    def _upsert_instrument(self, record: StockBasicRecord) -> None:
        """instrument 映射规则（§7.1）；None 字段保留旧值（上游缺失不破坏已有数据）。"""
        inst = self.session.get(Instrument, record.instrument_id)
        if inst is None:
            self.session.add(
                Instrument(
                    instrument_id=record.instrument_id,
                    symbol=record.symbol,
                    name=record.name,
                    market="CN",
                    asset_type="STOCK",
                    currency="CNY",
                    exchange=record.exchange,
                    is_active=(record.list_status == "L") if record.list_status else True,
                )
            )
            self.session.flush()
            return
        if record.name is not None:
            inst.name = record.name  # 改名刷新
        if record.exchange is not None:
            inst.exchange = record.exchange
        if record.list_status is not None:
            inst.is_active = record.list_status == "L"  # 退市仅置 false

    # ---- cn_stock_company（§9） ----

    def upsert_stock_company(
        self,
        records: list[StockCompanyRecord],
        *,
        source: str,
        run_id: str | None,
        fetched_at: datetime,
    ) -> int:
        """写入公司资料，跳过主档中不存在的证券（返回实际写入数）。

        `cn_stock_company.instrument_id` 有指向 `instrument` 的外键；而
        Tushare 的 stock_company 覆盖面比 stock_basic 更宽（实测 6294 vs
        5915，多出的 442 个代码连按 ts_code 直查 stock_basic 也为空、当日
        无行情），这些孤立条目无法映射到证券主档。若直接 upsert 会触发外键
        约束失败，把非阻塞的公司资料刷新变成整轮报错。
        """
        candidates = {record.instrument_id for record in records}
        known = (
            set(
                self.session.scalars(
                    select(Instrument.instrument_id).where(
                        Instrument.instrument_id.in_(candidates)
                    )
                )
            )
            if candidates
            else set()
        )
        written = 0
        skipped = 0
        for record in records:
            if record.instrument_id not in known:
                skipped += 1
                continue
            row = self.session.get(CnStockCompany, record.instrument_id)
            if row is None:
                row = CnStockCompany(
                    instrument_id=record.instrument_id,
                    ts_code=record.ts_code,
                )
                self.session.add(row)
            else:
                row.ts_code = record.ts_code
            for field in _STOCK_COMPANY_FIELDS:
                setattr(row, field, getattr(record, field))
            row.source = source
            row.fetched_at = fetched_at
            row.sync_run_id = run_id
            written += 1
        self.session.flush()
        if skipped:
            logger.warning(
                "stock_company 跳过主档中不存在的证券 %d 条（共 %d 条）",
                skipped, len(records),
            )
        return written

    # ---- cn_stock_name_change（§10.2/§10.3） ----

    def replace_name_changes_for_instrument(
        self,
        instrument_id: str,
        records: list[StockNameChangeRecord],
        *,
        source: str,
        run_id: str | None,
        fetched_at: datetime,
    ) -> int:
        """整证券替换（bootstrap，§10.2）：删除该证券全部旧事件后插入当前
        返回集——records 为空同样执行删除（该证券无改名历史是合法结果）。"""
        self.session.execute(
            delete(CnStockNameChange).where(
                CnStockNameChange.instrument_id == instrument_id
            )
        )
        return self._insert_name_changes(
            records, source=source, run_id=run_id, fetched_at=fetched_at
        )

    def replace_name_changes_in_window(
        self,
        records: list[StockNameChangeRecord],
        *,
        window_start: date,
        source: str,
        run_id: str | None,
        fetched_at: datetime,
    ) -> int:
        """重叠窗口增量（§10.3）：删除涉及证券在窗口内（start_date >=
        window_start）的旧事件后插入当前返回集；保留更早历史事件。"""
        if records:
            instrument_ids = {record.instrument_id for record in records}
            self.session.execute(
                delete(CnStockNameChange).where(
                    CnStockNameChange.instrument_id.in_(instrument_ids),
                    CnStockNameChange.start_date >= window_start,
                )
            )
        return self._insert_name_changes(
            records, source=source, run_id=run_id, fetched_at=fetched_at
        )

    def _insert_name_changes(
        self,
        records: list[StockNameChangeRecord],
        *,
        source: str,
        run_id: str | None,
        fetched_at: datetime,
    ) -> int:
        for record in records:
            self.session.add(
                CnStockNameChange(
                    event_key=namechange_event_key(
                        record.ts_code, record.name, record.start_date
                    ),
                    instrument_id=record.instrument_id,
                    ts_code=record.ts_code,
                    name=record.name,
                    start_date=record.start_date,
                    end_date=record.end_date,
                    ann_date=record.ann_date,
                    change_reason=record.change_reason,
                    source=source,
                    fetched_at=fetched_at,
                    sync_run_id=run_id,
                )
            )
        self.session.flush()
        return len(records)

    # ---- 查询 ----

    def list_cn_stock_instruments(self) -> list[Instrument]:
        """全部 A 股证券（含退市——历史回填需要退市证券的旧交易日数据，§7.2）。"""
        return list(
            self.session.scalars(
                select(Instrument)
                .where(
                    Instrument.market == "CN",
                    Instrument.asset_type == "STOCK",
                )
                .order_by(Instrument.symbol)
            )
        )

    def list_ts_codes_tradable_on(self, trade_date: date) -> list[str]:
        """在该交易日可能产生行情的 ts_code 集合（含退市，按 ts_code 排序）。

        用于接口行数上限截断后的候选集构造（§33.2）：以 ``list_date``
        上市、``delist_date`` 退市或尚在市为准。日期字段为空的记录保守
        纳入（宁可多查一只，不可漏一只）。
        """
        rows = self.session.scalars(
            select(CnStockBasic.ts_code)
            .where(
                or_(CnStockBasic.list_date.is_(None), CnStockBasic.list_date <= trade_date),
                or_(
                    CnStockBasic.delist_date.is_(None),
                    CnStockBasic.delist_date >= trade_date,
                ),
            )
            .order_by(CnStockBasic.ts_code)
        )
        return list(rows)

    def count_stock_basic(self) -> int:
        return int(
            self.session.scalar(select(func.count()).select_from(CnStockBasic)) or 0
        )

    def count_stock_company(self) -> int:
        return int(
            self.session.scalar(select(func.count()).select_from(CnStockCompany)) or 0
        )

    def count_name_changes(self) -> int:
        return int(
            self.session.scalar(select(func.count()).select_from(CnStockNameChange)) or 0
        )
