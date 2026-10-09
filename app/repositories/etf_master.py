"""ETF 主档仓储（etf-data-module）。

职责：
- ``instrument`` + ``cn_etf_basic`` 同事务 upsert（对齐 stock_basic 模式）；
- 退市不删除，is_active=false；
- 本轮未见 ETF 置 is_active=false（universe 列表不再包含时）；
- list_date 缺失（NULL）由 planner 回退 history.start_date。

事务边界与提交由调用方负责。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.history_market import CnEtfBasic
from app.models.instrument import Instrument
from app.providers.base import EtfUniverseRecord

logger = logging.getLogger(__name__)

# EtfUniverseRecord 的业务字段（与 cn_etf_basic 列对齐）
# 注：delist_date 不在 EtfUniverseRecord 中（V1 东财列表无退市日期来源），
# 保持数据库 NULL（对齐 historical-data-storage spec "V1 东财列表无退市日期来源、恒为 NULL"）
_ETF_BASIC_FIELDS = (
    "ts_code",
    "symbol",
    "name",
    "exchange",
    "list_date",
)


class EtfMasterRepository:
    """ETF 主档 + instrument 的 upsert；全部方法在调用方事务内执行。"""

    def __init__(self, session: Session):
        self.session = session

    def upsert_cn_etf_master(
        self,
        records: list[EtfUniverseRecord],
        *,
        source: str,
        run_id: str | None,
        fetched_at: datetime,
    ) -> int:
        """同事务逐条 upsert instrument 与 cn_etf_basic；本轮未见 ETF 置 is_active=false。

        返回实际 upsert 的记录数（不含本轮未见的 inactive 标记数）。
        """
        # 收集本轮 universe 中的全部 instrument_id
        current_ids = {r.instrument_id for r in records}

        # 读取数据库中已有的全部 ETF instrument_id（含 inactive）
        existing_ids = set(
            self.session.scalars(
                select(Instrument.instrument_id).where(
                    Instrument.market == "CN", Instrument.asset_type == "ETF"
                )
            )
        )

        # 本轮未见的 ETF：置 is_active=false
        missing_ids = existing_ids - current_ids
        if missing_ids:
            for inst_id in missing_ids:
                inst = self.session.get(Instrument, inst_id)
                if inst and inst.is_active:
                    inst.is_active = False
                    logger.info("ETF %s 本轮未见，置 is_active=false", inst_id)

        # 逐条 upsert
        for record in records:
            self._upsert_instrument(record)
            row = self.session.get(CnEtfBasic, record.instrument_id)
            if row is None:
                row = CnEtfBasic(
                    instrument_id=record.instrument_id,
                    ts_code=record.ts_code,
                    symbol=record.symbol,
                )
                self.session.add(row)

            # 更新全部业务字段
            for field in _ETF_BASIC_FIELDS:
                setattr(row, field, getattr(record, field))

            row.source = source
            row.fetched_at = fetched_at
            row.source_last_seen_at = fetched_at
            row.sync_run_id = run_id

        self.session.flush()
        return len(records)

    def _upsert_instrument(self, record: EtfUniverseRecord) -> None:
        """instrument 映射规则（CN:ETF:<symbol>）；退市 ETF 重新上市时恢复 is_active=true。"""
        inst = self.session.get(Instrument, record.instrument_id)
        if inst is None:
            self.session.add(
                Instrument(
                    instrument_id=record.instrument_id,
                    symbol=record.symbol,
                    name=record.name,
                    market="CN",
                    asset_type="ETF",
                    currency="CNY",
                    exchange=record.exchange,
                    is_active=True,  # 新出现在 universe 即为 active
                )
            )
            self.session.flush()
            return

        # 已存在：更新字段
        if record.name is not None:
            inst.name = record.name
        if record.exchange is not None:
            inst.exchange = record.exchange

        # 本轮再次出现：恢复 is_active=true（退市后重新上市场景）
        if not inst.is_active:
            inst.is_active = True
            logger.info("ETF %s 重新出现在 universe，恢复 is_active=true", record.instrument_id)

    def list_cn_etf_instruments(self) -> list[Instrument]:
        """列出全部 ETF instruments（含 inactive），按 symbol 升序。"""
        return list(
            self.session.scalars(
                select(Instrument)
                .where(Instrument.market == "CN", Instrument.asset_type == "ETF")
                .order_by(Instrument.symbol)
            )
        )

    def get_cn_etf_lifecycle_map(self) -> dict[str, tuple[date | None, date | None]]:
        """返回 {instrument_id: (list_date, delist_date)} 映射，供 planner 使用。

        list_date 缺失时返回 None（planner 回退 history.start_date）。
        """
        rows = self.session.execute(
            select(
                CnEtfBasic.instrument_id, CnEtfBasic.list_date, CnEtfBasic.delist_date
            ).where(CnEtfBasic.instrument_id.isnot(None))
        ).all()

        return {row.instrument_id: (row.list_date, row.delist_date) for row in rows}
