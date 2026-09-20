"""HistorySyncJob 与 FastAPI lifespan 的接线测试（tasks 6.2）。

验证配置开关生效：``history.enabled=true`` 时挂载并启动 Job；
``false`` 时不启动（不注册任务、不产生 run）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import AppConfig, DatabaseConfig
from app.jobs.history_sync import HistorySyncJob


class FakeNameProvider:
    def get_name(self, market, asset_type, symbol):
        return None


def _make_app(duckdb_url, session_factory, *, enabled: bool):
    from app.main import create_app

    config = AppConfig(database=DatabaseConfig(url=duckdb_url))
    config.history.enabled = enabled
    # startup catch-up 会真的调 Tushare；测试里不启动调度副作用
    config.history.startup_catchup = False
    app = create_app(config)
    app.state.session_factory = session_factory
    app.state.name_provider = FakeNameProvider()
    return app


def test_job_started_when_enabled(duckdb_url, session_factory):
    """enabled=true：lifespan 挂载并启动 HistorySyncJob。"""
    app = _make_app(duckdb_url, session_factory, enabled=True)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        job = app.state.history_sync_job
        assert isinstance(job, HistorySyncJob)
        assert job._task is not None and not job._task.done(), "Job 调度循环应已启动"
        assert app.state.history_sync_service is not None


def test_job_not_started_when_disabled(duckdb_url, session_factory):
    """enabled=false：不启动 Job（配置开关必须真的生效）。"""
    app = _make_app(duckdb_url, session_factory, enabled=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.history_sync_job is None
        assert not hasattr(app.state, "history_sync_service")


def test_job_stopped_on_shutdown(duckdb_url, session_factory):
    """退出 lifespan 后调度循环已停止。"""
    app = _make_app(duckdb_url, session_factory, enabled=True)
    with TestClient(app):
        job = app.state.history_sync_job
        assert job._task is not None
    assert job._task is None, "停机后应清理调度任务"
