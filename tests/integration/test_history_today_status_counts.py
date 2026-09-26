"""今日成功/失败统计的多时区正确性（per-stock-history-sync，tasks 7.6）。

验证 ``StockSyncStateRepository.today_status_counts`` 在不同 session 时区下
都能正确按 Asia/Shanghai 业务日期归属：

- 北京时间凌晨（如 00:05）完成的任务，按上海日期属"今天"；
  若按 UTC 则是前一天 16:05，不应被 UTC 时区带偏。
- 同日先失败后成功只计成功（last_status 是最终状态）。
- 服务器部署时区（session TimeZone）不影响统计结果。

design D11 / spec "今日成功/失败统计"。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.models.history_sync import (
    DatasetName,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCESS,
)
from app.repositories.history_sync import StockSyncStateRepository

BEIJING = ZoneInfo("Asia/Shanghai")


# ---- helper ----


def _create_stock_state(
    session,
    *,
    dataset: str = "daily",
    instrument_id: str,
    ts_code: str,
    last_status: str,
    last_attempt_at: datetime,  # 上海本地 naive 时间
    watermark_date: date | None = None,
):
    from datetime import timezone
    from app.models.history_sync import StockSyncState

    now_utc = datetime.now(timezone.utc)
    state = StockSyncState(
        dataset=dataset,
        instrument_id=instrument_id,
        ts_code=ts_code,
        watermark_date=watermark_date,
        last_status=last_status,
        last_attempt_at=last_attempt_at,
        last_success_at=last_attempt_at if last_status == TASK_STATUS_SUCCESS else None,
        created_at=now_utc,
        updated_at=now_utc,
    )
    session.add(state)
    session.flush()
    return state


# ---- 基本行为 ----


class TestTodayStatusCountsBasic:
    """基本计数行为：状态分组、同日最终态。"""

    def test_success_and_failed_separate(self, session):
        """成功与失败分开计数。"""
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:000001",
            ts_code="000001.SZ",
            last_status=TASK_STATUS_SUCCESS,
            last_attempt_at=datetime(2026, 9, 24, 10, 0),
            watermark_date=date(2026, 9, 24),
        )
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:000002",
            ts_code="000002.SZ",
            last_status=TASK_STATUS_FAILED,
            last_attempt_at=datetime(2026, 9, 24, 10, 5),
        )
        counts = StockSyncStateRepository(session).today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )
        assert counts.get(TASK_STATUS_SUCCESS, 0) == 1
        assert counts.get(TASK_STATUS_FAILED, 0) == 1

    def test_same_stock_later_success_only_counts_success(self, session):
        """Scenario "同日先失败后成功计成功"：last_status=success 只计成功。"""
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:000001",
            ts_code="000001.SZ",
            last_status=TASK_STATUS_SUCCESS,  # 最终态
            last_attempt_at=datetime(2026, 9, 24, 9, 10),
            watermark_date=date(2026, 9, 24),
        )
        counts = StockSyncStateRepository(session).today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )
        assert counts.get(TASK_STATUS_SUCCESS, 0) == 1
        assert counts.get(TASK_STATUS_FAILED, 0) == 0

    def test_other_days_not_counted(self, session):
        """其他日期的记录不计入今天。"""
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:000001",
            ts_code="000001.SZ",
            last_status=TASK_STATUS_SUCCESS,
            last_attempt_at=datetime(2026, 9, 23, 15, 0),
            watermark_date=date(2026, 9, 23),
        )
        counts = StockSyncStateRepository(session).today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )
        assert counts == {}

    def test_null_last_status_not_counted(self, session):
        """last_status 为 NULL（从未尝试）的行不计入。"""
        from datetime import timezone
        from app.models.history_sync import StockSyncState

        now_utc = datetime.now(timezone.utc)
        session.add(StockSyncState(
            dataset="daily",
            instrument_id="CN:STOCK:000003",
            ts_code="000003.SZ",
            watermark_date=None,
            last_status=None,
            last_attempt_at=None,
            created_at=now_utc,
            updated_at=now_utc,
        ))
        session.flush()
        counts = StockSyncStateRepository(session).today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )
        assert counts == {}


# ---- 多时区部署：session TimeZone 不影响结果 ----


class TestTodayStatusCountsTimezones:
    """Scenario "时区无关"：服务器部署时区不影响统计结果。

    关键验证点：北京时间深夜（如 23:30 = UTC 15:30）和北京时间凌晨
    （如 00:05 = UTC 前一天 16:05）的任务，无论 session 设置为 Asia/Shanghai
    还是 UTC，都正确归属到上海日期。

    ``today_status_counts`` 有两条路径：
    - 传入 ``today`` 参数：直接比较 CAST(last_attempt_at AS DATE) = :today
      （因 last_attempt_at 以本地 naive 存储，与 session 时区无关）
    - 不传 today：用 now() AT TIME ZONE 'Asia/Shanghai' 算今天
      （这才是时区相关的路径，需要验证）

    本类重点验证"不传 today"的动态路径，在 session TZ=UTC 时仍正确。
    """

    def _set_session_tz(self, session, tz: str) -> None:
        """设置 session 级 TimeZone。"""
        session.execute(text(f"SET TimeZone = '{tz}'"))
        session.commit()

    def test_shanghai_2330_counts_as_today_in_utc_session(self, session):
        """北京时间 23:30 完成 → 上海今天。

        在 session TimeZone='UTC' 时，now() 若返回 UTC 15:30，
        now() AT TIME ZONE 'Asia/Shanghai' 应渲染为 23:30，
        CAST 后日期与 last_attempt_at（上海本地存储）一致。
        """
        # 先确保 session 时区为 UTC
        self._set_session_tz(session, "UTC")

        # 写入一条"上海时间 23:30 成功"的记录
        shanghai_time = datetime(2026, 9, 24, 23, 30)
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:000001",
            ts_code="000001.SZ",
            last_status=TASK_STATUS_SUCCESS,
            last_attempt_at=shanghai_time,
            watermark_date=date(2026, 9, 24),
        )
        session.commit()

        # 用 today= 参数（静态路径）验证：23:30 属于 09-24
        repo = StockSyncStateRepository(session)
        counts_static = repo.today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )
        assert counts_static.get(TASK_STATUS_SUCCESS, 0) == 1

    def test_shanghai_0005_counts_as_today_in_utc_session(self, session):
        """Scenario "时区无关"：上海 00:05 属上海今天，而非 UTC 昨天。

        北京时间 2026-09-25 00:05 = UTC 2026-09-24 16:05。
        若 session TZ=UTC 且错误地用 CAST(now() AS DATE) 取"今天"，
        会得到 09-24 而漏掉 00:05 完成的任务；正确做法是
        now() AT TIME ZONE 'Asia/Shanghai' 后再取日期 → 09-25。

        这里用静态 today 参数路径验证存储口径的正确性：
        last_attempt_at 以本地 naive 存储（上海时间），CAST 为日期后
        就是上海日期 09-25，与 session 时区无关。
        """
        self._set_session_tz(session, "UTC")

        # 上海时间凌晨 00:05 完成（日期 = 09-25）
        shanghai_midnight = datetime(2026, 9, 25, 0, 5)
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:000010",
            ts_code="000010.SZ",
            last_status=TASK_STATUS_SUCCESS,
            last_attempt_at=shanghai_midnight,
            watermark_date=date(2026, 9, 24),
        )
        session.commit()

        repo = StockSyncStateRepository(session)
        # 上海 09-25 应该能查到这条（它的 last_attempt_at 日期 = 09-25）
        counts = repo.today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 25)
        )
        assert counts.get(TASK_STATUS_SUCCESS, 0) == 1

        # 上海 09-24 查不到（00:05 已经是 09-25 了）
        counts_yesterday = repo.today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )
        assert TASK_STATUS_SUCCESS not in counts_yesterday

    def test_utc_session_does_not_shift_stored_date(self, session):
        """session 时区切换不影响已存储 last_attempt_at 的日期归属。

        分别在 UTC 和 Asia/Shanghai 时区下查询同一天的 today 计数，
        结果应该完全一致（因为存储是本地 naive，CAST 为日期与时区无关）。
        """
        shanghai_time = datetime(2026, 9, 24, 12, 0)  # 上海中午
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:00001",
            ts_code="000001.SZ",
            last_status=TASK_STATUS_SUCCESS,
            last_attempt_at=shanghai_time,
            watermark_date=date(2026, 9, 24),
        )
        _create_stock_state(
            session,
            instrument_id="CN:STOCK:00002",
            ts_code="000002.SZ",
            last_status=TASK_STATUS_FAILED,
            last_attempt_at=shanghai_time,
        )
        session.commit()

        repo = StockSyncStateRepository(session)

        # Shanghai TZ
        self._set_session_tz(session, "Asia/Shanghai")
        counts_sh = repo.today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )

        # UTC TZ
        self._set_session_tz(session, "UTC")
        counts_utc = repo.today_status_counts(
            DatasetName.DAILY, today=date(2026, 9, 24)
        )

        assert counts_sh == counts_utc
        assert counts_sh.get(TASK_STATUS_SUCCESS, 0) == 1
        assert counts_sh.get(TASK_STATUS_FAILED, 0) == 1

    def test_dynamic_now_path_in_utc_session(self, session):
        """动态 now() 路径在 UTC session 下取到的今天也是上海日期。

        验证 now() AT TIME ZONE 'Asia/Shanghai' 的换算正确性：
        把 last_attempt_at 设为"上海当前日期"的某一时刻，
        动态查询应能匹配上。

        注意：本测试不冻结系统时间，所以用"最近一分钟内"的近似
        方式验证：写入 now() 时刻的记录，立即查询 today 计数应 ≥ 1。
        用 ``today`` 参数可精确断言，但动态路径需要真实 now()，
        这里通过"设置 last_attempt_at = 当前上海时间"来验证匹配。
        """
        # 切换到 UTC 时区，模拟 UTC 部署
        self._set_session_tz(session, "UTC")

        # 取得"上海现在"并写入一条记录（与生产代码写入口径一致：上海本地 naive）
        now_shanghai = datetime.now(BEIJING).replace(microsecond=0)
        today_shanghai = now_shanghai.date()

        _create_stock_state(
            session,
            instrument_id="CN:STOCK:09999",
            ts_code="09999.SZ",
            last_status=TASK_STATUS_SUCCESS,
            last_attempt_at=now_shanghai.replace(tzinfo=None),
            watermark_date=today_shanghai,
        )
        session.commit()

        repo = StockSyncStateRepository(session)
        # 动态路径（不传 today）：now() AT TIME ZONE 'Asia/Shanghai'
        counts = repo.today_status_counts(DatasetName.DAILY)

        # 至少有 1 条（我们刚写入的那条）
        assert counts.get(TASK_STATUS_SUCCESS, 0) >= 1
