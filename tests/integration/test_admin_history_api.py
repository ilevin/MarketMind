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

    def test_stocks_requires_admin(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/stocks?dataset=daily").status_code == 403

    def test_stocks_requires_login(self, client_factory):
        with client_factory(FakeNameProvider()) as client:
            assert client.get("/api/admin/history-data/stocks?dataset=daily").status_code == 401

    def test_tasks_requires_admin(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/tasks/1").status_code == 403

    def test_tasks_requires_login(self, client_factory):
        with client_factory(FakeNameProvider()) as client:
            assert client.get("/api/admin/history-data/tasks/1").status_code == 401

    def test_datasets_requires_admin(self, client_factory):
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/datasets").status_code == 403


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
        # v0.4.0 个股口径：旧字段保留 + 6 个新字段
        assert set(daily) == {
            "dataset", "display_name", "status", "history_start_date",
            "data_min_date", "data_max_date", "latest_complete_trade_date",
            "latest_expected_trade_date", "next_trade_date", "lag_trade_days",
            "record_count", "current_trade_date", "current_attempt",
            "last_success_at", "last_error_code", "last_error",
            "stock_count", "up_to_date_count", "lagging_count",
            "today_success_count", "today_failed_count", "completion_rate",
        }
        assert daily["display_name"] == "日线行情"
        assert daily["record_count"] == 3
        assert daily["lag_trade_days"] == 0, "旧 lag 字段保留（冻结兼容）"
        assert daily["last_success_at"].endswith("+08:00"), "时间须为北京时间带时区 ISO"
        # 无 stock_sync_state 行时，个股统计全为 0
        assert daily["stock_count"] == 0
        assert daily["completion_rate"] == 0.0

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

    def test_individual_stock_failure_is_lagging_not_error(
        self, client_factory, session_factory
    ):
        """v0.4.0 个股口径：个股失败 → overall_status = LAGGING，非 ERROR。

        设计 D12：单股失败不升级 ERROR，系统级（数据集 status=FAILED）才 ERROR。
        直接构造 stock_sync_state 行模拟个股失败，验证 summary 口径。
        """
        from app.models.history_sync import StockSyncState
        from app.models.instrument import Instrument
        from app.models.history_market import CnStockBasic

        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName.DAILY,
            status=DatasetStatus.CAUGHT_UP.value,
            latest_complete_trade_date=date(2026, 9, 16),
            latest_expected_trade_date=date(2026, 9, 16),
        )
        # 构造 2 只股票：一只追平、一只失败
        # Instrument 先建（FK 约束），再建 cn_stock_basic + stock_sync_state
        with session_factory() as session:
            session.add_all([
                Instrument(
                    instrument_id="CN:STOCK:000001", symbol="000001",
                    name="平安银行", market="CN", asset_type="STOCK",
                    currency="CNY", exchange="SZSE", is_active=True,
                ),
                Instrument(
                    instrument_id="CN:STOCK:000002", symbol="000002",
                    name="万科A", market="CN", asset_type="STOCK",
                    currency="CNY", exchange="SZSE", is_active=True,
                ),
            ])
            session.flush()
            session.add_all([
                CnStockBasic(
                    instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                    symbol="000001", name="平安银行",
                    list_date=date(2010, 1, 4),
                    source="tushare", fetched_at=now_beijing(),
                    source_last_seen_at=now_beijing(),
                ),
                CnStockBasic(
                    instrument_id="CN:STOCK:000002", ts_code="000002.SZ",
                    symbol="000002", name="万科A",
                    list_date=date(2010, 1, 4),
                    source="tushare", fetched_at=now_beijing(),
                    source_last_seen_at=now_beijing(),
                ),
                StockSyncState(
                    dataset=DatasetName.DAILY.value,
                    instrument_id="CN:STOCK:000001",
                    ts_code="000001.SZ",
                    watermark_date=date(2026, 9, 16),  # 已追平
                    last_status="success",
                    last_success_at=now_beijing(),
                    last_attempt_at=now_beijing(),
                    created_at=now_beijing(),
                    updated_at=now_beijing(),
                ),
                StockSyncState(
                    dataset=DatasetName.DAILY.value,
                    instrument_id="CN:STOCK:000002",
                    ts_code="000002.SZ",
                    watermark_date=date(2026, 9, 14),  # 落后
                    last_status="failed",
                    last_error_code="TUSHARE_TIMEOUT",
                    last_error="请求超时",
                    last_attempt_at=now_beijing(),
                    created_at=now_beijing(),
                    updated_at=now_beijing(),
                ),
            ])
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        daily = next(d for d in body["daily_datasets"] if d["dataset"] == "daily")
        assert daily["stock_count"] == 2
        assert daily["up_to_date_count"] == 1
        assert daily["lagging_count"] == 1
        # 个股失败不升级 ERROR：数据集 status 仍为 CAUGHT_UP，整体 LAGGING
        assert daily["status"] == DatasetStatus.CAUGHT_UP.value
        assert body["overall_status"] == "LAGGING", (
            "个股失败 → 整体 LAGGING，不得升级 ERROR"
        )
        assert body["overall_status"] != "ERROR"

    def test_all_caught_up_is_healthy(self, client_factory, session_factory):
        """全部股票追平 → overall_status = HEALTHY。"""
        from app.models.history_sync import StockSyncState
        from app.models.instrument import Instrument
        from app.models.history_market import CnStockBasic

        _seed_calendar(session_factory)
        for ds in (
            DatasetName.DAILY, DatasetName.ADJ_FACTOR,
            DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW,
        ):
            _seed_state(
                session_factory, ds,
                status=DatasetStatus.CAUGHT_UP.value,
                latest_complete_trade_date=date(2026, 9, 16),
                latest_expected_trade_date=date(2026, 9, 16),
            )
        with session_factory() as session:
            session.add(Instrument(
                instrument_id="CN:STOCK:000001", symbol="000001",
                name="平安银行", market="CN", asset_type="STOCK",
                currency="CNY", exchange="SZSE", is_active=True,
            ))
            session.flush()
            session.add(CnStockBasic(
                instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                symbol="000001", name="平安银行",
                list_date=date(2010, 1, 4),
                source="tushare", fetched_at=now_beijing(),
                source_last_seen_at=now_beijing(),
            ))
            for ds in (
                DatasetName.DAILY, DatasetName.ADJ_FACTOR,
                DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW,
            ):
                session.add(StockSyncState(
                    dataset=ds.value,
                    instrument_id="CN:STOCK:000001",
                    ts_code="000001.SZ",
                    watermark_date=date(2026, 9, 16),  # 已追平
                    last_status="success",
                    last_success_at=now_beijing(),
                    last_attempt_at=now_beijing(),
                    created_at=now_beijing(),
                    updated_at=now_beijing(),
                ))
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        assert body["overall_status"] == "HEALTHY"
        for d in body["daily_datasets"]:
            assert d["stock_count"] == 1
            assert d["up_to_date_count"] == 1
            assert d["lagging_count"] == 0
            assert d["completion_rate"] == 1.0

    def test_system_level_failure_is_error(self, client_factory, session_factory):
        """数据集级 FAILED（系统级失败） → overall_status = ERROR。"""
        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName.DAILY,
            status=DatasetStatus.FAILED.value,
            latest_complete_trade_date=date(2026, 9, 14),
            current_trade_date=date(2026, 9, 15),
            last_error_code="DATABASE_ERROR",
            last_error="数据库写入失败",
        )
        for ds in (DatasetName.ADJ_FACTOR, DatasetName.DAILY_BASIC, DatasetName.MONEYFLOW):
            _seed_state(
                session_factory, ds,
                status=DatasetStatus.CAUGHT_UP.value,
                latest_complete_trade_date=date(2026, 9, 16),
            )

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        assert body["overall_status"] == "ERROR", "数据集级 FAILED 必须升级 ERROR"

    def test_active_run_is_running(self, client_factory, session_factory):
        """有 active run → overall_status = RUNNING（优先级最高）。"""
        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName.DAILY,
            status=DatasetStatus.FAILED.value,  # 即使有 FAILED
            last_error_code="X", last_error="x",
        )
        with session_factory() as session:
            session.add(HistorySyncRun(
                run_id="r-running", trigger_type=TriggerType.MANUAL.value,
                status=RunStatus.RUNNING.value, started_at=now_beijing(),
                created_at=now_beijing(),
            ))
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/summary").json()

        assert body["overall_status"] == "RUNNING", "active run 优先于 ERROR"
        assert body["active_run"]["run_id"] == "r-running"

    def test_history_run_dataset_preserves_error(self, client_factory, session_factory):
        """历史不丢：失败 run_dataset 仍保留错误码（不因 state 清除而丢失）。"""
        _seed_calendar(session_factory)
        started = now_beijing()
        with session_factory() as session:
            session.add(HistorySyncRun(
                run_id="run-failed", trigger_type=TriggerType.MANUAL.value,
                status=RunStatus.FAILED.value,
                started_at=started, finished_at=started + timedelta(seconds=30),
                error_summary="daily 失败",
                created_at=started,
            ))
            session.add(HistorySyncRunDataset(
                run_id="run-failed", dataset=DatasetName.DAILY.value,
                status=RunDatasetStatus.FAILED.value,
                last_error_code="TUSHARE_TIMEOUT",
                last_error="请求超时",
                processed_count=0, task_success_count=0,
                task_failed_count=1, skipped_count=0,
                started_at=started, finished_at=started + timedelta(seconds=30),
            ))
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            detail = client.get("/api/admin/history-data/runs/run-failed").json()

        daily = next(d for d in detail["datasets"] if d["dataset"] == "daily")
        assert daily["last_error_code"] == "TUSHARE_TIMEOUT"
        assert daily["last_error"] == "请求超时"
        # 个股统计列存在
        assert "processed_count" in daily
        assert daily["task_failed_count"] == 1

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
        # requested_by_user_id 为字符串（DB 列 String(64)），修复 int→str 类型瑕疵
        assert user_id == str(admin["user_id"]), "必须使用认证用户 id，而非客户端传值"
        assert isinstance(user_id, str), "requested_by_user_id 须为字符串"
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

    def test_run_detail_has_stock_counts_and_old_watermark_compat(
        self, client_factory, session_factory
    ):
        """run 详情同时包含个股统计列（新）与旧水位列（兼容冻结）。"""
        started = now_beijing()
        with session_factory() as session:
            session.add(HistorySyncRun(
                run_id="run-stock", trigger_type=TriggerType.MANUAL.value,
                status=RunStatus.SUCCESS.value,
                started_at=started, finished_at=started + timedelta(seconds=10),
                created_at=started,
            ))
            session.add(HistorySyncRunDataset(
                run_id="run-stock", dataset=DatasetName.DAILY.value,
                status=RunDatasetStatus.SUCCESS.value,
                # 旧水位列（历史数据保留，但新 run 可以为 NULL）
                start_watermark=None, target_trade_date=None, end_watermark=None,
                dates_completed=0,
                # 累计列
                rows_written=1234, request_count=5, retry_count=0,
                # 个股统计列（新）
                processed_count=100, task_success_count=98,
                task_failed_count=2, skipped_count=50,
                started_at=started, finished_at=started + timedelta(seconds=10),
            ))
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/runs/run-stock").json()

        daily = next(d for d in body["datasets"] if d["dataset"] == "daily")
        # 新列存在
        assert daily["processed_count"] == 100
        assert daily["task_success_count"] == 98
        assert daily["task_failed_count"] == 2
        assert daily["skipped_count"] == 50
        # 旧兼容列仍存在（值为 NULL 或 0）
        assert "start_watermark" in daily
        assert "end_watermark" in daily
        assert "dates_completed" in daily
        # progress 字段存在（非运行中为 None）
        assert "progress" in body
        assert body["progress"] is None

    def test_run_detail_running_has_progress_field(self, client_factory, session_factory):
        """RUNNING 状态的 run，detail 响应带 progress 字段（可能为 None 或有值）。"""
        started = now_beijing()
        with session_factory() as session:
            session.add(HistorySyncRun(
                run_id="run-progress", trigger_type=TriggerType.MANUAL.value,
                status=RunStatus.RUNNING.value,
                started_at=started, created_at=started,
            ))
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/runs/run-progress").json()

        assert "progress" in body
        # progress 可以是 None（没有 history_sync_service 在 app.state）或 dict
        if body["progress"] is not None:
            assert "current_dataset" in body["progress"]
            assert "processed" in body["progress"]


