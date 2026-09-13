"""后台任务运行状态服务（v0.03 技术方案 §23-§25）。

以 job_name 为主键的单行 upsert：开始 / 成功 / 失败三态更新。
语义（§24）：失败不清除 last_success_at；成功将 consecutive_failures 清零；
duration 由调用方计时传入。状态写入自身失败仅记日志，
绝不中断 Job 主流程（观测设施故障不拖垮刷新循环，design D8）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import BUSINESS_TZ_NAME
from app.db import write_coordinator
from app.models.job_status import JobStatus

logger = logging.getLogger(__name__)

_BEIJING = ZoneInfo(BUSINESS_TZ_NAME)


def _now() -> datetime:
    """aware 北京时间：DuckDB TIMESTAMPTZ 保真往返，全库时间列统一 aware（design D4）。"""
    return datetime.now(_BEIJING)


class JobStatusService:
    def __init__(self, session_factory: sessionmaker):
        self.session_factory = session_factory

    def _update(self, job_name: str, **fields) -> None:
        try:
            with write_coordinator.write():
                with self.session_factory() as session:
                    self._apply(session, job_name, **fields)
                    session.commit()
        except Exception:
            logger.exception("job_status 写入失败: job=%s fields=%s", job_name, list(fields))

    @staticmethod
    def _apply(session: Session, job_name: str, **fields) -> JobStatus:
        row = session.get(JobStatus, job_name)
        if row is None:
            row = JobStatus(job_name=job_name, consecutive_failures=0)
            session.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        return row

    def record_started(self, job_name: str) -> None:
        self._update(job_name, last_started_at=_now())

    def record_success(self, job_name: str, duration_ms: int) -> None:
        self._update(
            job_name,
            last_success_at=_now(),
            last_duration_ms=duration_ms,
            consecutive_failures=0,
        )

    def record_failure(self, job_name: str, duration_ms: int, error: str) -> None:
        """失败递增连续失败计数；last_success_at 保持原值（不清除）。"""
        try:
            with write_coordinator.write():
                with self.session_factory() as session:
                    row = self._apply(
                        session,
                        job_name,
                        last_error_at=_now(),
                        last_error=error,
                        last_duration_ms=duration_ms,
                    )
                    row.consecutive_failures = (row.consecutive_failures or 0) + 1
                    session.commit()
        except Exception:
            logger.exception("job_status 写入失败: job=%s (failure)", job_name)

    def get_all(self) -> list[JobStatus]:
        with self.session_factory() as session:
            return list(session.scalars(select(JobStatus).order_by(JobStatus.job_name)))
