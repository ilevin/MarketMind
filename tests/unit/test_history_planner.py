"""HistorySyncPlanner 个股有效区间单测（per-stock-history-sync，design D8）。

覆盖：
- 无水位从 eff_start（history.start_date）起第一个交易日
- 中途上市股票从 list_date 起（上市前不计缺口）
- 退市股同步至 delist_date
- delist_date 早于 history.start_date 的退市股返回空区间
- 春节休市等非交易日不产生缺口
- 已追平（watermark >= eff_end 或区间内无 open day）返回空
- watermark 在区间内：从 watermark 之后第一个 open day 起
"""

from __future__ import annotations

from datetime import date

from app.services.history.planner import HistorySyncPlanner


_HISTORY_START = date(2010, 1, 1)


class TestStockEffectiveRange:
    def test_null_watermark_starts_from_history_start_first_open_day(self) -> None:
        """无水位 + 无 list_date 限制：从历史起点起第一个交易日。"""
        open_days = [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=None,
            target=date(2010, 1, 6),
            history_start_date=_HISTORY_START,
            list_date=None,
            delist_date=None,
            open_days=open_days,
        )
        assert not result.is_empty
        assert result.start_date == date(2010, 1, 4)
        assert result.end_date == date(2010, 1, 6)

    def test_mid_ipod_stock_starts_from_list_date(self) -> None:
        """中途上市：list_date 晚于 history_start，从 list_date 起第一个交易日。"""
        open_days = [date(2015, 6, 12), date(2015, 6, 15), date(2015, 6, 16)]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=None,
            target=date(2015, 6, 16),
            history_start_date=_HISTORY_START,
            list_date=date(2015, 6, 12),
            delist_date=None,
            open_days=open_days,
        )
        assert not result.is_empty
        assert result.start_date == date(2015, 6, 12)
        assert result.end_date == date(2015, 6, 16)

    def test_delisted_stock_ends_at_delist_date(self) -> None:
        """退市股：有效终点为 delist_date（早于 target）。"""
        open_days = [date(2020, 8, 26), date(2020, 8, 27), date(2020, 8, 28)]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=None,
            target=date(2026, 9, 16),
            history_start_date=_HISTORY_START,
            list_date=date(2010, 1, 4),
            delist_date=date(2020, 8, 28),
            open_days=open_days,
        )
        assert not result.is_empty
        assert result.end_date == date(2020, 8, 28)

    def test_delisted_before_history_start_returns_empty(self) -> None:
        """退市早于历史起点：完全无工作。"""
        open_days = [date(2010, 1, 4), date(2010, 1, 5)]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=None,
            target=date(2026, 9, 16),
            history_start_date=_HISTORY_START,
            list_date=date(2008, 1, 1),
            delist_date=date(2009, 5, 1),
            open_days=open_days,
        )
        assert result.is_empty

    def test_watermark_within_range_starts_after_watermark(self) -> None:
        """水位在区间内：从水位之后第一个 open day 起（不用 date+1）。"""
        open_days = [
            date(2025, 9, 1),
            date(2025, 9, 2),
            date(2026, 9, 15),
            date(2026, 9, 16),
        ]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=date(2025, 9, 1),
            target=date(2026, 9, 16),
            history_start_date=_HISTORY_START,
            list_date=None,
            delist_date=None,
            open_days=open_days,
        )
        assert not result.is_empty
        assert result.start_date == date(2025, 9, 2)
        assert result.end_date == date(2026, 9, 16)

    def test_spring_festival_gap_produces_no_artificial_gap(self) -> None:
        """春节休市跨段：休市日不被计为缺失，区间两端为最近 open day。"""
        # 春节假期：2026-02-16 ~ 2026-02-22 休市（示例）
        open_days = [
            date(2026, 2, 13),  # 节前最后一个交易日
            date(2026, 2, 23),  # 节后第一个交易日
            date(2026, 2, 24),
        ]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=date(2026, 2, 13),
            target=date(2026, 2, 24),
            history_start_date=_HISTORY_START,
            list_date=None,
            delist_date=None,
            open_days=open_days,
        )
        assert not result.is_empty
        assert result.start_date == date(2026, 2, 23)
        assert result.end_date == date(2026, 2, 24)

    def test_caught_up_returns_empty(self) -> None:
        """水位已追平有效终点：返回空区间。"""
        open_days = [date(2026, 9, 15), date(2026, 9, 16)]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=date(2026, 9, 16),
            target=date(2026, 9, 16),
            history_start_date=_HISTORY_START,
            list_date=None,
            delist_date=None,
            open_days=open_days,
        )
        assert result.is_empty

    def test_watermark_ahead_of_target_returns_empty(self) -> None:
        """水位超前于目标：无工作（极端边界，防御性）。"""
        result = HistorySyncPlanner.stock_effective_range(
            watermark=date(2026, 9, 18),
            target=date(2026, 9, 16),
            history_start_date=_HISTORY_START,
            list_date=None,
            delist_date=None,
            open_days=[date(2026, 9, 16), date(2026, 9, 18)],
        )
        assert result.is_empty

    def test_list_date_non_trading_day_rounds_to_next_open(self) -> None:
        """list_date 本身不是交易日：收敛到其后第一个 open day。"""
        # 周六上市，下周一才是第一个交易日
        open_days = [date(2015, 6, 15), date(2015, 6, 16)]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=None,
            target=date(2015, 6, 16),
            history_start_date=_HISTORY_START,
            list_date=date(2015, 6, 13),  # 周六，非交易日
            delist_date=None,
            open_days=open_days,
        )
        assert not result.is_empty
        assert result.start_date == date(2015, 6, 15)

    def test_watermark_in_gap_starts_from_next_open_day(self) -> None:
        """watermark 落在非交易日（如停牌后）：从其后第一个 open day 起。

        watermark 本身是已确认的最后一个交易日（恒为 open day），
        因此这里验证的是"严格按 >watermark 在 open_days 中找下一天"，
        与 date+1 可能落在非交易日不同。
        """
        open_days = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)]
        result = HistorySyncPlanner.stock_effective_range(
            watermark=date(2026, 9, 15),
            target=date(2026, 9, 17),
            history_start_date=_HISTORY_START,
            list_date=None,
            delist_date=None,
            open_days=open_days,
        )
        assert not result.is_empty
        assert result.start_date == date(2026, 9, 16)
        assert result.end_date == date(2026, 9, 17)