class TestStocks:
    """GET /api/admin/history-data/stocks 个股列表测试（tasks 8.2）。"""

    def _seed_stocks(self, session_factory, *, dataset="daily", n=3):
        """构造测试股票数据（instrument + cn_stock_basic + 可选 stock_sync_state）。"""
        from app.models.history_sync import StockSyncState as SSS
        from app.models.instrument import Instrument
        from app.models.history_market import CnStockBasic

        _seed_calendar(session_factory)
        _seed_state(
            session_factory, DatasetName(dataset),
            status=DatasetStatus.CAUGHT_UP.value,
            latest_complete_trade_date=date(2026, 9, 16),
            latest_expected_trade_date=date(2026, 9, 16),
        )
        with session_factory() as session:
            for i in range(1, n + 1):
                symbol = f"{i:06d}"
                inst_id = f"CN:STOCK:{symbol}"
                ts_code = f"{symbol}.SZ"
                session.add(Instrument(
                    instrument_id=inst_id, symbol=symbol,
                    name=f"股票{i}", market="CN", asset_type="STOCK",
                    currency="CNY", exchange="SZSE", is_active=True,
                ))
            session.flush()
            for i in range(1, n + 1):
                symbol = f"{i:06d}"
                inst_id = f"CN:STOCK:{symbol}"
                ts_code = f"{symbol}.SZ"
                session.add(CnStockBasic(
                    instrument_id=inst_id, ts_code=ts_code,
                    symbol=symbol, name=f"股票{i}",
                    list_date=date(2010, 1, 4),
                    source="tushare", fetched_at=now_beijing(),
                    source_last_seen_at=now_beijing(),
                ))
            session.commit()

    def test_stocks_requires_dataset(self, client_factory, session_factory):
        """缺 dataset 参数 → 422。"""
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            assert client.get("/api/admin/history-data/stocks").status_code == 422

    def test_stocks_rejects_non_daily_dataset(self, client_factory, session_factory):
        """非日级数据集 → 422。"""
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            resp = client.get("/api/admin/history-data/stocks?dataset=stock_basic")
            assert resp.status_code == 422
            assert "日级数据集" in resp.json()["detail"]

    def test_stocks_rejects_invalid_status(self, client_factory, session_factory):
        """非法 status → 422。"""
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            resp = client.get("/api/admin/history-data/stocks?dataset=daily&status=bogus")
            assert resp.status_code == 422

    def test_stocks_requires_admin(self, client_factory):
        """普通用户 → 403。"""
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/stocks?dataset=daily").status_code == 403

    def test_stocks_requires_login(self, client_factory):
        """未登录 → 401。"""
        with client_factory(FakeNameProvider()) as client:
            assert client.get("/api/admin/history-data/stocks?dataset=daily").status_code == 401

    def test_stocks_list_response_structure(self, client_factory, session_factory):
        """响应结构：items + stats + pagination。"""
        self._seed_stocks(session_factory, n=3)
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/stocks?dataset=daily").json()

        assert "items" in body
        assert "stats" in body
        assert "pagination" in body
        assert isinstance(body["items"], list)
        assert len(body["items"]) == 3
        # items 字段
        item = body["items"][0]
        for field in (
            "ts_code", "name", "list_date", "delist_date",
            "watermark_date", "last_status", "last_error_code",
            "last_error", "last_success_at", "last_attempt_at",
        ):
            assert field in item, f"缺少字段 {field}"
        # stats 字段
        for field in (
            "stock_count", "up_to_date_count", "lagging_count",
            "today_success_count", "today_failed_count", "completion_rate",
        ):
            assert field in body["stats"], f"缺少 stats.{field}"
        # pagination 字段
        assert body["pagination"]["page"] == 1
        assert body["pagination"]["page_size"] == 100
        assert body["pagination"]["total"] == 3
        assert body["pagination"]["total_pages"] == 1

    def test_stocks_default_order_failed_first_then_watermark_asc(
        self, client_factory, session_factory
    ):
        """默认排序：失败优先 → 水位升序（NULL 最前） → ts_code 升序。"""
        from app.models.history_sync import StockSyncState as SSS

        self._seed_stocks(session_factory, n=3)
        # 股票1: 失败（排最前）、股票2: 无水位（NULL，次之）、股票3: 水位 09-16（最后）
        with session_factory() as session:
            session.add(SSS(
                dataset="daily", instrument_id="CN:STOCK:000001",
                ts_code="000001.SZ", watermark_date=date(2026, 9, 14),
                last_status="failed", last_error_code="E1", last_error="err",
                last_attempt_at=now_beijing(),
                created_at=now_beijing(), updated_at=now_beijing(),
            ))
            session.add(SSS(
                dataset="daily", instrument_id="CN:STOCK:000003",
                ts_code="000003.SZ", watermark_date=date(2026, 9, 16),
                last_status="success",
                last_success_at=now_beijing(), last_attempt_at=now_beijing(),
                created_at=now_beijing(), updated_at=now_beijing(),
            ))
            # 股票2 无 stock_sync_state 行（NULL 水位）
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/stocks?dataset=daily").json()

        codes = [item["ts_code"] for item in body["items"]]
        # 失败排最前 → NULL 水位 → 成功且有水位
        assert codes[0] == "000001.SZ", "失败股票应排最前"
        assert codes[1] == "000002.SZ", "无水位（NULL）应次之"
        assert codes[2] == "000003.SZ", "已追平排最后"

    def test_stocks_status_filter(self, client_factory, session_factory):
        """status=success / failed 筛选。"""
        from app.models.history_sync import StockSyncState as SSS

        self._seed_stocks(session_factory, n=3)
        with session_factory() as session:
            session.add(SSS(
                dataset="daily", instrument_id="CN:STOCK:000001",
                ts_code="000001.SZ", watermark_date=date(2026, 9, 14),
                last_status="failed",
                last_attempt_at=now_beijing(),
                created_at=now_beijing(), updated_at=now_beijing(),
            ))
            session.add(SSS(
                dataset="daily", instrument_id="CN:STOCK:000002",
                ts_code="000002.SZ", watermark_date=date(2026, 9, 16),
                last_status="success",
                last_success_at=now_beijing(), last_attempt_at=now_beijing(),
                created_at=now_beijing(), updated_at=now_beijing(),
            ))
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            success = client.get("/api/admin/history-data/stocks?dataset=daily&status=success").json()
            failed = client.get("/api/admin/history-data/stocks?dataset=daily&status=failed").json()

        assert len(success["items"]) == 1
        assert success["items"][0]["ts_code"] == "000002.SZ"
        assert len(failed["items"]) == 1
        assert failed["items"][0]["ts_code"] == "000001.SZ"

    def test_stocks_search_by_name_or_code(self, client_factory, session_factory):
        """q 参数按 name / ts_code 模糊搜索。"""
        self._seed_stocks(session_factory, n=5)
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            # 按代码搜
            r1 = client.get("/api/admin/history-data/stocks?dataset=daily&q=000003").json()
            assert len(r1["items"]) == 1
            assert r1["items"][0]["ts_code"] == "000003.SZ"
            # 按名称搜
            r2 = client.get("/api/admin/history-data/stocks?dataset=daily&q=股票5").json()
            assert len(r2["items"]) == 1
            assert r2["items"][0]["name"] == "股票5"

    def test_stocks_pagination(self, client_factory, session_factory):
        """分页：page_size=100，超过时分页。"""
        from app.models.history_sync import StockSyncState as SSS

        self._seed_stocks(session_factory, n=150)
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            p1 = client.get("/api/admin/history-data/stocks?dataset=daily&page=1").json()
            p2 = client.get("/api/admin/history-data/stocks?dataset=daily&page=2").json()

        assert len(p1["items"]) == 100
        assert p1["pagination"]["page"] == 1
        assert p1["pagination"]["total"] == 150
        assert p1["pagination"]["total_pages"] == 2

        assert len(p2["items"]) == 50
        assert p2["pagination"]["page"] == 2


