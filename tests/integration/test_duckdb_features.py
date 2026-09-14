"""DuckDB 特性集成测试（技术方案 §6/§9 / design D1/D3/D5/D6/D8/D10）。

覆盖 DuckDB 在本项目中用到的核心特性与语义验证：
- 复合主键 upsert 幂等（design D1：业务主键 + 应用层 upsert）；
- 显式 sequence 取号（design D3：不依赖数据库自增，跨库可移植）；
- 单键 upsert 幂等（quote_snapshot 一证券一行）；
- TIMESTAMPTZ 时区往返（design D5：统一时区，避免时区漂移）；
- 并发写序列化（design D8：单写者模型，write_coordinator 串行化）；
- 交易日历复合主键幂等。

使用 conftest 的 engine/session/session_factory fixtures（init_db 建表），
不走 Alembic 迁移路径——迁移正确性由 test_migrations 单独保障。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.db import write_coordinator
from app.models import (
    FundamentalSnapshot,
    Instrument,
    QuoteSnapshot,
    Tag,
    TradingCalendarDay,
)
from app.repositories.fundamental import FundamentalRepository
from app.repositories.quote import QuoteSnapshotRepository
from app.repositories.trading_calendar import TradingCalendarRepository


# ---------------------------------------------------------------------------
# 1. 复合主键 upsert
# ---------------------------------------------------------------------------

def test_fundamental_composite_pk_upsert_is_idempotent(session):
    """fundamental_snapshot 复合主键 (instrument_id, trade_date)：两次 upsert 只保留一行，值为第二次。

    验证 design D1（业务主键模型）与 FundamentalRepository.upsert 的
    on_conflict_do_update 语义；同键重复刷新不应产生冗余行。
    """
    # 先建 instrument（外键依赖）
    session.add(Instrument(
        instrument_id="CN:STOCK:600519",
        symbol="600519",
        name="贵州茅台",
        market="CN",
        asset_type="STOCK",
        currency="CNY",
    ))
    session.commit()

    repo = FundamentalRepository(session)
    day = date(2026, 9, 10)

    # 第一次 upsert
    repo.upsert(FundamentalSnapshot(
        instrument_id="CN:STOCK:600519",
        trade_date=day,
        pe_ttm=25.5,
        pb=8.2,
        dividend_yield_ttm=2.1,
        source="tushare",
    ))
    session.commit()

    # 第二次 upsert（同键，修改指标）
    repo.upsert(FundamentalSnapshot(
        instrument_id="CN:STOCK:600519",
        trade_date=day,
        pe_ttm=26.0,
        pb=8.5,
        dividend_yield_ttm=2.3,
        source="tushare",
    ))
    session.commit()

    rows = session.query(FundamentalSnapshot).filter(
        FundamentalSnapshot.instrument_id == "CN:STOCK:600519",
        FundamentalSnapshot.trade_date == day,
    ).all()
    assert len(rows) == 1, "复合主键 upsert 后行数应为 1"
    # DECIMAL 列读回 Decimal，须用 Decimal 字面量比较（float 2.3 表示不精确）
    assert rows[0].pe_ttm == Decimal("26.000000"), "upsert 后值应为第二次写入"
    assert rows[0].pb == Decimal("8.500000")
    assert rows[0].dividend_yield_ttm == Decimal("2.300000")


# ---------------------------------------------------------------------------
# 2. sequence 取号
# ---------------------------------------------------------------------------

def test_tag_sequence_generates_incrementing_ids(session, user_factory):
    """tag.tag_id 由 seq_tag_id sequence 生成：两次 flush 后 tag_id 为正整数且递增。

    验证 design D3（显式 sequence 取代自增主键）；
    不依赖任何数据库方言的 autoincrement 行为，跨 SQLite/DuckDB/PostgreSQL 一致。

    注：SQLAlchemy Sequence 在 flush 时是否立即触发 nextval 取决于方言实现。
    若 flush 后 tag_id 仍为 None（未触发），需改为 commit 后再断言——
    以实际行为为准，Phase 0 环境就绪后校准。

    multi-user-auth：tag 为用户私有数据（user_id NOT NULL），
    须先建属主用户再写 tag；sequence 语义本身不受影响。
    """
    owner = user_factory("tag_owner")
    tag1 = Tag(user_id=owner["user_id"], name="高股息")
    tag2 = Tag(user_id=owner["user_id"], name="成长股")
    session.add_all([tag1, tag2])
    session.flush()

    # flush 后 sequence 应已取号；若 DuckDB 方言延迟到 commit，下方断言可能失败
    assert tag1.tag_id is not None, "flush 后 tag1.tag_id 应由 sequence 填充"
    assert tag2.tag_id is not None, "flush 后 tag2.tag_id 应由 sequence 填充"
    assert tag1.tag_id > 0
    assert tag2.tag_id > tag1.tag_id

    session.commit()
    # commit 后再次确认（保险断言）
    assert tag1.tag_id is not None
    assert tag2.tag_id is not None
    assert tag2.tag_id > tag1.tag_id


# ---------------------------------------------------------------------------
# 3. upsert 幂等（单键：quote_snapshot）
# ---------------------------------------------------------------------------

def test_quote_snapshot_upsert_is_idempotent(session):
    """QuoteSnapshotRepository.upsert 同一 instrument_id 两次：只保留一行，值为第二次。

    验证 design D1（一证券一行，upsert 更新）与单主键冲突更新语义；
    行情刷新是高频操作，幂等性是正确性底线。
    """
    session.add(Instrument(
        instrument_id="CN:STOCK:600519",
        symbol="600519",
        name="贵州茅台",
        market="CN",
        asset_type="STOCK",
        currency="CNY",
    ))
    session.commit()

    repo = QuoteSnapshotRepository(session)

    repo.upsert(QuoteSnapshot(
        instrument_id="CN:STOCK:600519",
        price=1500.00,
        change_percent=1.25,
        source="tencent",
    ))
    session.commit()

    repo.upsert(QuoteSnapshot(
        instrument_id="CN:STOCK:600519",
        price=1520.00,
        change_percent=2.60,
        source="tencent",
    ))
    session.commit()

    assert session.query(QuoteSnapshot).count() == 1
    row = session.get(QuoteSnapshot, "CN:STOCK:600519")
    # DECIMAL 列读回 Decimal：非 2 的幂次分母的小数（如 2.6）float 表示不精确，
    # Decimal('2.600000') == 2.6 为 False，须用 Decimal 字面量比较（SQLite 时代返回 float 无此问题）
    assert row.price == Decimal("1520.000000")
    assert row.change_percent == Decimal("2.600000")


# ---------------------------------------------------------------------------
# 4. TIMESTAMPTZ 往返
# ---------------------------------------------------------------------------

def test_timestamptz_preserves_utc_aware_datetime(session):
    """TIMESTAMPTZ 列写入 aware UTC datetime，读出后 tzinfo 保留且时刻等值。

    验证 design D5（统一时区）：DuckDB 的 TIMESTAMPTZ 应保留时区信息，
    读出后仍是 UTC-aware datetime，不会降级为 naive。
    若读出为 naive 则说明时区信息丢失，需排查方言配置。
    """
    session.add(Instrument(
        instrument_id="CN:STOCK:600519",
        symbol="600519",
        name="贵州茅台",
        market="CN",
        asset_type="STOCK",
        currency="CNY",
    ))
    session.commit()

    sent = datetime(2026, 9, 13, 10, 30, 0, 123456, tzinfo=UTC)
    repo = QuoteSnapshotRepository(session)
    repo.upsert(QuoteSnapshot(
        instrument_id="CN:STOCK:600519",
        price=1500.00,
        source="tencent",
        fetched_at=sent,
    ))
    session.commit()

    row = session.get(QuoteSnapshot, "CN:STOCK:600519")
    fetched = row.fetched_at
    assert fetched.tzinfo is not None, "TIMESTAMPTZ 读出应保留时区信息（aware）"
    # 转换为 UTC 后比较 timestamp（微秒级精度）
    assert fetched.timestamp() == pytest.approx(sent.timestamp(), abs=1e-6), \
        "TIMESTAMPTZ 往返后时刻应精确一致"


# ---------------------------------------------------------------------------
# 5. 并发写序列化
# ---------------------------------------------------------------------------

def test_concurrent_writes_serialized_via_write_coordinator(engine, session_factory):
    """4 线程 × 各 5 次 upsert，全部走 write_coordinator.write() 串行化，无异常且行数正确。

    验证 design D8（单写者模型）：DuckDB 嵌入式数据库下，同表并发写事务
    会冲突，write_coordinator 进程级 RLock 确保事务串行提交，避免
    ``ConstraintException`` / ``TransactionException``。
    """
    # 先建 instrument（外键依赖，主线程一次性完成）
    from app.models import Instrument

    with session_factory() as s:
        for i in range(20):
            s.add(Instrument(
                instrument_id=f"CN:STOCK:TEST{i:02d}",
                symbol=f"TEST{i:02d}",
                name=f"测试标的{i}",
                market="CN",
                asset_type="STOCK",
                currency="CNY",
            ))
        s.commit()

    def worker(thread_id: int) -> list[str]:
        """每个线程 upsert 5 个不同 instrument_id 的行情快照。"""
        results = []
        for j in range(5):
            idx = thread_id * 5 + j
            instr_id = f"CN:STOCK:TEST{idx:02d}"
            with write_coordinator.write():
                with session_factory() as s:
                    repo = QuoteSnapshotRepository(s)
                    repo.upsert(QuoteSnapshot(
                        instrument_id=instr_id,
                        price=float(100 + idx),
                        change_percent=0.0,
                        source="test",
                    ))
                    s.commit()
            results.append(instr_id)
        return results

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker, tid) for tid in range(4)]
        # 应全部成功，不抛出任何异常
        for fut in as_completed(futures):
            fut.result()  # 若有异常会在此处重新抛出

    with session_factory() as s:
        count = s.query(QuoteSnapshot).count()
    assert count == 20, f"并发写入后应有 20 行，实际 {count} 行"


# ---------------------------------------------------------------------------
# 6. trading_calendar 复合主键幂等
# ---------------------------------------------------------------------------

def test_trading_calendar_save_days_idempotent(session):
    """TradingCalendarRepository.save_days 两遍同数据：不重复，count 不变。

    验证复合主键 (market, trade_date) 的幂等语义；
    交易日历刷新是周期性操作，重复执行不应产生脏数据。
    """
    repo = TradingCalendarRepository(session)
    days = [
        (date(2026, 9, 10), True),
        (date(2026, 9, 11), True),
        (date(2026, 9, 12), False),  # 周六休市
        (date(2026, 9, 13), False),  # 周日休市
    ]

    repo.save_days("CN", days)
    session.commit()
    first_count = session.query(TradingCalendarDay).filter(
        TradingCalendarDay.market == "CN"
    ).count()
    assert first_count == 4

    # 第二遍：同数据再存一次，行数应不变
    repo.save_days("CN", days)
    session.commit()
    second_count = session.query(TradingCalendarDay).filter(
        TradingCalendarDay.market == "CN"
    ).count()
    assert second_count == 4, "save_days 第二遍不应新增行"
