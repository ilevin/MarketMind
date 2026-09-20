"""日级数据集待处理日期计算与水位对账（a-share-historical-data，规范
historical-data-sync：严格交易日历推进、水位一致性对账）。

纯函数集合，不做任何 I/O：调用方（HistorySyncService）负责用严格交易
日历（``TushareTradingCalendarProvider.get_days(..., strict=True)``）与
``HistoryDayStatusRepository`` 取得所需数据后传入。这样可脱离数据库直接
单元测试（任务 5.9）。

目标日期（latest_expected_trade_date）由 ``AvailabilityPolicy`` 计算，不
在本模块重复；本模块只消费该目标计算待处理列表与落后天数。
"""

from __future__ import annotations

from datetime import date, timedelta


class HistorySyncPlanner:
    """水位 -> 待处理日期列表、落后天数、reconcile 回退（均为静态纯函数）。"""

    @staticmethod
    def pending_dates(
        *,
        watermark: date | None,
        target: date,
        history_start_date: date,
        open_days: list[date],
    ) -> list[date]:
        """严格日历升序待处理列表：``trade_date > 水位 AND trade_date <= target``。

        水位为 None（首次同步）时从 ``history_start_date`` 起的第一个
        open day 开始（而非 history_start_date 本身，若非交易日则自然
        由 open_days 过滤掉）。``open_days`` 须为升序且只含 is_open=True
        的严格交易日；不使用 date+1 或工作日近似（由调用方经严格日历
        Provider 保证）。
        """
        start = watermark + timedelta(days=1) if watermark is not None else history_start_date
        if start > target:
            return []
        return [day for day in open_days if start <= day <= target]

    @staticmethod
    def lag_days(
        *,
        latest_complete: date | None,
        latest_expected: date | None,
        open_days: list[date],
    ) -> int:
        """落后交易日天数（管理员页面展示用）：目标以内、已完成之后的交易日数。"""
        if latest_expected is None:
            return 0
        if latest_complete is None:
            return len([day for day in open_days if day <= latest_expected])
        return len([day for day in open_days if latest_complete < day <= latest_expected])

    @staticmethod
    def reconcile_watermark(
        *,
        watermark: date | None,
        expected_days: list[date],
        completed_dates: set[date],
    ) -> date | None:
        """水位一致性对账：按严格日历升序扫描 ``expected_days``（起点到水位
        的完整交易日区间），找到第一个在 ``completed_dates``（day ledger 的
        COMPLETE 记录）中缺失的交易日，回退水位到其前一交易日。

        ``expected_days`` 须为升序、覆盖 history_start_date 到 watermark
        （含）区间的全部严格交易日。一致（无缺口）时原样返回传入的
        watermark；水位为 None 时无需对账，原样返回 None；缺口出现在
        第一个交易日时回退为 None（尚无任何已确认完成的交易日）。
        """
        if watermark is None:
            return None
        for index, day in enumerate(expected_days):
            if day not in completed_dates:
                return expected_days[index - 1] if index > 0 else None
        return watermark