class TestTasks:
    """GET /api/admin/history-data/tasks/{task_id} 任务详情测试（tasks 8.3）。"""

    def _seed_task(self, session_factory):
        from app.models.instrument import Instrument
        from app.models.history_market import CnStockBasic
        from app.models.history_sync import SyncTask

        with session_factory() as session:
            session.add(Instrument(
                instrument_id="CN:STOCK:000001", symbol="000001",
                name="平安银行", market="CN", asset_type="STOCK",
                currency="CNY", exchange="SZSE", is_active=True,
            ))
            session.flush()
            session.add(CnStockBasic(
                instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                symbol="000001", name="平安银行",
                list_date=date(2010, 1, 4),
                source="tushare", fetched_at=now_beijing(),
                source_last_seen_at=now_beijing(),
            ))
            started = now_beijing()
            task = SyncTask(
                id=1,  # 手动指定 id（sequence 在 DuckDB 中测试不太方便）
                run_id="run-task-1", dataset=DatasetName.DAILY.value,
                instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                start_date=date(2026, 9, 15), end_date=date(2026, 9, 16),
                status="success", retry_count=0, attempt_count=1,
                records_fetched=2, records_written=2,
                started_at=started, finished_at=started + timedelta(seconds=3),
                duration_ms=3000,
                created_at=started,
            )
            session.add(task)
            session.commit()
            return task.id

    def test_task_detail_success(self, client_factory, session_factory):
        """任务详情正常返回 + JOIN 主档补 stock_name。"""
        task_id = self._seed_task(session_factory)
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get(f"/api/admin/history-data/tasks/{task_id}").json()

        assert body["id"] == task_id
        assert body["run_id"] == "run-task-1"
        assert body["dataset"] == "daily"
        assert body["ts_code"] == "000001.SZ"
        assert body["stock_name"] == "平安银行"
        assert body["status"] == "success"
        assert body["records_fetched"] == 2
        assert body["records_written"] == 2
        assert body["duration_ms"] == 3000
        assert body["started_at"].endswith("+08:00")
        assert "error_code" in body
        assert "error_message" in body

    def test_task_not_found(self, client_factory, session_factory):
        """不存在的 task_id → 404。"""
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            resp = client.get("/api/admin/history-data/tasks/999999")
            assert resp.status_code == 404
            assert "不存在" in resp.json()["detail"]

    def test_task_requires_admin(self, client_factory):
        """普通用户 → 403。"""
        with client_factory(FakeNameProvider(), login_as="alice") as client:
            assert client.get("/api/admin/history-data/tasks/1").status_code == 403

    def test_task_requires_login(self, client_factory):
        """未登录 → 401。"""
        with client_factory(FakeNameProvider()) as client:
            assert client.get("/api/admin/history-data/tasks/1").status_code == 401


