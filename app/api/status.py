"""运行状态查询 API（v0.03 技术方案 §26/§33）：版本、后台 Job 状态与 Provider 指标。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth.dependencies import require_admin
from app.config import BUSINESS_TZ_NAME
from app.version import APP_VERSION

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

_BEIJING = ZoneInfo(BUSINESS_TZ_NAME)

# 未运行过的 Job 也要出现在 jobs 中（字段为 null 而非缺键，job-status spec）。
# 新增 Job 必须同步登记，并由 tests 中的一致性断言守护，避免与
# 各 Job 类的 JOB_NAME 漂移：quote_refresh / fundamental_refresh / history_sync。
JOB_NAMES = ("quote_refresh", "fundamental_refresh", "history_sync")
PROVIDER_SOURCES = ("tencent", "akshare", "tushare")


class JobStatusItem(BaseModel):
    job_name: str
    last_started_at: str | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None
    consecutive_failures: int = 0


class ProviderStatusItem(BaseModel):
    request_count: int = 0
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error: str | None = None
    last_duration_ms: int | None = None


class AdminStatusResponse(BaseModel):
    version: str
    jobs: dict[str, JobStatusItem]
    providers: dict[str, ProviderStatusItem]


def _iso_beijing(dt: datetime | None) -> str | None:
    """北京时间带时区 ISO 格式（现有 API 时间约定）。

    输入恒为 aware：DuckDB TIMESTAMPTZ 保真往返（design D4）+
    内存 ProviderMetrics 记录的 aware UTC，做真正的时区换算。
    """
    if dt is None:
        return None
    return dt.astimezone(_BEIJING).isoformat()


def collect_admin_status(request: Request) -> AdminStatusResponse:
    """后台 Job 最近运行状态 + Provider 运行指标；未运行过的字段为 null 而非缺键。

    API（GET /api/admin/status）与 /admin/status 页面共用（页面为服务端渲染）。
    """
    jobs: dict[str, JobStatusItem] = {}
    job_service = getattr(request.app.state, "job_status_service", None)
    if job_service is not None:
        for row in job_service.get_all():
            jobs[row.job_name] = JobStatusItem(
                job_name=row.job_name,
                last_started_at=_iso_beijing(row.last_started_at),
                last_success_at=_iso_beijing(row.last_success_at),
                last_error_at=_iso_beijing(row.last_error_at),
                last_error=row.last_error,
                last_duration_ms=row.last_duration_ms,
                consecutive_failures=row.consecutive_failures or 0,
            )
    for name in JOB_NAMES:
        jobs.setdefault(name, JobStatusItem(job_name=name))

    providers: dict[str, ProviderStatusItem] = {}
    metrics_registry = getattr(request.app.state, "provider_metrics", None)
    if metrics_registry is not None:
        for source, m in metrics_registry.all().items():
            providers[source] = ProviderStatusItem(
                request_count=m.request_count,
                success_count=m.success_count,
                error_count=m.error_count,
                timeout_count=m.timeout_count,
                last_success_at=_iso_beijing(m.last_success_at),
                last_error_at=_iso_beijing(m.last_error_at),
                last_error=m.last_error,
                last_duration_ms=m.last_duration_ms,
            )
    for source in PROVIDER_SOURCES:
        providers.setdefault(source, ProviderStatusItem())

    return AdminStatusResponse(version=APP_VERSION, jobs=jobs, providers=providers)


@router.get("/status", response_model=AdminStatusResponse)
def get_admin_status(request: Request):
    return collect_admin_status(request)
