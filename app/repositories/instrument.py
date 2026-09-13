"""Instrument 仓储。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.instrument import Instrument


class InstrumentRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, instrument_id: str) -> Instrument | None:
        return self.session.scalar(
            select(Instrument).where(Instrument.instrument_id == instrument_id)
        )

    def upsert(
        self,
        *,
        instrument_id: str,
        symbol: str,
        name: str,
        market: str,
        asset_type: str,
        exchange: str | None = None,
        currency: str = "CNY",
    ) -> Instrument:
        """幂等写入：不存在则插入，存在则更新名称等信息。"""
        inst = self.get(instrument_id)
        if inst is None:
            inst = Instrument(
                instrument_id=instrument_id,
                symbol=symbol,
                name=name,
                market=market,
                asset_type=asset_type,
                exchange=exchange,
                currency=currency,
            )
            self.session.add(inst)
        else:
            inst.name = name
            if exchange:
                inst.exchange = exchange
        self.session.flush()
        return inst
