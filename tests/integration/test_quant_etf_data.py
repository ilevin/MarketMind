"""ETF 量化查询测试（etf-data-module，tasks 7.3：复权计算 + REST 权限矩阵）。

服务层（EtfDataService，design D10）：
- qfq/hfq 公式与 factor_latest 基准（全库最新因子，含区间外——停牌日
  因子行参与基准）；
- 跨源一致性：区间内日线有行而因子缺失 → 明确报错列出缺失日；因子
  全空 → "复权因子未同步"；raw 不受因子表状态影响；
- 复权只作用于价格字段（volume/amount/turnover_rate 不变）、NULL 保留；
- 升序输出、未知代码报错、空区间空 items。

REST（design D11）：
- 权限矩阵：未登录 401、登录用户 200（非 admin 专属）；
- 422：symbol 非六位数字 / adjust 枚举外 / start > end / 缺参数；
- 404 未知代码；409 因子缺失（含缺失日明细）；
- 响应序列化：日期 ISO、数值口径。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import table as sa_table

from app.models.history_fact import HISTORY_FACT_TABLES
from app.models.history_market import CnEtfBasic
from app.models.instrument import Instrument
from app.services.quant.etf_data import (
    AdjustFactorMissingDaysError,
    AdjustFactorNotSyncedError,
    EtfDataService,
    UnknownEtfError,
)

# 测试交易日：2026-09-14/15/16
DAY_1, DAY_2, DAY_3 = date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)
DAYS = (DAY_1, DAY_2, DAY_3)


class FakeNameProvider:
    def get_name(self, market, asset_type, symbol):
        return None


# ---- 造数 ----

def _seed_etf(
    session_factory,
    *,
    symbol: str = "510300",
    name: str = "沪深300ETF",
    exchange: str = "SSE",
    daily_rows: dict[date, dict] | None = None,
    factor_rows: dict[date, float] | None = None,
):
    """造一只 ETF：instrument + cn_etf_basic + etf_daily + etf_adj_factor。

    daily_rows：{trade_date: 字段覆盖}；factor_rows：{trade_date: adj_factor}。
    None 表示不造该表数据。
    """
    from datetime import datetime

    instrument_id = f"CN:ETF:{symbol}"
    ts_code = f"{symbol}.{'SH' if exchange == 'SSE' else 'SZ'}"
    with session_factory() as session:
        session.add(
            Instrument(
                instrument_id=instrument_id, symbol=symbol, name=name,
                market="CN", asset_type="ETF", currency="CNY",
                exchange=exchange, is_active=True,
            )
        )
        session.flush()  # 先落 instrument，满足 cn_etf_basic 的 FK
        session.add(
            CnEtfBasic(
                instrument_id=instrument_id, ts_code=ts_code, symbol=symbol,
                name=name, exchange=exchange,
                source="eastmoney", fetched_at=datetime(2026, 9, 17),
                source_last_seen_at=datetime(2026, 9, 17),
            )
        )
        daily_table = HISTORY_FACT_TABLES["etf_daily"]
        for trade_date, overrides in (daily_rows or {}).items():
            values = dict(
                instrument_id=instrument_id, ts_code=ts_code, trade_date=trade_date,
                open=2.0, high=2.2, low=1.9, close=2.1,
                volume=1000, amount=2100.0, turnover_rate=1.5,
                source="eastmoney", fetched_at=date(2026, 9, 17),
            )
            values.update(overrides)
            session.execute(daily_table.insert().values(**values))
        factor_table = HISTORY_FACT_TABLES["etf_adj_factor"]
        for trade_date, adj_factor in (factor_rows or {}).items():
            session.execute(
                factor_table.insert().values(
                    instrument_id=instrument_id, ts_code=ts_code,
                    trade_date=trade_date, adj_factor=adj_factor,
                    source="tushare", fetched_at=date(2026, 9, 17),
                )
            )
        session.commit()
    return instrument_id, ts_code


# ===================================================================
# 服务层：raw 直读与基础行为
# ===================================================================


class TestRawQuery:
    def test_raw_returns_rows_ascending(self, session_factory):
        """raw 直读：区间行升序返回。"""
        _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={d: 1.0 for d in DAYS},
        )
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, DAY_3, adjust="raw"
            )
        assert [bar.trade_date for bar in result.items] == list(DAYS)
        assert result.symbol == "510300"
        assert result.name == "沪深300ETF"
        assert result.ts_code == "510300.SH"
        assert result.instrument_id == "CN:ETF:510300"
        assert result.adjust == "raw"

    def test_raw_ignores_factor_table(self, session_factory):
        """raw 查询不受因子表状态影响（全库无因子行也正常）。"""
        _seed_etf(session_factory, daily_rows={d: {} for d in DAYS}, factor_rows=None)
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, DAY_3, adjust="raw"
            )
        assert len(result.items) == 3

    def test_empty_range_returns_empty_items(self, session_factory):
        """空区间：正常返回空 items（200 语义）。"""
        _seed_etf(session_factory, daily_rows={d: {} for d in DAYS}, factor_rows={d: 1.0 for d in DAYS})
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", date(2026, 10, 1), date(2026, 10, 9)
            )
        assert result.items == []

    def test_unknown_symbol_raises(self, session_factory):
        """未知代码明确报错。"""
        with session_factory() as session:
            with pytest.raises(UnknownEtfError, match="未知 ETF 代码"):
                EtfDataService(session).get_etf_daily("999999", DAY_1, DAY_3)

    def test_null_price_fields_preserved(self, session_factory):
        """NULL 价格字段原样保留（合法脏数据）。"""
        _seed_etf(
            session_factory,
            daily_rows={DAY_1: {"open": None, "high": None, "low": None, "close": None}},
            factor_rows={DAY_1: 2.0},
        )
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, DAY_1, adjust="hfq"
            )
        bar = result.items[0]
        assert bar.open is None and bar.high is None and bar.low is None and bar.close is None


# ===================================================================
# 服务层：qfq/hfq 复权计算
# ===================================================================


class TestAdjustCalculation:
    """因子：09-14/15 = 1.0，09-16 = 1.5（factor_latest = 1.5）。

    raw close=2.1 →
    - hfq close = 2.1 × factor_t（09-16 = 3.15）
    - qfq close = 2.1 × factor_t / 1.5（09-16 = 2.1，当前价=真实价）
    """

    def test_hfq_formula(self, session_factory):
        _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={DAY_1: 1.0, DAY_2: 1.0, DAY_3: 1.5},
        )
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, DAY_3, adjust="hfq"
            )
        closes = {bar.trade_date: bar.close for bar in result.items}
        assert closes[DAY_1] == pytest.approx(2.1 * 1.0)
        assert closes[DAY_2] == pytest.approx(2.1 * 1.0)
        assert closes[DAY_3] == pytest.approx(2.1 * 1.5)

    def test_qfq_formula_with_latest_baseline(self, session_factory):
        """qfq = raw × factor_t / factor_latest；最新交易日 qfq 价 = raw 价。"""
        _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={DAY_1: 1.0, DAY_2: 1.0, DAY_3: 1.5},
        )
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, DAY_3, adjust="qfq"
            )
        closes = {bar.trade_date: bar.close for bar in result.items}
        assert closes[DAY_1] == pytest.approx(2.1 * 1.0 / 1.5)
        assert closes[DAY_3] == pytest.approx(2.1 * 1.5 / 1.5)  # 当前价=真实价

    def test_factor_latest_from_outside_range(self, session_factory):
        """区间外的最新因子行参与 qfq 基准（停牌日因子参与基准）。"""
        day_outside = date(2026, 9, 17)
        _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={DAY_1: 1.0, DAY_2: 1.0, DAY_3: 1.5, day_outside: 2.1},
        )
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, DAY_3, adjust="qfq"
            )
        # factor_latest = 2.1（全库最新，区间外）
        closes = {bar.trade_date: bar.close for bar in result.items}
        assert closes[DAY_1] == pytest.approx(2.1 * 1.0 / 2.1)
        assert closes[DAY_3] == pytest.approx(2.1 * 1.5 / 2.1)

    def test_adjust_only_applies_to_prices(self, session_factory):
        """复权只作用于价格字段；volume/amount/turnover_rate 不变。"""
        _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={DAY_1: 1.0, DAY_2: 1.0, DAY_3: 1.5},
        )
        with session_factory() as session:
            raw = EtfDataService(session).get_etf_daily("510300", DAY_1, DAY_3)
            hfq = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, DAY_3, adjust="hfq"
            )
        raw_by_date = {b.trade_date: b for b in raw.items}
        for bar in hfq.items:
            src = raw_by_date[bar.trade_date]
            assert bar.volume == src.volume
            assert bar.amount == src.amount
            assert bar.turnover_rate == src.turnover_rate


# ===================================================================
# 服务层：跨源一致性
# ===================================================================


class TestFactorConsistency:
    def test_factor_missing_days_raise_with_detail(self, session_factory):
        """区间内日线有行而因子缺失 → 明确报错列出缺失日。"""
        _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={DAY_1: 1.0},  # 09-15/16 因子缺失
        )
        with session_factory() as session:
            with pytest.raises(AdjustFactorMissingDaysError) as exc_info:
                EtfDataService(session).get_etf_daily(
                    "510300", DAY_1, DAY_3, adjust="qfq"
                )
        assert exc_info.value.missing_days == [DAY_2, DAY_3]
        assert DAY_2.isoformat() in str(exc_info.value)

    def test_factor_all_missing_raises_not_synced(self, session_factory):
        """因子全空（全库无因子行）→ "复权因子未同步"报错。"""
        _seed_etf(session_factory, daily_rows={d: {} for d in DAYS}, factor_rows=None)
        with session_factory() as session:
            with pytest.raises(AdjustFactorNotSyncedError, match="复权因子未同步"):
                EtfDataService(session).get_etf_daily(
                    "510300", DAY_1, DAY_3, adjust="hfq"
                )

    def test_suspended_day_factor_still_counts(self, session_factory):
        """因子存在而日线缺失（停牌日有因子行）→ 该日无行情输出，
        因子仍参与 factor_latest 基准。"""
        day_suspended = date(2026, 9, 17)  # 有因子行、无日线行
        _seed_etf(
            session_factory,
            daily_rows={DAY_1: {}, DAY_3: {}},  # DAY_2 停牌无日线
            factor_rows={DAY_1: 1.0, DAY_2: 1.2, DAY_3: 1.5, day_suspended: 3.0},
        )
        with session_factory() as session:
            result = EtfDataService(session).get_etf_daily(
                "510300", DAY_1, day_suspended, adjust="qfq"
            )
        # DAY_2 无日线行 → 不输出（日线缺失日无行情）；基准 = 3.0（区间外停牌日因子）
        assert [bar.trade_date for bar in result.items] == [DAY_1, DAY_3]
        closes = {bar.trade_date: bar.close for bar in result.items}
        assert closes[DAY_1] == pytest.approx(2.1 * 1.0 / 3.0)


# ===================================================================
# REST 端点：权限矩阵与响应序列化
# ===================================================================


class TestQuantEtfDailyApi:
    def _seed_full(self, session_factory):
        return _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={DAY_1: 1.0, DAY_2: 1.0, DAY_3: 1.5},
        )

    def test_requires_login_401(self, client_factory):
        """未登录 401。"""
        with client_factory(FakeNameProvider()) as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={"symbol": "510300", "start": "2026-09-14", "end": "2026-09-16"},
            )
        assert resp.status_code == 401

    def test_logged_in_user_200(self, client_factory, session_factory):
        """登录用户（非 admin 专属）即可访问。"""
        self._seed_full(session_factory)
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={
                    "symbol": "510300", "start": "2026-09-14",
                    "end": "2026-09-16", "adjust": "hfq",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["symbol"] == "510300"
        assert body["name"] == "沪深300ETF"
        assert body["ts_code"] == "510300.SH"
        assert body["instrument_id"] == "CN:ETF:510300"
        assert body["adjust"] == "hfq"
        assert len(body["items"]) == 3
        # 序列化：日期 ISO 格式、数值口径
        assert body["items"][0]["trade_date"] == "2026-09-14"
        assert body["items"][2]["close"] == pytest.approx(2.1 * 1.5)
        assert body["items"][0]["volume"] == 1000

    def test_symbol_not_six_digits_422(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={"symbol": "5103", "start": "2026-09-14", "end": "2026-09-16"},
            )
        assert resp.status_code == 422

    def test_adjust_enum_422(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={
                    "symbol": "510300", "start": "2026-09-14",
                    "end": "2026-09-16", "adjust": "front",
                },
            )
        assert resp.status_code == 422

    def test_start_after_end_422(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={"symbol": "510300", "start": "2026-09-16", "end": "2026-09-14"},
            )
        assert resp.status_code == 422
        assert "start" in resp.json()["detail"]

    def test_missing_required_params_422(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            assert client.get("/api/quant/etf/daily").status_code == 422
            assert client.get(
                "/api/quant/etf/daily", params={"symbol": "510300"}
            ).status_code == 422

    def test_unknown_symbol_404(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={"symbol": "999999", "start": "2026-09-14", "end": "2026-09-16"},
            )
        assert resp.status_code == 404
        assert "未知" in resp.json()["detail"]

    def test_factor_missing_409(self, client_factory, session_factory):
        """因子缺失：409 语义化错误，含缺失日明细。"""
        _seed_etf(
            session_factory,
            daily_rows={d: {} for d in DAYS},
            factor_rows={DAY_1: 1.0},
        )
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={
                    "symbol": "510300", "start": "2026-09-14",
                    "end": "2026-09-16", "adjust": "qfq",
                },
            )
        assert resp.status_code == 409
        assert "2026-09-15" in resp.json()["detail"]

    def test_factor_not_synced_409(self, client_factory, session_factory):
        _seed_etf(session_factory, daily_rows={d: {} for d in DAYS}, factor_rows=None)
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={
                    "symbol": "510300", "start": "2026-09-14",
                    "end": "2026-09-16", "adjust": "hfq",
                },
            )
        assert resp.status_code == 409
        assert "复权因子未同步" in resp.json()["detail"]

    def test_empty_range_200_empty_items(self, client_factory, session_factory):
        """空区间返回 200 空 items。"""
        self._seed_full(session_factory)
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={"symbol": "510300", "start": "2026-10-01", "end": "2026-10-09"},
            )
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_raw_200_without_factors(self, client_factory, session_factory):
        """raw 不受因子表状态影响：无因子行仍 200。"""
        _seed_etf(session_factory, daily_rows={d: {} for d in DAYS}, factor_rows=None)
        with client_factory(FakeNameProvider(), login_as="researcher") as client:
            resp = client.get(
                "/api/quant/etf/daily",
                params={"symbol": "510300", "start": "2026-09-14", "end": "2026-09-16"},
            )
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 3
