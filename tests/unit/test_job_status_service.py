"""JobStatusService 单元测试（v0.03 技术方案 §36.4 + design D15）。

覆盖：首次成功 / 连续成功 / 失败 / 连续失败 / 失败后成功五场景，
写入失败被吞不向调用方传播，跨"重启"（重建 engine/service）状态持久化，
以及 QuoteRefreshJob / FundamentalRefreshJob 的包装接入。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import create_db_engine, init_db
from app.jobs.fundamental_refresh import FundamentalRefreshJob
from app.jobs.quote_refresh import QuoteRefreshJob
from app.services.job_status_service import JobStatusService


def _get(factory, job_name="quote_refresh"):
    from app.models.job_status import JobStatus

    with factory() as s:
        return s.get(JobStatus, job_name)


# ---- 五场景（技术方案 §36.4）----


def test_first_success(session_factory):
    svc = JobStatusService(session_factory)
    svc.record_success("quote_refresh", 1200)

    row = _get(session_factory)
    assert row.last_success_at is not None
    assert row.last_duration_ms == 1200
    assert row.consecutive_failures == 0


def test_consecutive_success_updates_timestamp(session_factory):
    svc = JobStatusService(session_factory)
    svc.record_success("quote_refresh", 100)
    first = _get(session_factory).last_success_at
    svc.record_success("quote_refresh", 200)

    row = _get(session_factory)
    assert row.last_duration_ms == 200
    assert row.last_success_at >= first


def test_failure_keeps_last_success(session_factory):
    """失败不清除 last_success_at（技术方案 §24 核心语义）。"""
    svc = JobStatusService(session_factory)
    svc.record_success("quote_refresh", 100)
    success_at = _get(session_factory).last_success_at

    svc.record_failure("quote_refresh", 50, "boom")

    row = _get(session_factory)
    assert row.last_success_at == success_at  # 保留
    assert row.last_error_at is not None
    assert row.last_error == "boom"
    assert row.last_duration_ms == 50
    assert row.consecutive_failures == 1


def test_consecutive_failures_accumulate(session_factory):
    svc = JobStatusService(session_factory)
    svc.record_failure("quote_refresh", 10, "e1")
    svc.record_failure("quote_refresh", 20, "e2")

    row = _get(session_factory)
    assert row.consecutive_failures == 2
    assert row.last_error == "e2"


def test_success_after_failures_resets_counter(session_factory):
    svc = JobStatusService(session_factory)
    for i in range(3):
        svc.record_failure("quote_refresh", 10, f"e{i}")
    svc.record_success("quote_refresh", 300)

    row = _get(session_factory)
    assert row.consecutive_failures == 0
    assert row.last_success_at is not None
    # last_error 历史保留（最近一次错误信息），但连续失败清零
    assert row.last_error == "e2"


def test_record_started(session_factory):
    svc = JobStatusService(session_factory)
    svc.record_started("quote_refresh")
    row = _get(session_factory)
    assert row.last_started_at is not None
    assert row.last_success_at is None  # 尚未成功


def test_timestamps_aware_beijing(session_factory):
    """DuckDB TIMESTAMPTZ 保真往返（design D4），时间戳 aware 落库，展示层换算北京时间。

    曾因 SQLite naive + UTC 落库导致 /api/admin/status 时间偏差 8 小时（v0.03 线上修复）。
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    svc = JobStatusService(session_factory)
    svc.record_started("quote_refresh")
    svc.record_failure("quote_refresh", 10, "x")
    svc.record_success("quote_refresh", 100)
    row = _get(session_factory)

    now_beijing = datetime.now(ZoneInfo("Asia/Shanghai"))
    for field in ("last_started_at", "last_error_at", "last_success_at"):
        value = getattr(row, field)
        assert value.tzinfo is not None  # aware，DuckDB TIMESTAMPTZ 读出带时区
        assert abs((value.astimezone(ZoneInfo("Asia/Shanghai")) - now_beijing)) < timedelta(
            minutes=2
        )  # 北京时刻，非 UTC


# ---- 观测设施故障不拖垮主流程 ----


def test_write_failure_swallowed(session_factory):
    """session_factory 抛异常时，record_* 不向调用方传播（design D8 风险缓解）。"""
    svc = JobStatusService(session_factory)

    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("db broken")

    broken = JobStatusService(BrokenFactory())
    broken.record_started("quote_refresh")  # 不抛
    broken.record_success("quote_refresh", 100)  # 不抛
    broken.record_failure("quote_refresh", 100, "x")  # 不抛

    # 正常 factory 仍可写入（主流程未受影响）
    svc.record_success("quote_refresh", 200)
    assert _get(session_factory).last_duration_ms == 200


# ---- 跨"重启"持久化 ----


def test_state_survives_service_rebuild(tmp_path):
    """文件库：Job 成功后重建 service/engine，状态仍为重启前的值。"""
    db = tmp_path / "jobs.duckdb"
    engine1 = create_db_engine(f"duckdb:///{db}")
    init_db(engine1)
    factory1 = sessionmaker(bind=engine1, autoflush=False, expire_on_commit=False)

    JobStatusService(factory1).record_success("quote_refresh", 900)
    expected = _get(factory1).last_success_at
    engine1.dispose()

    engine2 = create_db_engine(f"duckdb:///{db}")
    factory2 = sessionmaker(bind=engine2, autoflush=False, expire_on_commit=False)
    rows = JobStatusService(factory2).get_all()
    assert len(rows) == 1
    assert rows[0].job_name == "quote_refresh"
    assert rows[0].last_success_at == expected
    assert rows[0].last_duration_ms == 900
    engine2.dispose()


# ---- Job 包装接入 ----


class FakeRefreshService:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls = 0

    def tick(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        from app.services.refresh_service import RefreshResult

        return RefreshResult(updated=1, failed=0, markets=["CN"])


def test_quote_refresh_job_records_success_and_failure(session_factory):
    svc = JobStatusService(session_factory)
    ok = QuoteRefreshJob.__new__(QuoteRefreshJob)
    ok.refresh_seconds = 60
    ok.refresh_service = FakeRefreshService()
    ok.job_status = svc
    ok._task = None

    ok._tick_with_status()
    row = _get(session_factory)
    assert row.last_success_at is not None
    assert row.consecutive_failures == 0

    bad = QuoteRefreshJob.__new__(QuoteRefreshJob)
    bad.refresh_seconds = 60
    bad.refresh_service = FakeRefreshService(RuntimeError("provider down"))
    bad.job_status = svc
    bad._task = None

    bad._tick_with_status()
    row = _get(session_factory)
    assert row.consecutive_failures == 1
    assert "provider down" in row.last_error
    assert row.last_success_at is not None  # 失败不清除


def test_fundamental_job_records_via_wrapper(session_factory):
    svc = JobStatusService(session_factory)

    class StubJob(FundamentalRefreshJob):
        def __init__(self):
            self.job_status = svc
            self._maybe_run_called = False

        def _maybe_run(self):
            self._maybe_run_called = True

    StubJob()._maybe_run_with_status()
    row = _get(session_factory, "fundamental_refresh")
    assert row.last_success_at is not None
    assert row.consecutive_failures == 0
