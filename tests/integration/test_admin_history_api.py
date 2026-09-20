"""历史数据管理 API 集成测试（a-share-historical-data，tasks 7.3）。

覆盖 admin-data-management spec：
- 权限：未登录 401、普通用户 403、管理员 200；
- CSRF：POST 缺少 / 错误 X-CSRF-Token 被拒（既有中间件，不新增绕过）；
- 手动同步：无运行中任务 202 且后台启动；运行中 409 附当前 run_id；
  requested_by_user_id 服务端取自当前用户；
- summary / runs：结构与字段、时间格式、不触发事实表扫描（D21）；
- overall_status 优先级（§84）。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.config import AppConfig, DatabaseConfig
from app.models.history_sync import (
    DatasetName,
    DatasetStatus,
    HistoryDayStatus,
    HistorySyncRun,
    HistorySyncRunDataset,
    RunDatasetStatus,
    RunStatus,
    TriggerType,
)
from app.models.trading_calendar import TradingCalendarDay
from app.services.history.availability import AvailabilityPolicy
from app.services.market_session_service import now_beijing
from tests.integration.test_history_sync_service import (  # noqa: F401 - fixture 复用
    FakeHistoryProviders,
    frozen_now,
    make_service,
)

BEIJING = ZoneInfo("Asia/Shanghai")
OPEN_DAYS = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]


class FakeNameProvider:
    def get_name(self, market, asset_type, symbol):
        return None


class StubJob:
    """HistorySyncJob 替身：只验证 API 层的 202/409 与用户取值。

    ``busy=True`` 模拟"已有任务在运行"：API 拿不到预留值时必须回落到
    ``running_run_id()``（真实 Job 先查本进程登记值，再查库中 RUNNING 行）。

    互斥语义与真实 Job 对齐（``try_begin`` 预约、``run_manual`` 结束时才释放），
    否则测试会依赖"后台线程恰好还没跑完"这一时序假设。
    """

    def __init__(self, *, busy: bool = False):
        self.busy = busy
        self.run_calls: list[tuple[str, str | None]] = []
        self.begun = 0
        self._running_run_id: str | None = "running-run-1" if busy else None
        # 执行窗口同步原语（替代 sleep 轮询）：
        #   entered  — run_manual 已进入；release — 允许执行继续（默认放行）；
        #   finished — 执行结束且互斥已释放
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.finished = threading.Event()
        # 每次预留/释放的状态快照，供测试断言"执行期间从未提前释放"
        self.state: list[tuple[bool, str | None]] = [(self.busy, self._running_run_id)]

    def try_begin(self, *, run_id: str | None = None) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.begun += 1
        self._running_run_id = run_id
        self.state.append((self.busy, self._running_run_id))
        return True

    def running_run_id(self) -> str | None:
        return self._running_run_id if self.busy else None

    def is_holding_reservation(self, run_id: str) -> bool:
        """当前是否仍持有该 run_id 的预留（测试断言用，不参与 API 契约）。"""
        return self.busy and self._running_run_id == run_id

    def run_manual(self, requested_by_user_id: str, *, run_id: str | None = None) -> str:
        self.run_calls.append((requested_by_user_id, run_id))
        self.entered.set()
        # 模拟真实 Job._execute 的耗时：互斥从 try_begin 一直保持到本方法返回，
        # 与 API 是否已回 202 无关（202 只依赖预留，不等待执行）
        self.release.wait()
        self.busy = False
        self._running_run_id = None
        self.state.append((self.busy, self._running_run_id))
        self.finished.set()
        return run_id or "default-run"


@pytest.fixture()
def app_client_factory(session_factory, duckdb_url):
    """按角色构造带真实 lifespan 的 TestClient 工厂（history.enabled 打开）。"""

    def _make(*, login_as=None, role="user", job: StubJob | None = None):
        from app.main import create_app

        config = AppConfig(database=DatabaseConfig(url=duckdb_url))
        config.history.startup_catchup = False
        app = create_app(config)
        app.state.session_factory = session_factory
        app.state.name_provider = FakeNameProvider()
        client = TestClient(app)
        return app, client, login_as, role, job

    return _make


def _seed_calendar(session_factory):
    """写入严格日历（source='tushare'）与目标日期所需的最小日历。"""
    with session_factory() as session:
        day = date(2026, 9, 1)
        while day <= date(2026, 9, 30):
            session.add(
                TradingCalendarDay(
                    market="CN", trade_date=day, is_open=day in OPEN_DAYS,
                    exchange="SSE", source="tushare", fetched_at=now_beijing(),
                )
            )
            day += timedelta(days=1)
        session.commit()


def _sync_state(session_factory, dataset: DatasetName):
    """读 history_sync_state 行（回归用例断言 CLEAR 后的字段用）。"""
    from app.repositories.history_sync import HistorySyncStateRepository

    with session_factory() as session:
        return HistorySyncStateRepository(session).get(dataset)


def _seed_state(session_factory, dataset: DatasetName, **values):
    from app.models.history_sync import HistorySyncState

    defaults = dict(
        dataset=dataset.value,
        dataset_kind="DAILY_CONTIGUOUS",
        status=DatasetStatus.CAUGHT_UP.value,
        history_start_date=date(2010, 1, 1),
        updated_at=now_beijing(),
    )
    defaults.update(values)
    with session_factory() as session:
        session.add(HistorySyncState(**defaults))
        session.commit()


class TestAuthAndCsrf:
    def test_summary_requires_login(self, client_factory):
        with client_factory(FakeNameProvider()) as client:
            assert client.get("/api/admin/history-data/summary").status_code == 401

    def test_summary_forbidden_for_normal_user(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/summary").status_code == 403

    def test_summary_allowed_for_admin(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            assert client.get("/api/admin/history-data/summary").status_code == 200

    def test_runs_requires_admin(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/runs").status_code == 403

    def test_run_detail_requires_admin(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/runs/x").status_code == 403

    def test_sync_requires_admin(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.post("/api/admin/history-data/sync").status_code == 403

    def test_sync_rejects_missing_csrf(self, client_factory):
        """既有 CSRF 中间件覆盖本接口：无 X-CSRF-Token 的写请求被拒。"""
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            resp = client._client.post("/api/admin/history-data/sync")
            assert resp.status_code == 403
            assert "CSRF" in resp.json()["detail"]

    def test_sync_rejects_wrong_csrf(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            resp = client._client.post(
                "/api/admin/history-data/sync", headers={"X-CSRF-Token": "bogus"}
            )
            assert resp.status_code == 403


class TestSummary:
    def test_fresh_db_shape(self, client_factory, session_factory):
        """全新库：四个日级 + 四个主档键齐全，未开始的数据集为 UNINITIALIZED。"""
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        assert body["history_start_date"] == "2010-01-01"
        assert body["active_run"] is None
        assert [d["dataset"] for d in body["daily_datasets"]] == [
            "daily", "adj_factor", "daily_basic", "moneyflow",
        ]
        assert [d["dataset"] for d in body["master_datasets"]] == [
            "stock_basic", "trade_cal", "namechange", "stock_company",
        ]
        assert all(
            d["status"] == DatasetStatus.UNINITIALIZED.value for d in body["daily_datasets"]
        )
        assert body["overall_status"] == "UNINITIALIZED"
        # 主档不得伪造交易日水位字段（§54.3）
        for master in body["master_datasets"]:
            assert "latest_complete_trade_date" not in master
            assert "next_trade_date" not in master

    def test_daily_dataset_fields(self, client_factory, session_factory):
        """日级数据集字段与 §52.1 清单逐一对应。"""
        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName.DAILY,
            status=DatasetStatus.CAUGHT_UP.value,
            latest_complete_trade_date=date(2026, 9, 16),
            latest_expected_trade_date=date(2026, 9, 16),
            data_min_date=date(2026, 9, 14), data_max_date=date(2026, 9, 16),
            record_count=3, last_success_at=now_beijing(),
            current_trade_date=None, current_attempt=0,
        )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        daily = next(d for d in body["daily_datasets"] if d["dataset"] == "daily")
        assert set(daily) == {
            "dataset", "display_name", "status", "history_start_date",
            "data_min_date", "data_max_date", "latest_complete_trade_date",
            "latest_expected_trade_date", "next_trade_date", "lag_trade_days",
            "record_count", "current_trade_date", "current_attempt",
            "last_success_at", "last_error_code", "last_error",
        }
        assert daily["display_name"] == "日线行情"
        assert daily["record_count"] == 3
        assert daily["lag_trade_days"] == 0
        assert daily["last_success_at"].endswith("+08:00"), "时间须为北京时间带时区 ISO"

    def test_failed_dataset_shows_watermark_and_next_date(
        self, client_factory, session_factory, monkeypatch
    ):
        """spec"失败数据集可见"：水位不越过失败日，next_trade_date 指向失败日。"""
        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName.MONEYFLOW,
            status=DatasetStatus.FAILED.value,
            latest_complete_trade_date=date(2026, 9, 15),
            current_trade_date=date(2026, 9, 16), current_attempt=10,
            last_error_code="TUSHARE_TIMEOUT", last_error="请求超时",
        )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        moneyflow = next(d for d in body["daily_datasets"] if d["dataset"] == "moneyflow")
        assert moneyflow["status"] == DatasetStatus.FAILED.value
        assert moneyflow["latest_complete_trade_date"] == "2026-09-15"
        assert moneyflow["next_trade_date"] == "2026-09-16"
        assert moneyflow["current_trade_date"] == "2026-09-16"
        assert moneyflow["current_attempt"] == 10
        assert moneyflow["last_error_code"] == "TUSHARE_TIMEOUT"
        assert body["overall_status"] == "ERROR", "任一核心数据集失败即整体 ERROR（§84）"

    def test_failed_then_success_no_longer_shows_stale_error(
        self, make_service, session_factory, frozen_now, client_factory
    ):
        """回归：FAILED → 下一轮 SUCCESS 后 summary 不再展示旧错误。

        旧实现 ``finish_success`` 只写 status/last_success_at，失败留下的
        ``last_error_code``/``last_error`` 会被管理员页面当作"最后错误"继续
        展示，出现 "CAUGHT_UP + 陈旧错误码" 的自相矛盾状态（现场实测：
        stock_basic 追平后仍显示 TUSHARE_TIMEOUT）。本用例端到端跑两轮真实
        Service：首轮让 daily 某日失败 10 次 → FAILED，次轮恢复 → 追平，再断言
        summary 不再带旧错误。
        """
        frozen_now()
        _seed_calendar(session_factory)

        # 首轮：09-15 抛错 10 次 → daily FAILED（水位停 09-14），其余数据集成功
        providers = FakeHistoryProviders(
            fail_dates={"daily": {date(2026, 9, 15): 10}}
        )
        service, _p, _c, _slept = make_service(providers=providers)
        service.run(trigger=TriggerType.MANUAL)

        failed_state = _sync_state(session_factory, DatasetName.DAILY)
        assert failed_state.status == DatasetStatus.FAILED.value
        assert failed_state.last_error_code == "TUSHARE_TIMEOUT"
        assert failed_state.last_error is not None
        assert failed_state.last_error_at is not None
        assert failed_state.current_trade_date == date(2026, 9, 15)

        # 全程复用同一个 client：同一用户重复登录会产生多条 Session，
        # conftest 的 _current_csrf_token 取单值会 MultipleResultsFound。
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            # 失败态必须可见（spec"失败数据集可见"）——先确认前置语义成立
            body = client.get("/api/admin/history-data/summary").json()
            daily = next(d for d in body["daily_datasets"] if d["dataset"] == "daily")
            assert daily["last_error_code"] == "TUSHARE_TIMEOUT"
            assert body["overall_status"] == "ERROR"

            # 次轮：故障恢复，daily 推进到 09-16 追平
            recovered = FakeHistoryProviders()  # 无 fail_dates
            service2, _p2, _c2, _s2 = make_service(providers=recovered)
            service2.run(trigger=TriggerType.MANUAL)

            ok_state = _sync_state(session_factory, DatasetName.DAILY)
            assert ok_state.status == DatasetStatus.CAUGHT_UP.value, "次轮应追平"
            assert ok_state.latest_complete_trade_date == date(2026, 9, 16)
            assert ok_state.last_error_code is None, "追平后不得残留错误码"
            assert ok_state.last_error is None, "追平后不得残留错误文本"
            assert ok_state.last_error_at is None, "追平后不得残留错误时刻"
            assert ok_state.current_trade_date is None, "追平后不得残留失败日"
            assert ok_state.current_attempt == 0, "追平后尝试次数归零"
            assert ok_state.last_success_at is not None, "last_success_at 必须保留"

            # 核心断言：summary 不再展示旧错误，整体状态回到健康
            body = client.get("/api/admin/history-data/summary").json()
        daily = next(d for d in body["daily_datasets"] if d["dataset"] == "daily")
        assert daily["last_error_code"] is None, "summary 不得展示上一轮的陈旧错误码"
        assert daily["last_error"] is None, "summary 不得展示上一轮的陈旧错误文本"
        assert daily["current_trade_date"] is None
        assert daily["current_attempt"] == 0
        assert body["overall_status"] != "ERROR", "追平后整体状态不得停在 ERROR"

        # 历史不丢：失败那一轮的 run_dataset 仍保留错误码（§19 执行记录）
        with session_factory() as session:
            failed_rows = [
                row
                for row in session.scalars(
                    select(HistorySyncRunDataset).where(
                        HistorySyncRunDataset.dataset == DatasetName.DAILY.value,
                        HistorySyncRunDataset.status == RunDatasetStatus.FAILED.value,
                    )
                )
            ]
        assert failed_rows, "失败的历史执行记录必须保留"
        assert any(
            row.last_error_code == "TUSHARE_TIMEOUT" for row in failed_rows
        ), "历史错误码由 run_dataset 承载，清除 state 不得丢失历史"

    def test_master_dataset_fields(self, client_factory, session_factory):
        from app.models.history_sync import HistorySyncState

        with session_factory() as session:
            session.add(
                HistorySyncState(
                    dataset=DatasetName.STOCK_BASIC.value, dataset_kind="MASTER",
                    status=DatasetStatus.CAUGHT_UP.value, record_count=5300,
                    last_success_at=now_beijing(), updated_at=now_beijing(),
                )
            )
            session.add(
                HistorySyncState(
                    dataset=DatasetName.NAMECHANGE.value, dataset_kind="MASTER",
                    status=DatasetStatus.CAUGHT_UP.value, record_count=120,
                    master_cursor="600519", bootstrap_complete=False,
                    updated_at=now_beijing(),
                )
            )
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        basic = next(m for m in body["master_datasets"] if m["dataset"] == "stock_basic")
        assert basic["record_count"] == 5300
        assert basic["display_name"] == "股票基础信息"
        nc = next(m for m in body["master_datasets"] if m["dataset"] == "namechange")
        assert nc["master_cursor"] == "600519"
        assert nc["bootstrap_complete"] is False

    def test_overall_status_running_when_active_run(
        self, client_factory, session_factory
    ):
        with session_factory() as session:
            session.add(
                HistorySyncRun(
                    run_id="r1", trigger_type=TriggerType.MANUAL.value,
                    status=RunStatus.RUNNING.value, started_at=now_beijing(),
                    created_at=now_beijing(),
                )
            )
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        assert body["overall_status"] == "RUNNING"
        assert body["active_run"]["run_id"] == "r1"
        assert body["active_run"]["trigger_type"] == "MANUAL"
        assert body["active_run"]["started_at"].endswith("+08:00")

    def test_overall_status_healthy_when_all_caught_up(
        self, client_factory, session_factory
    ):
        _seed_calendar(session_factory)
        for dataset in (
            DatasetName.DAILY, DatasetName.ADJ_FACTOR,
            DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW,
        ):
            _seed_state(
                session_factory, dataset,
                status=DatasetStatus.CAUGHT_UP.value,
                latest_complete_trade_date=date(2026, 9, 16),
            )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()
        assert body["overall_status"] == "HEALTHY"

    def test_core_master_failure_escalates(self, client_factory, session_factory):
        """§84：trade_cal/stock_basic 失败升级整体异常级别。"""
        _seed_calendar(session_factory)
        for dataset in (
            DatasetName.DAILY, DatasetName.ADJ_FACTOR,
            DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW,
        ):
            _seed_state(
                session_factory, dataset,
                status=DatasetStatus.CAUGHT_UP.value,
                latest_complete_trade_date=date(2026, 9, 16),
            )
        _seed_state(
            session_factory, DatasetName.TRADE_CAL,
            dataset_kind="MASTER", status=DatasetStatus.FAILED.value,
            last_error_code="CALENDAR_UNAVAILABLE", last_error="严格日历不可用",
        )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()
        assert body["overall_status"] == "ERROR"

    def test_non_core_master_failure_does_not_escalate(
        self, client_factory, session_factory
    ):
        """§84：stock_company/namechange 短暂失败不必然升级整体 ERROR。"""
        _seed_calendar(session_factory)
        for dataset in (
            DatasetName.DAILY, DatasetName.ADJ_FACTOR,
            DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW,
        ):
            _seed_state(
                session_factory, dataset,
                status=DatasetStatus.CAUGHT_UP.value,
                latest_complete_trade_date=date(2026, 9, 16),
            )
        _seed_state(
            session_factory, DatasetName.STOCK_COMPANY,
            dataset_kind="MASTER", status=DatasetStatus.FAILED.value,
        )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()
        assert body["overall_status"] == "HEALTHY"

    def test_core_master_failure_escalates_before_uninitialized(
        self, client_factory, session_factory
    ):
        """回归：日级数据集全未初始化但核心主档 FAILED 时，须报 ERROR 而非 UNINITIALIZED。

        实际场景：首次部署时严格日历就拉取失败（无 Token/网络不通），四个日级
        数据集连 state 行都没有。此时显示"尚未开始"会把管理员引向点同步按钮
        （必被硬前置挡住），而正确动作是修 Token/网络。
        """
        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName.TRADE_CAL,
            dataset_kind="MASTER", status=DatasetStatus.FAILED.value,
            last_error_code="CALENDAR_UNAVAILABLE", last_error="严格日历不可用",
        )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()
        assert all(
            d["status"] == "UNINITIALIZED" for d in body["daily_datasets"]
        ), "本用例前提：日级数据集全未初始化"
        assert body["overall_status"] == "ERROR", (
            "核心主档失败必须升级为 ERROR，不得被 UNINITIALIZED 吞掉"
        )

    def test_stale_calendar_does_not_report_healthy(
        self, client_factory, session_factory
    ):
        """回归：缓存日历过期时不得报 HEALTHY（lag 恒为 0 会掩盖真实落后）。"""
        # 日历只覆盖到很久以前：同步长期失败/停机数月的真实形态
        with session_factory() as session:
            for day in (date(2026, 1, 5), date(2026, 1, 6)):
                session.add(
                    TradingCalendarDay(
                        market="CN", trade_date=day, is_open=True,
                        exchange="SSE", source="tushare", fetched_at=now_beijing(),
                    )
                )
            session.commit()
        for dataset in (
            DatasetName.DAILY, DatasetName.ADJ_FACTOR,
            DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW,
        ):
            _seed_state(
                session_factory, dataset,
                status=DatasetStatus.CAUGHT_UP.value,
                latest_complete_trade_date=date(2026, 1, 6),
                latest_expected_trade_date=date(2026, 1, 6),
            )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        assert body["latest_market_trade_date"] == "2026-01-06"
        assert all(d["lag_trade_days"] == 0 for d in body["daily_datasets"])
        assert body["overall_status"] == "LAGGING", (
            "日历已过期时 lag=0 不可信，不得据此报 HEALTHY"
        )

    def test_in_progress_state_without_lag_is_not_lagging(
        self, client_factory, session_factory
    ):
        """回归：无落后但某数据集处于 SYNCING/RETRYING 时不得报 LAGGING。

        否则页面自相矛盾：同一响应里四个 lag_trade_days 全为 0 却显示"落后"。
        """
        _seed_calendar(session_factory)
        for dataset in (
            DatasetName.DAILY, DatasetName.ADJ_FACTOR, DatasetName.MONEYFLOW,
        ):
            _seed_state(
                session_factory, dataset,
                status=DatasetStatus.CAUGHT_UP.value,
                latest_complete_trade_date=date(2026, 9, 16),
                latest_expected_trade_date=date(2026, 9, 16),
            )
        _seed_state(
            session_factory, DatasetName.DAILY_BASIC,
            status=DatasetStatus.SYNCING.value,
            latest_complete_trade_date=date(2026, 9, 16),
            latest_expected_trade_date=date(2026, 9, 16),
            current_trade_date=date(2026, 9, 17), current_attempt=1,
        )
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        assert all(d["lag_trade_days"] == 0 for d in body["daily_datasets"])
        assert body["overall_status"] != "LAGGING", "无落后不该报 LAGGING"

    def test_summary_does_not_scan_fact_tables(self, client_factory, session_factory):
        """D21/§52.1：summary 只读同步小表，不得触碰事实大表。"""
        from app.models.history_fact import HISTORY_FACT_TABLES

        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName.DAILY,
            status=DatasetStatus.CAUGHT_UP.value,
            latest_complete_trade_date=date(2026, 9, 16),
        )

        statements: list[str] = []
        engine = session_factory.kw["bind"]

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            with client_factory(
                FakeNameProvider(), login_as="admin", role="admin"
            ) as client:
                assert client.get("/api/admin/history-data/summary").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", record)

        fact_names = [table.name for table in HISTORY_FACT_TABLES.values()]
        offenders = [
            stmt for stmt in statements
            if any(f"FROM {name}" in stmt or f"from {name}" in stmt for name in fact_names)
        ]
        assert not offenders, f"summary 扫描了事实表: {offenders}"


class TestManualSync:
    def test_start_returns_202_with_run_id(self, client_factory, session_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            job = StubJob()
            client.app.state.history_sync_job = job
            resp = client.post("/api/admin/history-data/sync")

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "RUNNING"
        assert body["run_id"], "202 必须回传预留的 run_id"

    def test_conflict_returns_409_with_running_run_id(self, client_factory, session_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            client.app.state.history_sync_job = StubJob(busy=True)
            resp = client.post("/api/admin/history-data/sync")

        assert resp.status_code == 409
        body = resp.json()
        assert body["run_id"] == "running-run-1"
        assert "正在运行" in body["message"]

    def test_conflict_does_not_start_second_run(self, client_factory, session_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            job = StubJob(busy=True)
            client.app.state.history_sync_job = job
            client.post("/api/admin/history-data/sync")
        assert job.run_calls == [], "409 不得启动第二个 run"

    def test_requested_by_taken_from_authenticated_user(
        self, client_factory, session_factory, user_factory
    ):
        """requested_by_user_id 服务端取值：客户端传值必须被忽略。"""
        from app.auth.session import CurrentUser  # noqa: F401  (语义注释：取自已认证用户)

        admin = user_factory("admin", role="admin")
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            job = StubJob()
            client.app.state.history_sync_job = job
            resp = client.post(
                "/api/admin/history-data/sync", json={"requested_by_user_id": "attacker"}
            )
            assert resp.status_code == 202
            # 后台任务在线程池中执行，用事件等待其登记（不 sleep 轮询）
            assert job.entered.wait(timeout=10), "后台任务未启动"

        assert job.run_calls, "后台任务未启动"
        user_id, reserved_run_id = job.run_calls[0]
        assert user_id == admin["user_id"], "必须使用认证用户 id，而非客户端传值"
        assert reserved_run_id == resp.json()["run_id"]

    def test_conflict_reports_reserved_run_id_immediately(
        self, client_factory, session_factory
    ):
        """回归：run 行尚未落库时，409 也必须给出 run_id（§52.2 契约）。

        真实竞态：请求 1 已取得 single-flight 但后台线程还在等写锁、run 行
        尚未 create；此时请求 2 到达必须能拿到请求 1 的 run_id，而不是 null。

        确定性构造（不使用 sleep / 超时）：StubJob.run_manual 未收到 ``release``
        前一直不返回，即互斥保持——这正是"已预留但 run 行未落库"的窗口。
        第二个请求在该窗口内发出，因此不存在"后台线程恰好已复位"的时序假设。
        """
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            job = StubJob()
            job.release.clear()  # 首次执行不返回：窗口由测试掌控
            client.app.state.history_sync_job = job
            first = client.post("/api/admin/history-data/sync")

            assert first.status_code == 202
            reserved_run_id = first.json()["run_id"]
            # 后台线程已进入执行且尚未返回（事件同步，非轮询）
            assert job.entered.wait(timeout=10), "首个后台任务未启动"

            # 窗口内：库里没有 RUNNING 行（stub 不落库），只有进程内预留值
            assert job.state == [(False, None), (True, reserved_run_id)], (
                f"执行期间互斥必须保持预留状态，实际 {job.state}"
            )
            assert job.is_holding_reservation(reserved_run_id), (
                "202 返回后互斥不得被提前释放"
            )

            second = client.post("/api/admin/history-data/sync")
            assert second.status_code == 409
            assert second.json()["run_id"] == reserved_run_id, (
                "409 必须回传正在运行的 run_id，不得为 null"
            )
            assert job.begun == 1, "409 不得启动第二个 run"

            # 放行首个执行：结束后互斥才释放
            job.release.set()
            assert job.finished.wait(timeout=10), "首个后台任务未结束"
            assert job.state[-1] == (False, None), "执行结束后必须释放预留"

            third = client.post("/api/admin/history-data/sync")
            assert third.status_code == 202, "释放后应可再次启动"
            assert third.json()["run_id"] != reserved_run_id

    def test_sync_unavailable_returns_503(self, client_factory, session_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            client.app.state.history_sync_job = None
            resp = client.post("/api/admin/history-data/sync")
        assert resp.status_code == 503

    def test_real_job_reserves_atomically_before_202(
        self, client_factory, session_factory
    ):
        """用真实 HistorySyncJob 验证：202 返回时预留已生效，且保持到执行结束。

        与 StubJob 用例（验证 API 层分支）互补：此处互斥是真实实现（真锁 +
        真 Job），Service 用惰性桩把"执行中"窗口交给测试掌控——不 sleep、
        不放宽断言。断言 202 与预留之间不存在可见窗口：收到 202 后立刻发起的
        并发请求必得 409，且 run_id 与 202 回传值一致。
        """
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            job = client.app.state.history_sync_job
            assert job is not None, "history.enabled 应装配真实 Job"

            entered = threading.Event()
            release = threading.Event()
            released = threading.Event()

            def _blocking_run(**kwargs):
                entered.set()
                release.wait(timeout=15)
                return kwargs.get("run_id") or "generated"

            # 只替换 Service 的 run（Job 的锁与预留逻辑保持真实）；
            # 在 _end() 真正执行完的瞬间置事件，使"释放"可被事件等待而非轮询。
            original_end = job._end

            def _end_signalling():
                original_end()
                released.set()

            job.history.run = _blocking_run
            job._end = _end_signalling

            first = client.post("/api/admin/history-data/sync")
            assert first.status_code == 202
            reserved = first.json()["run_id"]
            assert entered.wait(timeout=10), "后台任务未进入执行"

            # 202 之后、执行结束之前：预留必须仍然有效
            assert job._running_run_id == reserved
            assert job.is_running is True
            assert job.try_begin(run_id="intruder") is False, "预留期内不得再次取得"

            second = client.post("/api/admin/history-data/sync")
            assert second.status_code == 409
            assert second.json()["run_id"] == reserved, (
                "409 必须回传 202 预留的同一个 run_id"
            )

            release.set()
            assert released.wait(timeout=10), "执行结束后必须释放互斥"

            # release 已置位，第三次执行体立即返回，不会留下悬挂线程
            third = client.post("/api/admin/history-data/sync")
            assert third.status_code == 202, "释放后应可再次启动"


class TestRuns:
    def test_runs_list_and_detail(self, client_factory, session_factory):
        started = now_beijing()
        with session_factory() as session:
            session.add(
                HistorySyncRun(
                    run_id="run-1", trigger_type=TriggerType.SCHEDULED.value,
                    requested_by_user_id=None, status=RunStatus.PARTIAL.value,
                    started_at=started, finished_at=started + timedelta(seconds=42),
                    error_summary="moneyflow 失败", created_at=started,
                )
            )
            session.add(
                HistorySyncRunDataset(
                    run_id="run-1", dataset=DatasetName.DAILY.value,
                    status=RunDatasetStatus.SUCCESS.value,
                    start_watermark=date(2026, 9, 14), target_trade_date=date(2026, 9, 16),
                    end_watermark=date(2026, 9, 16), dates_completed=2,
                    rows_written=10000, request_count=2, retry_count=1,
                    started_at=started, finished_at=started + timedelta(seconds=20),
                )
            )
            session.add(
                HistorySyncRunDataset(
                    run_id="run-1", dataset=DatasetName.MONEYFLOW.value,
                    status=RunDatasetStatus.FAILED.value,
                    start_watermark=date(2026, 9, 14), target_trade_date=date(2026, 9, 16),
                    end_watermark=date(2026, 9, 14), dates_completed=0,
                    failed_trade_date=date(2026, 9, 15),
                    last_error_code="TUSHARE_TIMEOUT", last_error="请求超时",
                    started_at=started, finished_at=started + timedelta(seconds=42),
                )
            )
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            listed = client.get("/api/admin/history-data/runs?limit=20")
            detail = client.get("/api/admin/history-data/runs/run-1")
            missing = client.get("/api/admin/history-data/runs/nope")

        assert listed.status_code == 200
        items = listed.json()["items"]
        assert [i["run_id"] for i in items] == ["run-1"]
        assert items[0]["trigger_type"] == "SCHEDULED"
        assert items[0]["status"] == "PARTIAL"
        assert items[0]["duration_ms"] == 42000
        assert items[0]["started_at"].endswith("+08:00")

        assert detail.status_code == 200
        body = detail.json()
        assert body["run"]["run_id"] == "run-1"
        by_dataset = {d["dataset"]: d for d in body["datasets"]}
        assert by_dataset["daily"]["dates_completed"] == 2
        assert by_dataset["daily"]["rows_written"] == 10000
        assert by_dataset["daily"]["retry_count"] == 1
        assert by_dataset["daily"]["start_watermark"] == "2026-09-14"
        assert by_dataset["daily"]["end_watermark"] == "2026-09-16"
        assert by_dataset["daily"]["display_name"] == "日线行情"
        assert by_dataset["moneyflow"]["failed_trade_date"] == "2026-09-15"
        assert by_dataset["moneyflow"]["last_error_code"] == "TUSHARE_TIMEOUT"

        assert missing.status_code == 404

    def test_runs_limit_validation(self, client_factory, session_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            assert client.get("/api/admin/history-data/runs?limit=0").status_code == 422
            assert client.get("/api/admin/history-data/runs?limit=500").status_code == 422


class TestNoTokenLeak:
    def test_summary_and_runs_never_contain_token(self, client_factory, session_factory):
        """§51.3/§62：API 响应不得出现 Token 明文（沿 config 无 Token 路径）。"""
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            summary = client.get("/api/admin/history-data/summary")
            runs = client.get("/api/admin/history-data/runs")
        for resp in (summary, runs):
            assert "token" not in resp.text.lower().replace("last_error_code", "")
