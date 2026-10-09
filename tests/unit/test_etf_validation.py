"""ETF 数据校验单测（etf-data-module）。

覆盖：etf_daily 专项规则（OHLC 非负且 high/low 约束、volume/amount/turnover_rate 非负、
NULL 保留）、etf_adj_factor 规则（adj_factor>0）、0 行合法（区间模式）、
非法值拒绝、_DAY_LEVEL_DATASETS 扩展。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.providers.base import AdjFactor, EtfDailyBar, ProviderBatch
from app.services.history.validation import (
    InvalidValueError,
    TradeDateMismatchError,
    validate_batch,
)


def _etf_daily_record(
    instrument_id: str = "CN:ETF:510300",
    trade_date: date = date(2026, 9, 16),
    **overrides,
) -> EtfDailyBar:
    """构造合法 ETF 日线记录。"""
    fields = dict(
        instrument_id=instrument_id,
        ts_code="510300.SH",
        trade_date=trade_date,
        open=4.5,
        high=4.6,
        low=4.4,
        close=4.55,
        volume=1000000,
        amount=4550000.0,
        turnover_rate=5.5,
    )
    fields.update(overrides)
    return EtfDailyBar(**fields)


def _adj_factor_record(
    instrument_id: str = "CN:ETF:510300",
    trade_date: date = date(2026, 9, 16),
    **overrides,
) -> AdjFactor:
    """构造合法 ETF 复权因子记录。"""
    fields = dict(
        instrument_id=instrument_id,
        ts_code="510300.SH",
        trade_date=trade_date,
        adj_factor=1.0,
    )
    fields.update(overrides)
    return AdjFactor(**fields)


class TestEtfDailyValidation:
    """etf_daily 数据集专项校验测试。"""

    def test_valid_etf_daily_passes(self):
        """合法 ETF 日线记录通过校验。"""
        batch = ProviderBatch(
            records=[_etf_daily_record()],
            source="eastmoney",
            raw_row_count=1,
        )
        validate_batch(
            "etf_daily",
            batch,
            trade_date=date(2026, 9, 16),
            known_instrument_ids={"CN:ETF:510300"},
        )

    def test_negative_open_rejected(self):
        """open 为负数时拒绝。"""
        batch = ProviderBatch(
            records=[_etf_daily_record(open=-1.0)],
            source="eastmoney",
            raw_row_count=1,
        )
        with pytest.raises(InvalidValueError, match="open.*负数"):
            validate_batch(
                "etf_daily",
                batch,
                trade_date=date(2026, 9, 16),
                known_instrument_ids={"CN:ETF:510300"},
            )

    def test_negative_volume_rejected(self):
        """volume 为负数时拒绝。"""
        batch = ProviderBatch(
            records=[_etf_daily_record(volume=-100)],
            source="eastmoney",
            raw_row_count=1,
        )
        with pytest.raises(InvalidValueError, match="volume.*负数"):
            validate_batch(
                "etf_daily",
                batch,
                trade_date=date(2026, 9, 16),
                known_instrument_ids={"CN:ETF:510300"},
            )

    def test_negative_turnover_rate_rejected(self):
        """turnover_rate 为负数时拒绝。"""
        batch = ProviderBatch(
            records=[_etf_daily_record(turnover_rate=-5.5)],
            source="eastmoney",
            raw_row_count=1,
        )
        with pytest.raises(InvalidValueError, match="turnover_rate.*负数"):
            validate_batch(
                "etf_daily",
                batch,
                trade_date=date(2026, 9, 16),
                known_instrument_ids={"CN:ETF:510300"},
            )

    def test_high_lower_than_open_rejected(self):
        """high < open 时拒绝。"""
        batch = ProviderBatch(
            records=[_etf_daily_record(open=4.5, high=4.0, low=3.5, close=4.2)],
            source="eastmoney",
            raw_row_count=1,
        )
        with pytest.raises(InvalidValueError, match="high.*低于.*OHLC"):
            validate_batch(
                "etf_daily",
                batch,
                trade_date=date(2026, 9, 16),
                known_instrument_ids={"CN:ETF:510300"},
            )

    def test_low_higher_than_close_rejected(self):
        """low > close 时拒绝。"""
        batch = ProviderBatch(
            records=[_etf_daily_record(open=4.5, high=4.6, low=4.58, close=4.55)],
            source="eastmoney",
            raw_row_count=1,
        )
        with pytest.raises(InvalidValueError, match="low.*高于.*open/close"):
            validate_batch(
                "etf_daily",
                batch,
                trade_date=date(2026, 9, 16),
                known_instrument_ids={"CN:ETF:510300"},
            )

    def test_null_fields_allowed(self):
        """NULL 字段保留（合法停牌场景）。"""
        batch = ProviderBatch(
            records=[
                _etf_daily_record(
                    open=None, high=None, low=None, close=None,
                    volume=None, amount=None, turnover_rate=None,
                )
            ],
            source="eastmoney",
            raw_row_count=1,
        )
        validate_batch(
            "etf_daily",
            batch,
            trade_date=date(2026, 9, 16),
            known_instrument_ids={"CN:ETF:510300"},
        )

    def test_range_mode_zero_rows_allowed(self):
        """区间模式 0 行合法（停牌区间）。"""
        batch = ProviderBatch(records=[], source="eastmoney", raw_row_count=0)
        validate_batch(
            "etf_daily",
            batch,
            trade_date=None,
            known_instrument_ids={"CN:ETF:510300"},
            date_range=(date(2026, 9, 1), date(2026, 9, 16)),
        )

    def test_range_mode_date_outside_range_rejected(self):
        """区间模式：记录日期落在区间外时拒绝。"""
        batch = ProviderBatch(
            records=[_etf_daily_record(trade_date=date(2026, 9, 20))],
            source="eastmoney",
            raw_row_count=1,
        )
        with pytest.raises(TradeDateMismatchError, match="落在请求区间.*之外"):
            validate_batch(
                "etf_daily",
                batch,
                trade_date=None,
                known_instrument_ids={"CN:ETF:510300"},
                date_range=(date(2026, 9, 1), date(2026, 9, 16)),
            )

    def test_range_mode_before_list_date_rejected(self):
        """区间模式：记录日期早于上市日时拒绝。"""
        batch = ProviderBatch(
            records=[_etf_daily_record(trade_date=date(2015, 5, 1))],
            source="eastmoney",
            raw_row_count=1,
        )
        with pytest.raises(TradeDateMismatchError, match="早于上市日"):
            validate_batch(
                "etf_daily",
                batch,
                trade_date=None,
                known_instrument_ids={"CN:ETF:510300"},
                date_range=(date(2015, 1, 1), date(2026, 9, 16)),
                lifecycle=(date(2015, 6, 1), None),
            )


class TestEtfAdjFactorValidation:
    """etf_adj_factor 数据集专项校验测试。"""

    def test_valid_adj_factor_passes(self):
        """合法复权因子通过校验。"""
        batch = ProviderBatch(
            records=[_adj_factor_record()],
            source="tushare",
            raw_row_count=1,
        )
        validate_batch(
            "etf_adj_factor",
            batch,
            trade_date=date(2026, 9, 16),
            known_instrument_ids={"CN:ETF:510300"},
        )

    def test_zero_adj_factor_rejected(self):
        """adj_factor=0 时拒绝。"""
        batch = ProviderBatch(
            records=[_adj_factor_record(adj_factor=0.0)],
            source="tushare",
            raw_row_count=1,
        )
        with pytest.raises(InvalidValueError, match="adj_factor 必须 > 0"):
            validate_batch(
                "etf_adj_factor",
                batch,
                trade_date=date(2026, 9, 16),
                known_instrument_ids={"CN:ETF:510300"},
            )

    def test_negative_adj_factor_rejected(self):
        """adj_factor<0 时拒绝。"""
        batch = ProviderBatch(
            records=[_adj_factor_record(adj_factor=-1.0)],
            source="tushare",
            raw_row_count=1,
        )
        with pytest.raises(InvalidValueError, match="adj_factor 必须 > 0"):
            validate_batch(
                "etf_adj_factor",
                batch,
                trade_date=date(2026, 9, 16),
                known_instrument_ids={"CN:ETF:510300"},
            )

    def test_range_mode_zero_rows_allowed(self):
        """区间模式 0 行合法（无除权事件场景）。"""
        batch = ProviderBatch(records=[], source="tushare", raw_row_count=0)
        validate_batch(
            "etf_adj_factor",
            batch,
            trade_date=None,
            known_instrument_ids={"CN:ETF:510300"},
            date_range=(date(2026, 9, 1), date(2026, 9, 16)),
        )
