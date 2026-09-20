"""HistorySyncPlanner / AvailabilityPolicy 离线单测（tasks 5.9）。

覆盖：latest_expected_trade_date 的 cutoff 判定（盘中未到发布时间回退到
上一交易日、已过发布时间取当日）；pending_dates 的 NULL 水位起点、一年
backlog 完整列表、无 pending；reconcile_watermark 的一致场景与缺口回退
（含缺口出现在第一个交易日的边界）。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import AppConfig
from app.models.history_sync import DatasetName
from app.services.history.availability import AvailabilityPolicy
from app.services.history.planner import HistorySyncPlanner

_BEIJING = ZoneInfo("Asia/Shanghai")


class TestAvailabilityPolicy:
    def setup_method(self) -> None:
        self.policy = AvailabilityPolicy(AppConfig())

    def test_before_cutoff_falls_back_to_previous_open_day(self) -> None:
        open_days = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
        now = datetime(2026, 9, 17, 10, 0, tzinfo=_BEIJING)
        result = self.policy.latest_expected_trade_date(
            DatasetName.DAILY, now=now, strict_open_days=open_days
        )
        assert result == date(2026, 9, 16)

    def test_after_cutoff_takes_today(self) -> None:
        open_days = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
        now = datetime(2026, 9, 17, 17, 0, tzinfo=_BEIJING)
        result = self.policy.latest_expected_trade_date(
            DatasetName.DAILY, now=now, strict_open_days=open_days
        )
        assert result == date(2026, 9, 17)

    def test_empty_calendar_returns_none(self) -> None:
        now = datetime(2026, 9, 17, 17, 0, tzinfo=_BEIJING)
        result = self.policy.latest_expected_trade_date(
            DatasetName.DAILY, now=now, strict_open_days=[]
        )
        assert result is None


class TestPendingDates:
    def test_null_watermark_starts_from_history_start_date(self) -> None:
        open_days = [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)]
        result = HistorySyncPlanner.pending_dates(
            watermark=None,
            target=date(2010, 1, 6),
            history_start_date=date(2010, 1, 1),
            open_days=open_days,
        )
        assert result == open_days

    def test_full_year_backlog_returns_complete_ordered_list(self) -> None:
        open_days = [
            date(2025, 9, 1),
            date(2025, 9, 2),
            date(2026, 9, 15),
            date(2026, 9, 16),
        ]
        result = HistorySyncPlanner.pending_dates(
            watermark=date(2025, 9, 1),
            target=date(2026, 9, 16),
            history_start_date=date(2010, 1, 1),
            open_days=open_days,
        )
        assert result == [date(2025, 9, 2), date(2026, 9, 15), date(2026, 9, 16)]

    def test_watermark_equals_target_returns_empty(self) -> None:
        open_days = [date(2026, 9, 16), date(2026, 9, 17)]
        result = HistorySyncPlanner.pending_dates(
            watermark=date(2026, 9, 17),
            target=date(2026, 9, 17),
            history_start_date=date(2010, 1, 1),
            open_days=open_days,
        )
        assert result == []

    def test_watermark_ahead_of_target_returns_empty(self) -> None:
        result = HistorySyncPlanner.pending_dates(
            watermark=date(2026, 9, 18),
            target=date(2026, 9, 17),
            history_start_date=date(2010, 1, 1),
            open_days=[date(2026, 9, 17), date(2026, 9, 18)],
        )
        assert result == []


class TestLagDays:
    def test_lag_counts_open_days_after_complete_up_to_expected(self) -> None:
        open_days = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
        lag = HistorySyncPlanner.lag_days(
            latest_complete=date(2026, 9, 14),
            latest_expected=date(2026, 9, 17),
            open_days=open_days,
        )
        assert lag == 3

    def test_lag_zero_when_caught_up(self) -> None:
        open_days = [date(2026, 9, 16), date(2026, 9, 17)]
        lag = HistorySyncPlanner.lag_days(
            latest_complete=date(2026, 9, 17),
            latest_expected=date(2026, 9, 17),
            open_days=open_days,
        )
        assert lag == 0

    def test_lag_none_expected_is_zero(self) -> None:
        lag = HistorySyncPlanner.lag_days(
            latest_complete=None, latest_expected=None, open_days=[]
        )
        assert lag == 0


class TestReconcileWatermark:
    def test_null_watermark_needs_no_reconcile(self) -> None:
        result = HistorySyncPlanner.reconcile_watermark(
            watermark=None, expected_days=[], completed_dates=set()
        )
        assert result is None

    def test_consistent_watermark_unchanged(self) -> None:
        days = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
        result = HistorySyncPlanner.reconcile_watermark(
            watermark=date(2026, 9, 17),
            expected_days=days,
            completed_dates=set(days),
        )
        assert result == date(2026, 9, 17)

    def test_gap_rolls_back_to_previous_trade_date(self) -> None:
        days = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
        completed = {date(2026, 9, 15), date(2026, 9, 17)}
        result = HistorySyncPlanner.reconcile_watermark(
            watermark=date(2026, 9, 17), expected_days=days, completed_dates=completed
        )
        assert result == date(2026, 9, 15)

    def test_gap_on_first_day_rolls_back_to_none(self) -> None:
        days = [date(2026, 9, 15), date(2026, 9, 16)]
        completed: set[date] = set()
        result = HistorySyncPlanner.reconcile_watermark(
            watermark=date(2026, 9, 16), expected_days=days, completed_dates=completed
        )
        assert result is None
