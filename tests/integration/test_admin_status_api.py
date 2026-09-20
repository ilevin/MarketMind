"""/api/admin/status 集成测试（v0.03 技术方案 §26/§33 + design D9）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.job_status_service import JobStatusService
from app.services.market_session_service import MarketStatus
from app.version import APP_VERSION

# 三个 Job 的 job_name（job-status spec：均 SHALL 接入 JobStatusService）
_EXPECTED_JOB_NAMES = frozenset({"quote_refresh", "fundamental_refresh", "history_sync"})


class FakeNameProvider:
    def get_name(self, market, asset_type, symbol):
        return None


class ClosedSessionService:
    def status(self, market, now=None):
        return MarketStatus.CLOSED

    def all_status(self):
        return {"CN": MarketStatus.CLOSED, "HK": MarketStatus.CLOSED}


@pytest.fixture()
def client(client_factory):
    # multi-user-auth：/api/admin/* 全部要求 admin 角色（未登录 401、普通用户 403），
    # 统一以 admin 登录访问。
    with client_factory(FakeNameProvider(), login_as="admin", role="admin") as c:
        c.app.state.session_service = ClosedSessionService()
        yield c


def test_admin_status_structure(client):
    resp = client.get("/api/admin/status")
    assert resp.status_code == 200
    body = resp.json()

    assert body["version"] == APP_VERSION
    # job-status spec：三个 Job（quote_refresh / fundamental_refresh / history_sync）
    # SHALL 分别接入 JobStatusService；未运行过的 Job 字段为 null 而非缺键。
    # history_sync 虽有独立进度表，其高层健康仍走 job_status（与其余两个 Job 同构），
    # 故 jobs 键集恒为以下三项。
    assert set(body["jobs"]) == _EXPECTED_JOB_NAMES
    for name in _EXPECTED_JOB_NAMES:
        job = body["jobs"][name]
        # 键恒在且自述名一致；值可为 null（未运行）或有效值（首轮已跑）
        assert job["job_name"] == name
        assert isinstance(job["consecutive_failures"], int)
    # history_sync 本用例内不可能运行（startup_catchup=False 且未手动触发），
    # 必须表现为 null 而非缺键——这正是 JOB_NAMES 登记与否的可见差异。
    assert body["jobs"]["history_sync"]["last_started_at"] is None
    assert set(body["providers"]) == {"tencent", "akshare", "tushare"}


def test_admin_status_job_names_match_registered_jobs():
    """JOB_NAMES 与各 Job 类的 JOB_NAME 不得漂移。

    新增 Job 时若只改了某个 Job 类而忘了登记 JOB_NAMES，未运行过的该 Job 就
    不会出现在 /api/admin/status 的 jobs 中（表现为缺键而非 null），本用例即失败。
    """
    from app.api.status import JOB_NAMES
    from app.jobs.fundamental_refresh import FundamentalRefreshJob
    from app.jobs.history_sync import HistorySyncJob
    from app.jobs.quote_refresh import QuoteRefreshJob

    assert set(JOB_NAMES) == _EXPECTED_JOB_NAMES
    assert {job.JOB_NAME for job in (QuoteRefreshJob, FundamentalRefreshJob, HistorySyncJob)} == (
        _EXPECTED_JOB_NAMES
    )


def test_admin_status_fresh_db_null_fields_not_missing_keys(client):
    """全新库：三个 Job 键存在；字段为 null 或有效值（lifespan 首轮可能已记录），绝不缺键。"""
    body = client.get("/api/admin/status").json()

    for name in sorted(_EXPECTED_JOB_NAMES):
        job = body["jobs"][name]
        # 键必须存在；值要么 None（未运行）要么为北京时间 ISO / int（已运行）
        for key in ("last_started_at", "last_success_at", "last_error_at"):
            value = job[key]
            assert value is None or value.endswith("+08:00"), f"{name}.{key}={value}"
        assert job["last_error"] is None or isinstance(job["last_error"], str)
        assert job["last_duration_ms"] is None or isinstance(job["last_duration_ms"], int)
        assert isinstance(job["consecutive_failures"], int)

    # Provider 指标键存在且计数为非负整数（lifespan 首轮可能已有真实调用）
    for source in ("tencent", "akshare", "tushare"):
        assert isinstance(body["providers"][source]["request_count"], int)
        assert body["providers"][source]["request_count"] >= 0


def test_admin_status_job_times_beijing_iso(client, tmp_path):
    """预写 job_status 后：时间字段为 +08:00 北京时间 ISO 格式。

    使用独立 job_name，避免与后台 quote_refresh Job 的周期写入竞态。
    """
    svc = JobStatusService(client.app.state.session_factory)
    svc.record_started("test_job")
    svc.record_success("test_job", 1280)

    body = client.get("/api/admin/status").json()
    job = body["jobs"]["test_job"]
    assert job["last_duration_ms"] == 1280
    assert job["consecutive_failures"] == 0
    assert job["last_success_at"].endswith("+08:00")
    assert job["last_started_at"].endswith("+08:00")
    # 时间可解析且时区为 +08:00
    parsed = datetime.fromisoformat(job["last_success_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 8 * 3600
    # 值级校验：与北京当前时刻相差 2 分钟内（曾因 UTC 落库偏差 8 小时，仅查后缀发现不了）
    from zoneinfo import ZoneInfo

    now_beijing = datetime.now(ZoneInfo("Asia/Shanghai"))
    assert abs(now_beijing - parsed) < timedelta(minutes=2)


def test_admin_status_reflects_provider_metrics(client):
    """metrics registry 有记录后，providers 计数反映。"""
    from app.observability.provider_metrics import call_with_metrics

    registry = client.app.state.provider_metrics
    before = registry.get("tencent")
    base_request = before.request_count
    base_success = before.success_count
    base_error = before.error_count

    call_with_metrics(registry, "tencent", lambda: "ok")

    def boom():
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        call_with_metrics(registry, "tencent", boom)

    body = client.get("/api/admin/status").json()
    tencent = body["providers"]["tencent"]
    assert tencent["request_count"] == base_request + 2
    assert tencent["success_count"] == base_success + 1
    assert tencent["error_count"] == base_error + 1
    assert tencent["timeout_count"] == 0
    assert tencent["last_duration_ms"] is not None
