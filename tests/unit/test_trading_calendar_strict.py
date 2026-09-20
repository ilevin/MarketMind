"""严格交易日历单测（tasks 3.9：strict 无降级、旧行重拉、完整性校验）。

mock trade_cal 返回（不依赖真实 Token/网络）+ 真实临时 DuckDB，
覆盖技术方案 §11.1 / §31.6。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from app.config import AppConfig, TushareConfig
from app.models.trading_calendar import TradingCalendarDay
from app.providers.trading_calendar.provider import (
    CalendarUnavailableError,
    TushareTradingCalendarProvider,
)
from app.providers.tushare_common import (
    TushareRequestGate,
    TushareTransport,
)
from app.repositories.trading_calendar import TradingCalendarRepository


def _full_year_df(year: int, *, drop: date | None = None) -> pd.DataFrame:
    """全年 trade_cal（周末休市近似）；drop 模拟上游缺失日。"""
    rows = []
    day = date(year, 1, 1)
    while day.year == year:
        if day != drop:
            is_open = 0 if day.weekday() >= 5 else 1
            rows.append(
                {
                    "exchange": "SSE",
                    "cal_date": day.strftime("%Y%m%d"),
                    "is_open": is_open,
                    "pretrade_date": "20260911" if is_open else None,
                }
            )
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def _provider(session_factory, response, config: AppConfig | None = None):
    cfg = config or AppConfig(tushare=TushareConfig(token="fake-token"))

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def trade_cal(self, **params):
            self.calls += 1
            if callable(response):
                return response(**params)
            return response

    client = FakeClient()
    transport = TushareTransport(
        cfg, gate=TushareRequestGate(0), client_factory=lambda c: client
    )
    provider = TushareTradingCalendarProvider(cfg, session_factory, transport=transport)
    provider.fake_client = client
    return provider


def test_strict_fetches_persists_and_returns_with_metadata(session_factory):
    provider = _provider(session_factory, lambda **p: _full_year_df(int(p["start_date"][:4])))
    records = provider.get_days("CN", date(2026, 9, 14), date(2026, 9, 19), strict=True)

    assert [r.trade_date for r in records] == [date(2026, 9, d) for d in range(14, 20)]
    assert records[0].is_open is True
    assert records[0].pretrade_date == date(2026, 9, 11)
    assert records[5].is_open is False and records[5].pretrade_date is None  # 周六

    with session_factory() as s:
        rows = TradingCalendarRepository(s).get_days_between(
            "CN", date(2026, 9, 13), date(2026, 9, 19)
        )
        assert len(rows) == 7
        row = next(r for r in rows if r.trade_date == date(2026, 9, 13))
        assert row.source == "tushare" and row.exchange == "SSE"
        assert row.is_open is False and row.pretrade_date is None
        assert row.fetched_at is not None


def test_strict_second_call_uses_cache_without_refetch(session_factory):
    provider = _provider(session_factory, lambda **p: _full_year_df(int(p["start_date"][:4])))
    provider.get_days("CN", date(2026, 9, 14), date(2026, 9, 15), strict=True)
    assert provider.fake_client.calls == 1
    records = provider.get_days("CN", date(2026, 9, 14), date(2026, 9, 15), strict=True)
    assert provider.fake_client.calls == 1  # 缓存命中，不再请求
    assert records[0].is_open is True


def test_strict_refetches_year_with_legacy_null_source_rows(session_factory):
    """实时路径写入的旧行（source=NULL）不算严格数据，触发重拉并覆盖元数据。"""
    with session_factory() as s:
        s.add(TradingCalendarDay(market="CN", trade_date=date(2025, 10, 13), is_open=True))
        s.commit()

    provider = _provider(session_factory, lambda **p: _full_year_df(int(p["start_date"][:4])))
    records = provider.get_days("CN", date(2025, 10, 13), date(2025, 10, 17), strict=True)
    assert records[0].is_open is True
    with session_factory() as s:
        row = next(
            r
            for r in TradingCalendarRepository(s).get_days_between(
                "CN", date(2025, 10, 13), date(2025, 10, 13)
            )
        )
        assert row.source == "tushare"  # 旧行元数据被严格落库覆盖
        assert row.exchange == "SSE" and row.is_open is True


def test_strict_incomplete_upstream_year_raises(session_factory):
    """上游返回缺日：绝不以 weekday 补造（§31.6），直接抛异常。"""
    provider = _provider(
        session_factory, lambda **p: _full_year_df(int(p["start_date"][:4]), drop=date(2026, 9, 16))
    )
    with pytest.raises(CalendarUnavailableError) as exc_info:
        provider.get_days("CN", date(2026, 9, 14), date(2026, 9, 18), strict=True)
    assert "不完整" in str(exc_info.value)
    assert exc_info.value.error_code == "CALENDAR_UNAVAILABLE"


def test_strict_empty_upstream_result_raises(session_factory):
    provider = _provider(
        session_factory,
        pd.DataFrame(columns=["exchange", "cal_date", "is_open", "pretrade_date"]),
    )
    with pytest.raises(CalendarUnavailableError, match="返回为空"):
        provider.get_days("CN", date(2027, 1, 5), date(2027, 1, 9), strict=True)


def test_strict_without_token_raises(session_factory):
    provider = _provider(
        session_factory,
        lambda **p: _full_year_df(int(p["start_date"][:4])),
        config=AppConfig(),  # 无 Token
    )
    with pytest.raises(CalendarUnavailableError, match="Token"):
        provider.get_days("CN", date(2027, 1, 5), date(2027, 1, 9), strict=True)


def test_strict_false_keeps_legacy_weekday_fallback(session_factory):
    """strict=False 与现有实时行为一致：空返回年份降级 weekday 近似。"""
    provider = _provider(
        session_factory,
        pd.DataFrame(columns=["exchange", "cal_date", "is_open", "pretrade_date"]),
    )
    records = provider.get_days("CN", date(2027, 3, 1), date(2027, 3, 7), strict=False)
    assert [r.is_open for r in records] == [True, True, True, True, True, False, False]
    # 近似结果不缓存（现有行为：不写库）
    with session_factory() as s:
        assert not TradingCalendarRepository(s).has_year("CN", 2027)
