"""性能基准（a-share-historical-data 技术方案 §74；per-stock-history-sync tasks 9.1）。

两个场景，均走真实 Repository + 真实 DuckDB 临时库：

1. 整日替换（历史机制对照）：约 6000 行 × 100 个交易日 × 2 数据集，
   逐日"整日 delete + 批量 insert + state 更新"，并抽查事实表基础
   date filter 的读取路径；
2. 个股区间替换（v0.4.0 生产路径，design D14）：单股约 4000 行
   （16 年量级）× 多股连续提交，走真实 ``StockSyncExecutor``
   （fake registry 锁外返回合成批次），断言每股提交事务数固定为 2
   （task 创建 + 成功提交）、语句数不随股数/行数逐行放大。

目标不是设定绝对毫秒 SLA，而是证明实现没有退化成
"逐行 ORM / 每行一次 commit"。写入路径为"注册 DuckDB 视图 + INSERT
SELECT"（见 app/repositories/history_fact.py 的说明；实测相对 executemany
快约 14×）：

- 每行一次 commit 的退化会让提交次数与行数同阶（整日场景提交次数应
  恰好 = 数据集×交易日；个股场景 = 2×股数，均不随行数增长）；
- 逐行 ORM 的退化会让单行耗时逼近逐行基线。故本脚本在同机实测逐行
  基线（500 行样本外推），批量路径须显著更快（≥5×）——相对比值不受
  机器与负载漂移影响，比绝对毫秒阈值更可复现。

运行：.venv/bin/python scripts/bench/bench_history_write.py
退出码：0 通过（含各断言）；1 不达标。
"""

from __future__ import annotations

import dataclasses
import statistics
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import event  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.db import create_db_engine, init_db, make_session_factory  # noqa: E402
from app.models.history_sync import (  # noqa: E402
    DatasetKind,
    DatasetName,
    DatasetStatus,
    TriggerType,
)
from app.providers.base import AdjFactor, DailyBar, ProviderBatch  # noqa: E402
from app.repositories.history_fact import HistoryFactRepository, _table  # noqa: E402
from app.repositories.history_sync import (  # noqa: E402
    HistoryDayStatusRepository,
    HistorySyncRunDatasetRepository,
    HistorySyncRunRepository,
    HistorySyncStateRepository,
    StockSyncStateRepository,
)
from app.services.history.retry import RetryPolicy  # noqa: E402
from app.services.history.stock_executor import StockSyncExecutor  # noqa: E402
from app.services.market_session_service import now_beijing  # noqa: E402

ROWS_PER_DAY = 6000
DAYS = 100
DATASETS = (DatasetName.DAILY, DatasetName.ADJ_FACTOR)
BEIJING = timezone(timedelta(hours=8))

# 个股区间场景（design D14：单股 16 年约 4000 行一次事务）
STOCKS = 20
ROWS_PER_STOCK = 4000


def _make_records(dataset: DatasetName, trade_date: date, rows: int) -> list:
    """构造 rows 行内部标准模型记录（6 位证券代码循环复用）。"""
    out = []
    for i in range(rows):
        symbol = f"{i % 1000000:06d}"
        instrument_id = f"CN:STOCK:{symbol}"
        ts_code = f"{symbol}.SH"
        if dataset is DatasetName.DAILY:
            out.append(
                DailyBar(
                    instrument_id=instrument_id,
                    ts_code=ts_code,
                    trade_date=trade_date,
                    open=10.0 + i % 97 / 100,
                    high=10.5 + i % 97 / 100,
                    low=9.5 + i % 97 / 100,
                    close=10.2 + i % 97 / 100,
                    pre_close=10.1 + i % 97 / 100,
                    change=0.1,
                    pct_chg=0.99,
                    vol=1_000_000 + i,
                    amount=10_200_000.0 + i,
                )
            )
        else:
            out.append(
                AdjFactor(
                    instrument_id=instrument_id,
                    ts_code=ts_code,
                    trade_date=trade_date,
                    adj_factor=1.0 + i % 13 / 100,
                )
            )
    return out