class TestDatasetsEndpoint:
    def test_datasets_returns_daily_and_master(self, client_factory, session_factory):
        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            body = client.get("/api/admin/history-data/datasets").json()
        assert "daily" in body
        assert "master" in body
        assert len(body["daily"]) == 4
        assert any(d["dataset"] == "daily" for d in body["daily"])


class TestNoTokenLeak:
    def test_all_endpoints_never_contain_token(self, client_factory, session_factory):
        """§51.3/§62：API 响应不得出现 Token 明文（沿 config 无 Token 路径）。"""
        from app.models.history_sync import SyncTask
        from app.models.instrument import Instrument
        from app.models.history_market import CnStockBasic

        # 构造测试数据确保各端点有内容返回
        _seed_calendar(session_factory)
        _seed_state(session_factory, DatasetName.DAILY,
                    status=DatasetStatus.CAUGHT_UP.value)
        with session_factory() as session:
            session.add(Instrument(
                instrument_id="CN:STOCK:000001", symbol="000001",
                name="测试", market="CN", asset_type="STOCK",
                currency="CNY", exchange="SZSE", is_active=True,
            ))
            session.flush()
            session.add(CnStockBasic(
                instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                symbol="000001", name="测试",
                list_date=date(2010, 1, 4),
                source="tushare", fetched_at=now_beijing(),
                source_last_seen_at=now_beijing(),
            ))
            started = now_beijing()
            session.add(SyncTask(
                id=42, run_id="r1", dataset=DatasetName.DAILY.value,
                instrument_id="CN:STOCK:000001", ts_code="000001.SZ",
                start_date=date(2026, 9, 15), end_date=date(2026, 9, 16),
                status="success", records_fetched=1, records_written=1,
                started_at=started, finished_at=started, duration_ms=100,
                created_at=started,
            ))
            session.commit()

        with client_factory(FakeNameProvider(), login_as="admin", role="admin") as client:
            responses = [
                client.get("/api/admin/history-data/summary"),
                client.get("/api/admin/history-data/runs"),
                client.get("/api/admin/history-data/datasets"),
                client.get("/api/admin/history-data/stocks?dataset=daily"),
                client.get("/api/admin/history-data/tasks/42"),
            ]
        for resp in responses:
            assert resp.status_code == 200, resp.url
            text = resp.text.lower().replace("last_error_code", "")
            assert "token" not in text, f"{resp.url} 响应包含 token"
