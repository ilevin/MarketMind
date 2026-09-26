"""个股同步有效区间计算（per-stock-history-sync，design D8）。

纯函数集合，不做任何 I/O：调用方（HistorySyncService / StockSyncExecutor）
负责用严格交易日历（``TushareTradingCalendarProvider.get_days(..., strict=True)``）
与主档生命周期信息（list_date / delist_date）取得所需数据后传入。这样可
脱离数据库直接单元测试。

个股有效区间边界（design D8）：
- 有效起点 = max(history.start_date, list_date)；list_date 缺失保守取 history.start_date
- 有效终点上界 = min(target, delist_date)；delist_date 缺失取 target
- 请求起点 = watermark 之后第一个严格交易日（无水位时 eff_start 起第一个严格交易日）
- 请求终点 = eff_end 内最近严格交易日
- 端点收敛到传入的严格交易日历 open_days（升序列表），不用 date+1
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple


class StockSyncRange(NamedTuple):
    """单股一次同步的请求区间；空区间（start > end）表示无工作。"""

    start_date: date | None
    end_date: date | None

    @property
    def is_empty(self) -> bool:
        return self.start_date is None or self.end_date is None or self.start_date > self.end_date


class HistorySyncPlanner:
    """个股有效区间与辅助计算（全部为静态纯函数）。"""

    @staticmethod
    def stock_effective_range(
        *,
        watermark: date | None,
        target: date,
        history_start_date: date,
        list_date: date | None,
        delist_date: date | None,
        open_days: list[date],
    ) -> StockSyncRange:
        """计算单股本次同步的请求区间（严格日历收敛，design D8）。

        参数：
            watermark: 该股当前水位（已确认连续的最后一个交易日），
                None 表示尚未确认任何边界、从有效起点起全量同步。
            target: 数据集目标日（由 AvailabilityPolicy 计算）。
            history_start_date: 全局历史起点（config.history.start_date）。
            list_date: 该股上市日，None 表示未知、保守取 history_start_date。
            delist_date: 该股退市日，None 表示尚在市、取 target。
            open_days: 严格交易日历升序列表（只含 is_open=True 的日期）。

        返回：
            ``StockSyncRange(start_date, end_date)``；无工作时两端为 None
            （``is_empty`` 为 True）。
        """
        # 有效起点：上市日与历史起点取其晚
        eff_start = max(history_start_date, list_date) if list_date else history_start_date
        # 有效终点：退市日与目标取其早
        eff_end = min(target, delist_date) if delist_date else target

        if eff_start > eff_end:
            # 上市晚于目标 / 退市早于历史起点等：完全无工作
            return StockSyncRange(None, None)

        # 请求起点：水位之后第一个严格交易日
        if watermark is None:
            start_candidate = eff_start
        else:
            # 从水位之后找第一个交易日；不用 date+1，靠 open_days 严格收敛
            start_candidate = watermark  # 下面会过滤 <= watermark 的日期

        # 在 open_days 中截取 [start_candidate..eff_end]
        result_days = [
            d for d in open_days
            if (d > watermark if watermark is not None else d >= eff_start)
            and d <= eff_end
            and d >= eff_start
        ]

        if not result_days:
            return StockSyncRange(None, None)

        return StockSyncRange(result_days[0], result_days[-1])
