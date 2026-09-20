"""HistorySyncJob 离线单测（tasks 6.3，design.md 第 10 节 D17~D18）。

Service 用 stub 替身（本文件只验证 Job 的调度与互斥职责，不触碰数据库）：
定时到点触发、startup catch-up、single-flight 并发只有一个 run、优雅停机
置 cancellation event、JobStatusService 记录。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import AppConfig
from app.jobs import history_sync as job_module
from app.jobs.history_sync import HistorySyncJob, _parse_schedule_time
from app.models.history_sync import TriggerType

BEIJING = ZoneInfo("Asia/Shanghai")


class StubService:
    """HistorySyncService 替身：记录调用并可控阻塞（验证 single-flight）。"""

    def __init__(self, *, block: threading.Event | None = None):
        self.calls: list[tuple[TriggerType, str | None]] = []
        self.events: list[threading.Event | None] = []
        self.run_ids: list[str | None] = []
        self.block = block
        self.session_factory = None
        # 已进入 run()（测试用事件同步，不做 sleep 轮询）
        self.entered = threading.Event()

    def run(
        self, *, trigger, requested_by_user_id=None, cancellation_event=None, run_id=None
    ) -> str:
        self.entered.set()
        self.calls.append((trigger, requested_by_user_id))
        self.events.append(cancellation_event)
        self.run_ids.append(run_id)
        if self.block is not None:
            self.block.wait(timeout=5)
        # 调用方预留给定时触发为空时应自行生成；此处回显以便断言
        return run_id or f"run-{len(self.calls)}"


class StubJobStatus:
    def __init__(self):
        self.started: list[str] = []
        self.success: list[str] = []
        self.failure: list[tuple[str, str]] = []

    def record_started(self, name):
        self.started.append(name)

    def record_success(self, name, duration_ms):
        self.success.append(name)

    def record_failure(self, name, duration_ms, error):
        self.failure.append((name, error))


def make_job(**overrides) -> tuple[HistorySyncJob, StubService, StubJobStatus]:
    config = AppConfig()
    for key, value in overrides.items():
        setattr(config.history, key, value)
    service = StubService()
    status = StubJobStatus()
    return HistorySyncJob(config, service, status), service, status


# ---- schedule_time 解析 ----


class TestScheduleTimeParsing:
    def test_valid_value_parsed(self):
        parsed = _parse_schedule_time("20:30")
        assert (parsed.hour, parsed.minute) == (20, 30)

    @pytest.mark.parametrize("bad", ["2030", "", "aa:bb", "25:99"])
    def test_invalid_value_fails_fast(self, bad):
        """非法配置不静默降级：直接抛错，避免调度器"永不触发"却无人察觉。"""
        with pytest.raises(ValueError):
            _parse_schedule_time(bad)

    def test_default_is_2030(self):
        job, _s, _st = make_job()
        assert (job.schedule_time.hour, job.schedule_time.minute) == (20, 30)


# ---- 到点判定 ----


class TestShouldFireScheduled:
    def _moment(self, hour, minute, second=0):
        return datetime(2026, 9, 16, hour, minute, second, tzinfo=BEIJING)

    def test_fires_at_schedule_time(self):
        job, _s, _st = make_job()
        assert job._should_fire_scheduled(self._moment(20, 30)) is True

    def test_fires_within_tolerance_window(self):
        """sleep 抖动导致检查点略晚于整点：容差内仍触发。"""
        job, _s, _st = make_job()
        assert job._should_fire_scheduled(self._moment(20, 34)) is True

    def test_does_not_fire_before_schedule_time(self):
        job, _s, _st = make_job()
        assert job._should_fire_scheduled(self._moment(20, 29)) is False

    def test_does_not_fire_far_after_window(self):
        """容差窗口之外不补跑历史时点（缺口由 startup catch-up 与水位负责）。"""
        job, _s, _st = make_job()
        assert job._should_fire_scheduled(self._moment(21, 0)) is False

    def test_fires_only_once_per_day(self):
        job, _s, _st = make_job()
        assert job._should_fire_scheduled(self._moment(20, 30)) is True
        job._last_scheduled_date = self._moment(20, 30).date()
        assert job._should_fire_scheduled(self._moment(20, 31)) is False

    def test_weekend_also_fires(self):
        """含周末运行（design D19）：不产生虚假交易日且可追平周五缺口。"""
        config = AppConfig()
        config.history.schedule_time = "20:30"
        job = HistorySyncJob(config, StubService(), StubJobStatus())
        saturday = datetime(2026, 9, 19, 20, 30, tzinfo=BEIJING)
        assert saturday.weekday() == 5
        assert job._should_fire_scheduled(saturday) is True


# ---- single-flight ----


class TestSingleFlight:
    async def test_concurrent_scheduled_trigger_only_one_run(self):
        """spec"任务互斥"：运行中再次触发不得产生第二个 run。"""
        block = threading.Event()
        config = AppConfig()
        job = HistorySyncJob(config, StubService(block=block), StubJobStatus())

        first = asyncio.create_task(job.run_scheduled(TriggerType.SCHEDULED))
        await asyncio.sleep(0.15)  # 让第一个真正进入执行
        second = await job.run_scheduled(TriggerType.SCHEDULED)
        assert second is None, "运行中重复触发应被跳过"

        block.set()
        assert await first == "run-1"
        assert len(job.history.calls) == 1

    async def test_scheduled_allowed_after_previous_finishes(self):
        job, service, _st = make_job()
        assert await job.run_scheduled(TriggerType.SCHEDULED) == "run-1"
        assert await job.run_scheduled(TriggerType.SCHEDULED) == "run-2"
        assert len(service.calls) == 2

    async def test_is_running_reflects_state(self):
        block = threading.Event()
        job = HistorySyncJob(AppConfig(), StubService(block=block), StubJobStatus())
        assert job.is_running is False
        task = asyncio.create_task(job.run_scheduled(TriggerType.SCHEDULED))
        await asyncio.sleep(0.15)
        assert job.is_running is True
        block.set()
        await task
        assert job.is_running is False

    def test_try_begin_rejects_second_manual_trigger(self):
        """管理员手动触发：API 层用 try_begin 判定 409。"""
        job, _s, _st = make_job()
        assert job.try_begin() is True
        assert job.try_begin() is False, "运行中第二次应失败（API 回 409）"
        job._end()
        assert job.try_begin() is True

    def test_run_manual_passes_server_side_user_id(self):
        """requested_by_user_id 由服务端传入，Job 原样交给 Service。"""
        job, service, _st = make_job()
        assert job.try_begin()
        assert job.run_manual("user-42") == "run-1"
        assert service.calls == [(TriggerType.MANUAL, "user-42")]
        assert job.is_running is False, "手动执行结束应释放 single-flight"

    def test_reserved_run_id_visible_before_run_row_exists(self):
        """回归：预留 run_id 在 try_begin 后立即可见（run 行尚未落库时的 409）。

        真实竞态：请求 1 持有 single-flight 但后台线程尚未 create run 行，
        此时请求 2 的 409 若只能查库就会拿到 null，违反 §52.2 的 run_id 必填。
        """
        job, _s, _st = make_job()
        assert job.try_begin(run_id="reserved-run") is True
        assert job.running_run_id() == "reserved-run", (
            "run 行落库前也必须能给出 run_id"
        )
        job._end()
        assert job.is_running is False
        # 预留值同样交给 Service（202 回传的 run_id 就是它）
        assert job.try_begin(run_id="reserved-run-2") is True
        assert job.run_manual("u1", run_id="reserved-run-2") == "reserved-run-2"
        assert job.history.run_ids == ["reserved-run-2"]

    def test_reservation_held_until_execution_returns(self):
        """预留从 ``try_begin`` 保持到 ``run_manual`` 返回，期间第二次触发必失败。

        确定性构造（无 sleep）：Service 的 run() 阻塞在测试持有的事件上，
        即"执行中"窗口由测试掌控，可稳定断言窗口内 ``try_begin`` 为 False
        且 ``running_run_id`` 仍为该预留值。
        """
        block = threading.Event()
        job = HistorySyncJob(AppConfig(), StubService(block=block), StubJobStatus())

        assert job.try_begin(run_id="reserved-run") is True
        runner = threading.Thread(
            target=job.run_manual, args=("u1",), kwargs={"run_id": "reserved-run"}
        )
        runner.start()
        try:
            assert job.history.entered.wait(timeout=5), "run_manual 未进入 Service"
            # 窗口内：互斥与预留值均保持，第二次触发必失败（API 层据此回 409）
            assert job.is_running is True
            assert job.running_run_id() == "reserved-run"
            assert job.try_begin(run_id="second-run") is False
            assert job.running_run_id() == "reserved-run", "失败方不得覆盖在跑的 run_id"
        finally:
            block.set()
            runner.join(timeout=5)

        # 执行返回后互斥释放，预留值清空，可重新预约
        # （此处直读 ``_running_run_id``：本文件其余用例同样白盒使用 ``_end()``；
        #   不经 ``running_run_id()`` 是为了避开它"查库找遗留 RUNNING 行"的回退，
        #   该回退另由 integration 用例覆盖，与本用例的进程内互斥无关）
        assert job.is_running is False
        assert job._running_run_id is None, "执行结束后预留值必须清空"
        assert job.try_begin(run_id="third-run") is True
        assert job.running_run_id() == "third-run"


# ---- startup catch-up ----


class TestStartupCatchup:
    async def test_startup_triggers_once_when_enabled(self, monkeypatch):
        job, service, _st = make_job(startup_catchup=True)
        # 只跑一轮调度循环即取消（避免测试挂住）
        monkeypatch.setattr(job_module, "_CHECK_INTERVAL_SECONDS", 0.05)
        await job.start()
        await asyncio.sleep(0.3)
        await job.stop()
        assert service.calls and service.calls[0][0] is TriggerType.STARTUP

    async def test_startup_skipped_when_disabled(self, monkeypatch):
        """startup_catchup=false 只该压掉 STARTUP，不该压掉到点调度。

        不能断言 ``service.calls == []``：本用例真实 sleep，若恰好在 20:30
        容差窗口内运行，调度循环会正常触发 SCHEDULED——那样断言会随
        wall-clock 偶发失败（与 startup_catchup 无关）。只断言 STARTUP 缺席。
        """
        job, service, _st = make_job(startup_catchup=False)
        monkeypatch.setattr(job_module, "_CHECK_INTERVAL_SECONDS", 0.05)
        await job.start()
        await asyncio.sleep(0.2)
        await job.stop()
        assert [t for t, _ in service.calls if t is TriggerType.STARTUP] == [], (
            "startup_catchup=false 不得触发 STARTUP"
        )


# ---- 优雅停机 ----


class TestGracefulShutdown:
    async def test_stop_sets_cancellation_event(self):
        """§49：停机时置 cancellation event，Service 在检查点收尾。"""
        block = threading.Event()
        job = HistorySyncJob(AppConfig(), StubService(block=block), StubJobStatus())
        task = asyncio.create_task(job.run_scheduled(TriggerType.SCHEDULED))
        await asyncio.sleep(0.15)

        await job.stop()
        event = job.history.events[0]
        assert event is not None and event.is_set(), "停机应置位 cancellation event"

        block.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def test_stop_without_running_is_safe(self):
        job, _s, _st = make_job()
        await job.stop()  # 未 start 过也应安全


# ---- JobStatusService 接入 ----


class TestJobStatusIntegration:
    async def test_records_started_and_success(self):
        job, _s, status = make_job()
        await job.run_scheduled(TriggerType.SCHEDULED)
        assert status.started == ["history_sync"]
        assert status.success == ["history_sync"]

    async def test_records_failure_when_service_raises(self):
        """Service 极端异常（如 run 记录创建失败）记失败，不退出调度循环。"""
        job, service, status = make_job()

        def boom(**_kwargs):
            raise RuntimeError("模拟 run 记录创建失败")

        service.run = boom
        assert await job.run_scheduled(TriggerType.SCHEDULED) == ""
        assert status.failure and status.failure[0][0] == "history_sync"
        assert job.is_running is False, "异常后必须释放 single-flight"
