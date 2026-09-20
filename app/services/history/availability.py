"""日级数据集可得性策略（a-share-historical-data，技术方案 §24、design.md D19~D20）。

每个日级数据集在当前北京时间下计算 ``latest_expected_trade_date``：
最近一个"已过发布时间"的严格交易日。用于区分"历史日期空结果=异常"与
"当日临近发布时间空结果=WAITING_SOURCE"（§34），以及手动触发时的目标定位。

时区无关：调用方传入的 ``now`` 若为 naive datetime 按北京时间解释；
aware datetime 换算为北京时间。cutoff 时刻与交易日历均为北京时间/中国
自然日语义，不新增独立时区配置（复用 ``BUSINESS_TZ_NAME``）。
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.config import AppConfig, BUSINESS_TZ_NAME
from app.models.history_sync import DatasetName

_BEIJING = ZoneInfo(BUSINESS_TZ_NAME)


def _to_beijing(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=_BEIJING)
    return now.astimezone(_BEIJING)


def _parse_cutoff(value: str) -> time:
    hour_str, minute_str = value.split(":")
    return time(int(hour_str), int(minute_str))


class AvailabilityPolicy:
    """按数据集 cutoff 计算最近已发布交易日（§24）。"""

    def __init__(self, config: AppConfig):
        self._cutoffs: dict[str, time] = {
            DatasetName.ADJ_FACTOR.value: _parse_cutoff(config.history.availability.adj_factor),
            DatasetName.DAILY.value: _parse_cutoff(config.history.availability.daily),
            DatasetName.DAILY_BASIC.value: _parse_cutoff(config.history.availability.daily_basic),
            DatasetName.MONEYFLOW.value: _parse_cutoff(config.history.availability.moneyflow),
        }

    def cutoff_for(self, dataset: DatasetName | str) -> time:
        key = dataset.value if isinstance(dataset, DatasetName) else str(dataset)
        cutoff = self._cutoffs.get(key)
        if cutoff is None:
            raise ValueError(f"无发布时间配置的数据集: {dataset}")
        return cutoff

    def latest_expected_trade_date(
        self,
        dataset: DatasetName | str,
        *,
        now: datetime,
        strict_open_days: list[date],
    ) -> date | None:
        """最近一个已过发布时间的严格交易日（§24）。

        ``strict_open_days`` 须为升序、只含 is_open=True 的严格日历日期，
        且覆盖到 ``now`` 当天（由调用方保证，本层不重新拉日历）。当天若
        是交易日但尚未到 cutoff 时刻，则回退到更早一个交易日；若范围内
        无任何已过 cutoff 的交易日，返回 None（尚无可用数据）。
        """
        if not strict_open_days:
            return None
        cutoff = self.cutoff_for(dataset)
        beijing_now = _to_beijing(now)
        today = beijing_now.date()
        candidate: date | None = None
        for day in strict_open_days:
            if day > today:
                break
            if day == today and beijing_now.time() < cutoff:
                continue
            candidate = day
        return candidate
