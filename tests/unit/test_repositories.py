"""仓储层单测：DuckDB 临时文件库。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.fundamental import FundamentalSnapshot
from app.models.quote import QuoteSnapshot
from app.repositories.fundamental import FundamentalRepository
from app.repositories.instrument import InstrumentRepository
from app.repositories.quote import QuoteSnapshotRepository
from app.repositories.watchlist import IndexWatchlistRepository, WatchlistRepository


def _make_instrument(session, instrument_id="CN:STOCK:600519", name="贵州茅台"):
    repo = InstrumentRepository(session)
    market, asset_type, symbol = instrument_id.split(":", 2)
    repo.upsert(
        instrument_id=instrument_id,
        symbol=symbol,
        name=name,
        market=market,
        asset_type=asset_type,
        currency="CNY" if market == "CN" else "HKD",
    )
    session.commit()


def test_instrument_upsert_idempotent(session):
    _make_instrument(session)
    _make_instrument(session, name="贵州茅台A")  # 二次写入不重复
    repo = InstrumentRepository(session)
    rows = session.query(repo.get("CN:STOCK:600519").__class__).all()
    assert len(rows) == 1
    assert repo.get("CN:STOCK:600519").name == "贵州茅台A"


def test_watchlist_add_duplicate_exists(session):
    _make_instrument(session)
    repo = WatchlistRepository(session)
    repo.add("CN:STOCK:600519", sort_order=10)
    session.commit()
    assert repo.exists("CN:STOCK:600519") is True


def test_watchlist_remove_keeps_instrument(session):
    _make_instrument(session)
    repo = WatchlistRepository(session)
    repo.add("CN:STOCK:600519")
    session.commit()

    assert repo.remove("CN:STOCK:600519") is True
    session.commit()
    assert repo.exists("CN:STOCK:600519") is False
    # instrument 历史数据保留
    assert InstrumentRepository(session).get("CN:STOCK:600519") is not None


def test_watchlist_reorder(session):
    _make_instrument(session, "CN:STOCK:600519")
    _make_instrument(session, "HK:STOCK:00700", name="腾讯控股")
    repo = WatchlistRepository(session)
    repo.add("CN:STOCK:600519", sort_order=20)
    repo.add("HK:STOCK:00700", sort_order=10)
    session.commit()

    repo.reorder({"CN:STOCK:600519": 10, "HK:STOCK:00700": 20})
    session.commit()
    ordered = [iid for row, _inst in repo.list_ordered() for iid in [row.instrument_id]]
    assert ordered == ["CN:STOCK:600519", "HK:STOCK:00700"]


def test_index_watchlist_separate_from_watchlist(session):
    _make_instrument(session, "CN:INDEX:000001", name="上证指数")
    wrepo = WatchlistRepository(session)
    irepo = IndexWatchlistRepository(session)
    irepo.add("CN:INDEX:000001")
    session.commit()
    # 指数只在 index_watchlist，不进入普通 watchlist
    assert wrepo.exists("CN:INDEX:000001") is False
    assert irepo.exists("CN:INDEX:000001") is True


def test_quote_snapshot_upsert_and_latest(session):
    # quote_snapshot.instrument_id 外键引用 instrument（DuckDB 强制执行）
    _make_instrument(session)
    repo = QuoteSnapshotRepository(session)
    now = datetime.now(UTC)
    older = now.replace(year=now.year - 1)
    # 先旧后新：最终保留最新一次成功行情
    repo.upsert(
        QuoteSnapshot(
            instrument_id="CN:STOCK:600519",
            price=1200.0,
            change_percent=-0.5,
            source="akshare",
            fetched_at=older,
        )
    )
    repo.upsert(
        QuoteSnapshot(
            instrument_id="CN:STOCK:600519",
            price=1450.12,
            change_percent=1.25,
            source="akshare",
            fetched_at=now,
        )
    )
    session.commit()

    latest = repo.latest_many(["CN:STOCK:600519"])
    assert float(latest["CN:STOCK:600519"].price) == 1450.12


def test_fundamental_unique_per_trade_date(session):
    # fundamental_snapshot.instrument_id 外键引用 instrument（DuckDB 强制执行）
    _make_instrument(session)
    repo = FundamentalRepository(session)
    for pe in (20.0, 21.31):
        repo.upsert(
            FundamentalSnapshot(
                instrument_id="CN:STOCK:600519",
                trade_date=__import__("datetime").date(2026, 8, 18),
                pe_ttm=pe,
                pb=7.21,
                dividend_yield_ttm=3.12,
                source="tushare",
            )
        )
    session.commit()
    assert float(repo.latest("CN:STOCK:600519").pe_ttm) == 21.31
    assert repo.covered_instrument_ids(
        __import__("datetime").date(2026, 8, 18)
    ) == {"CN:STOCK:600519"}


def test_covered_ids_exclude_partial_null_rows(session):
    """任一指标为空的当日行不视为已覆盖（数据源当日指标延迟生成）。"""
    # fundamental_snapshot.instrument_id 外键引用 instrument（DuckDB 强制执行）
    _make_instrument(session, "CN:STOCK:600519")
    _make_instrument(session, "CN:STOCK:000001", name="平安银行")
    repo = FundamentalRepository(session)
    d = __import__("datetime").date(2026, 8, 18)
    repo.upsert(
        FundamentalSnapshot(
            instrument_id="CN:STOCK:600519", trade_date=d,
            pe_ttm=21.31, pb=7.21, dividend_yield_ttm=3.12, source="tushare",
        )
    )
    # pe/pb 有值、仅股息率为空（当日延迟生成的典型形态）
    repo.upsert(
        FundamentalSnapshot(
            instrument_id="CN:STOCK:000001", trade_date=d,
            pe_ttm=5.1, pb=0.47, source="tushare",
        )
    )
    session.commit()
    assert repo.covered_instrument_ids(d) == {"CN:STOCK:600519"}