def _make_stock_records(instrument_id: str, ts_code: str, start: date, rows: int) -> list:
    """单股 rows 个交易日的日线记录（合法 OHLC，供 validate_batch 通过）。"""
    out = []
    for i in range(rows):
        base = 10.0 + i % 97 / 100
        out.append(
            DailyBar(
                instrument_id=instrument_id,
                ts_code=ts_code,
                trade_date=start + timedelta(days=i),
                open=base,
                high=base + 0.5,
                low=base - 0.5,
                close=base + 0.2,
                pre_close=base + 0.1,
                change=0.1,
                pct_chg=0.99,
                vol=1_000_000 + i,
                amount=10_200_000.0 + i,
            )
        )
    return out


class _BenchRegistry:
    """fake Provider registry：锁外一次性返回该股全区间合成批次。"""

    source = "bench"

    def __init__(self, records_by_instrument: dict[str, list]):
        self._records = records_by_instrument

    def get_history_by_stock(self, dataset, instrument, start_date, end_date):
        records = self._records[instrument.instrument_id]
        return ProviderBatch(
            records=records,
            source=self.source,
            raw_row_count=len(records),
            truncation_risk=False,
        )


def run_stock_range_scenario(session_factory, config: AppConfig, counters: dict) -> dict:
    """个股区间场景（tasks 9.1，design D14）：真实 StockSyncExecutor 连续提交
    多股，返回计数统计供断言——每股固定 2 个写事务（task 创建 + 成功提交），
    语句数不随股数/行数逐行放大。``counters`` 为主流程的提交/语句计数器
    （commit 基线在预置事务之后记录，不计入场景断言）。
    """
    start = date(2010, 1, 4)
    records_by_instrument: dict[str, list] = {}
    with session_factory() as session:
        from app.models import Instrument

        instruments = []
        for i in range(STOCKS):
            instrument_id = f"CN:STOCK:{600000 + i}"
            instruments.append(
                Instrument(
                    instrument_id=instrument_id,
                    market="CN",
                    asset_type="STOCK",
                    symbol=f"{600000 + i}",
                    name=f"个股{i:02d}",
                )
            )
            records_by_instrument[instrument_id] = _make_stock_records(
                instrument_id, f"{600000 + i}.SH", start, ROWS_PER_STOCK
            )
        session.add_all(instruments)

        # executor 事务②依赖的 run / run_dataset / history_sync_state 预置
        run_id = "bench-stock-range-run"
        run_repo = HistorySyncRunRepository(session)
        if run_repo.get(run_id) is None:
            run_repo.create(
                run_id, trigger_type=TriggerType.MANUAL,
                requested_by_user_id=None, started_at=now_beijing(),
            )
        rd_repo = HistorySyncRunDatasetRepository(session)
        if rd_repo.get(run_id, DatasetName.DAILY) is None:
            rd_repo.start(
                run_id, DatasetName.DAILY,
                start_watermark=None, target_trade_date=None,
                started_at=now_beijing(),
            )
        HistorySyncStateRepository(session).ensure(
            DatasetName.DAILY,
            dataset_kind=DatasetKind.DAILY_CONTIGUOUS,
            history_start_date=start,
        )
        session.commit()

    # 预置事务不计入场景：基线在预置完成后记录
    commit_base = counters["commits"]["n"]
    stmt_base = counters["statements"]["n"]

    registry = _BenchRegistry(records_by_instrument)
    executor = StockSyncExecutor(config, session_factory, registry, RetryPolicy(config))

    # 逐股连续提交（一次拉全区间，watermark 从 None 起）
    wall_start = time.perf_counter()
    for inst in instruments:
        records = records_by_instrument[inst.instrument_id]
        outcome = executor.execute(
            run_id=run_id,
            dataset=DatasetName.DAILY,
            instrument=inst,
            ts_code=records[0].ts_code,
            start_date=records[0].trade_date,
            end_date=records[-1].trade_date,
            list_date=None,
            delist_date=None,
        )
        assert outcome.status == "success", f"基准路径不应失败: {outcome.error_message}"
    wall_seconds = time.perf_counter() - wall_start

    with session_factory() as session:
        state = StockSyncStateRepository(session).get(
            DatasetName.DAILY, instruments[0].instrument_id
        )
        assert state is not None and state.watermark_date == records_by_instrument[
            instruments[0].instrument_id
        ][-1].trade_date, "基准路径应推进水位到区间终点"
    return {
        "wall_seconds": wall_seconds,
        "total_rows": ROWS_PER_STOCK * STOCKS,
        "commits": counters["commits"]["n"] - commit_base,
        "stmts": counters["statements"]["n"] - stmt_base,
    }


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="marketmind-bench-")
    db_path = Path(tmpdir) / "bench.duckdb"
    engine = create_db_engine(f"duckdb:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    # 统计提交次数与语句数：确认不是"每行一次 commit"
    commits = {"n": 0}
    statements = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count_stmts(conn, cursor, statement, parameters, context, executemany):
        statements["n"] += 1

    @event.listens_for(engine, "commit")
    def _count_commits(conn):
        commits["n"] += 1

    # 事实表外键指向 instrument，先建 6000 个证券占位
    start = date(2024, 1, 2)
    dates = [start + timedelta(days=i) for i in range(DAYS)]
    now = datetime.now(BEIJING)
    with session_factory() as session:
        from app.models import Instrument

        session.add_all(
            [
                Instrument(
                    instrument_id=f"CN:STOCK:{i:06d}",
                    market="CN",
                    asset_type="STOCK",
                    symbol=f"{i:06d}",
                    name=f"证券{i:06d}",
                )
                for i in range(ROWS_PER_DAY)
            ]
        )
        # state 行必须预先存在（complete_day 要求 ensure 过）
        state_repo = HistorySyncStateRepository(session)
        for dataset in DATASETS:
            state_repo.ensure(
                dataset,
                dataset_kind=DatasetKind.DAILY_CONTIGUOUS,
                history_start_date=start,
            )
        session.commit()

    per_day_ms: list[float] = []
    fetch_ms: list[float] = []
    commit_before = commits["n"]
    total_rows = 0
    wall_start = time.perf_counter()

    for trade_date in dates:
        day_start = time.perf_counter()
        for dataset in DATASETS:
            records = _make_records(dataset, trade_date, ROWS_PER_DAY)
            total_rows += len(records)
            # 单日原子替换事务（§22 步骤 6~10），此处直接提交以模拟唯一写者
            with session_factory() as session:
                fact_repo = HistoryFactRepository(session)
                state_repo = HistorySyncStateRepository(session)
                day_repo = HistoryDayStatusRepository(session)

                old_count = fact_repo.count_for_date(dataset, trade_date)
                fact_repo.delete_for_date(dataset, trade_date)
                fact_repo.insert_records(
                    dataset, records, source="bench", fetched_at=now
                )
                day_repo.upsert_complete(
                    dataset,
                    trade_date,
                    row_count=len(records),
                    run_id="bench-run",
                    fetched_at=now,
                )
                state_repo.complete_day(
                    dataset,
                    trade_date,
                    rows_delta=len(records) - old_count,
                    status=DatasetStatus.CAUGHT_UP,
                )
                session.commit()
        per_day_ms.append((time.perf_counter() - day_start) * 1000)

    wall_seconds = time.perf_counter() - wall_start
    commits_total = commits["n"] - commit_before

    # 事实表基础 date filter（单日读取路径）
    with session_factory() as session:
        fact_repo = HistoryFactRepository(session)
        for _ in range(20):
            t0 = time.perf_counter()
            n = fact_repo.count_for_date(DatasetName.DAILY, dates[50])
            fetch_ms.append((time.perf_counter() - t0) * 1000)
        assert n == ROWS_PER_DAY, f"读取行数异常: {n}"

    n_days = len(per_day_ms)
    median_day = statistics.median(per_day_ms)

    # 同机逐行基线：直接对应 §74 要排除的"逐行 ORM"退化形态。
    # 取样本行（500 行）外推，避免真跑满 6000 行拖长基准本身。
    baseline_rows = 500
    baseline_records = _make_records(DatasetName.DAILY, dates[0], baseline_rows)
    with session_factory() as session:
        table = _table(DatasetName.DAILY)
        session.execute(table.delete())
        t0 = time.perf_counter()
        for record in baseline_records:
            session.execute(
                table.insert(),
                {
                    **{
                        f.name: getattr(record, f.name)
                        for f in dataclasses.fields(record)
                        if f.name in table.columns
                    },
                    "source": "bench-baseline",
                    "fetched_at": now,
                },
            )
        per_row_ms = (time.perf_counter() - t0) * 1000 / baseline_rows
        session.rollback()
    # 批量路径的单行耗时 vs 逐行路径的单行耗时
    batch_per_row_ms = (median_day / len(DATASETS)) / ROWS_PER_DAY
    batch_speedup = per_row_ms / batch_per_row_ms if batch_per_row_ms else 0.0

    # ---- 场景 2：个股区间替换（v0.4.0 生产路径，tasks 9.1 / design D14）----
    stock = run_stock_range_scenario(
        session_factory, AppConfig(),
        {"commits": commits, "statements": statements},
    )
    stock_commits = stock["commits"]
    stock_stmts = stock["stmts"]
    stock_rows = stock["total_rows"]
    stock_per_row_ms = (stock["wall_seconds"] * 1000) / stock_rows
    stock_speedup = per_row_ms / stock_per_row_ms if stock_per_row_ms else 0.0

    engine.dispose()

    p95_day = sorted(per_day_ms)[int(n_days * 0.95) - 1]
    median_fetch = statistics.median(fetch_ms)
    rows_per_second = total_rows / wall_seconds

    print("=" * 68)
    print("synthetic 性能基准（技术方案 §74 + per-stock-history-sync D14）")
    print("=" * 68)
    print("场景 1：整日替换（历史机制对照）")
    print(f"数据集 × 交易日      : {len(DATASETS)} × {DAYS}（共 {len(DATASETS) * DAYS} 次整日替换）")
    print(f"每次替换行数         : {ROWS_PER_DAY}")
    print(f"总写入行数           : {total_rows}")
    print(f"总耗时               : {wall_seconds:.2f} s（{rows_per_second:,.0f} 行/秒）")
    print(f"单日替换耗时 中位数  : {median_day:.1f} ms（两个数据集合计）")
    print(f"单日替换耗时 p95     : {p95_day:.1f} ms")
    print(f"提交次数             : {commits_total}（期望 = {len(DATASETS) * DAYS}）")
    print(f"SQL 语句总数         : {statements['n'] - stock_stmts:,}（期望远小于行数）")
    print(f"基础 date filter 耗时: {median_fetch:.2f} ms（中位数，6000 行）")
    print(
        f"逐行基线             : {per_row_ms:.2f} ms/行 → 批量 {batch_per_row_ms:.3f} ms/行"
        f"（{batch_speedup:.1f}× 更快）"
    )
    print("场景 2：个股区间替换（真实 StockSyncExecutor）")
    print(f"股数 × 每股行数      : {STOCKS} × {ROWS_PER_STOCK}（16 年量级）")
    print(f"总写入行数           : {stock_rows}")
    print(f"总耗时               : {stock['wall_seconds']:.2f} s"
          f"（{stock_rows / stock['wall_seconds']:,.0f} 行/秒）")
    print(f"提交次数             : {stock_commits}（期望 = 2 × {STOCKS}：task 创建 + 成功提交）")
    print(f"SQL 语句总数         : {stock_stmts:,}（期望远小于行数）")
    print(
        f"单行耗时             : {stock_per_row_ms:.3f} ms/行"
        f"（相对逐行基线 {stock_speedup:.1f}× 更快）"
    )
    print("-" * 68)

    checks = [
        # 提交次数恰为"数据集 × 交易日"：证明不是每行/每日一次 commit 之外的退化
        ("提交次数 == 数据集×交易日", commits_total == len(DATASETS) * DAYS),
        # 语句数远小于行数：证明是批量写入而不是逐行 ORM
        ("SQL 语句数 < 总行数的 1/100", (statements["n"] - stock_stmts) < total_rows / 100),
        # §74 的判定标准是"没有退化成逐行 ORM"，而非某个绝对毫秒数：
        # 同一台机器上实测逐行基线，批量路径必须显著更快（≥5×）。
        # 绝对阈值会随机器/负载漂移，相对比值才是可复现的退化守卫。
        (f"批量写入比逐行基线快 ≥5×（实测 {batch_speedup:.1f}×）", batch_speedup >= 5.0),
        # D14：每股固定 2 个写事务（task 创建 + 成功提交），不随行数/股数放大
        (f"个股提交次数 == 2×股数（实测 {stock_commits}）", stock_commits == 2 * STOCKS),
        (f"个股 SQL 语句数 < 总行数的 1/100（实测 {stock_stmts}）", stock_stmts < stock_rows / 100),
        (f"个股批量写入比逐行基线快 ≥5×（实测 {stock_speedup:.1f}×）", stock_speedup >= 5.0),
    ]
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print("=" * 68)
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
