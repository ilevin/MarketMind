"""交易日历 Provider：Tushare trade_cal + DuckDB 缓存（按年批量）。

时区规则（design.md D5.1）：
    日历查询所用日期由调用方（MarketSessionService）以北京时间推导，
    本 Provider 只接收 date，不做时区换算；Tushare 日历日期即市场本地自然日。

数据源降级：
    - CN：Tushare trade_cal（SSE）
    - HK：尝试 Tushare trade_cal（HKEX）；不可用则回退「周一至周五」近似，
      并记录警告（节假日会误判为交易日，只影响刷新尝试，不影响数据正确性）
    - 无 Token / 请求失败：回退近似规则，不缓存近似结果，待数据源可用后修正

严格模式（a-share-historical-data，技术方案 §11/§31.6）：``get_days(strict=True)``
仅供历史同步使用——必须来自 Tushare、无 weekday fallback、不补造缺失日，
上游不完整即抛 ``CalendarUnavailableError``，并落库/返回 pretrade_date 与
source 元数据。``strict=False`` 与既有实时行为完全一致。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import AppConfig
from app.db import write_coordinator
from app.providers.tushare_common import TushareTransport
from app.repositories.trading_calendar import (
    CalendarDayRecord,
    TradingCalendarRepository,
)

logger = logging.getLogger(__name__)

# Tushare 交易所代码
_EXCHANGES = {"CN": "SSE", "HK": "HKEX"}

# trade_cal 显式字段（技术方案 §32 精神：不依赖默认返回列）
TRADE_CAL_FIELDS = "exchange,cal_date,is_open,pretrade_date"

CALENDAR_SOURCE = "tushare"


class CalendarUnavailableError(RuntimeError):
    """严格交易日历不可用（技术方案 §51.3 错误码 CALENDAR_UNAVAILABLE）。"""

    error_code = "CALENDAR_UNAVAILABLE"


def _parse_cal_date(value) -> date | None:
    """Tushare YYYYMMDD -> date；空值归 None；不可解析抛异常。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text in {"nan", "None", "NaT"}:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (ValueError, IndexError):
        raise CalendarUnavailableError(f"交易日历日期不可解析: {text!r}") from None


def _parse_is_open(value) -> bool:
    """trade_cal is_open（0/1，偶为字符串）-> bool。"""
    if isinstance(value, str):
        return value.strip() in {"1", "true", "True"}
    return bool(value)


def _weekday_approx(day: date) -> bool:
    """近似规则：周一至周五视为交易日（无数据源时的回退，不缓存）。"""
    return day.weekday() < 5


class TushareTradingCalendarProvider:
    def __init__(
        self,
        config: AppConfig,
        session_factory: Callable[[], Session],
        transport: TushareTransport | None = None,
    ):
        self.config = config
        self.session_factory = session_factory
        self._transport = transport
        self._warned_fallback = False

    def _get_transport(self) -> TushareTransport:
        # 未显式注入时基于自身 config 构造；gate 取进程级共享单例（技术方案 §30）。
        # 经 transport 的 client 构造统一注入 timeout（此前 pro_api(token) 无超时）。
        if self._transport is None:
            self._transport = TushareTransport(self.config)
        return self._transport

    def is_trading_day(self, market: str, day: date) -> bool:
        market = market.upper()
        with self.session_factory() as session:
            cached = TradingCalendarRepository(session).get(market, day)
        if cached is not None:
            return cached

        self._load_year(market, day.year)

        with self.session_factory() as session:
            cached = TradingCalendarRepository(session).get(market, day)
        if cached is not None:
            return cached

        # 数据源不可用：近似规则（不写库，避免把不可靠数据固化）
        if not self._warned_fallback:
            logger.warning(
                "交易日历数据源不可用（market=%s），临时使用周一至周五近似规则", market
            )
            self._warned_fallback = True
        return _weekday_approx(day)

    def get_days(
        self,
        market: str,
        start_date: date,
        end_date: date,
        *,
        strict: bool = False,
    ) -> list[CalendarDayRecord]:
        """按日期范围返回逐日日历记录（升序）。

        ``strict=False``：逐日走既有 ``is_trading_day``（含 weekday 降级），
        行为与现有实时路径完全一致；``strict=True``：必须来自 Tushare、
        缺数据抛 ``CalendarUnavailableError``（技术方案 §31.6）。
        """
        market = market.upper()
        if not strict:
            records: list[CalendarDayRecord] = []
            day = start_date
            while day <= end_date:
                records.append(
                    CalendarDayRecord(trade_date=day, is_open=self.is_trading_day(market, day))
                )
                day += timedelta(days=1)
            return records
        return self._get_days_strict(market, start_date, end_date)

    def _get_days_strict(
        self, market: str, start: date, end: date
    ) -> list[CalendarDayRecord]:
        """严格日历：按年拉取缓存（source='tushare' 才算数），范围完整性
        校验失败即抛异常——禁止 weekday 近似与补造缺失日（§11.1）。"""
        for year in range(start.year, end.year + 1):
            with self.session_factory() as session:
                if TradingCalendarRepository(session).has_strict_year(market, year):
                    continue
            # 网络请求在写锁外；落库 + 提交持锁串行化（design D6）。
            days = self._fetch_year_strict(market, year)
            with write_coordinator.write():
                with self.session_factory() as session:
                    repo = TradingCalendarRepository(session)
                    # 其他任务可能已在网络请求期间完成同一年份的严格缓存。
                    if not repo.has_strict_year(market, year):
                        try:
                            repo.save_days_strict(
                                market,
                                days,
                                exchange=_EXCHANGES.get(market, "SSE"),
                                source=CALENDAR_SOURCE,
                                fetched_at=datetime.now(timezone.utc),
                            )
                            session.commit()
                        except Exception:
                            session.rollback()
                            raise
            logger.info(
                "已缓存 %d 年 %s 严格交易日历（%d 天，source=%s）",
                year, market, len(days), CALENDAR_SOURCE,
            )

        with self.session_factory() as session:
            rows = TradingCalendarRepository(session).get_days_between(market, start, end)
        by_date = {row.trade_date: row for row in rows}
        missing: list[date] = []
        day = start
        while day <= end:
            row = by_date.get(day)
            if row is None or row.source != CALENDAR_SOURCE:
                missing.append(day)
            day += timedelta(days=1)
        if missing:
            raise CalendarUnavailableError(
                f"严格交易日历数据不完整（market={market}，"
                f"范围 {start}~{end} 缺失 {len(missing)} 天，首个缺失 {missing[0]}）"
            )
        return [
            CalendarDayRecord(
                trade_date=row.trade_date,
                is_open=row.is_open,
                pretrade_date=row.pretrade_date,
            )
            for row in sorted(rows, key=lambda r: r.trade_date)
        ]

    def _fetch_year_strict(self, market: str, year: int) -> list[CalendarDayRecord]:
        """从 Tushare 拉取整年严格日历（含非交易日）；任何缺失/不可解析
        均抛 CalendarUnavailableError，绝不以 weekday 补造（§31.6）。"""
        if not self.config.has_tushare_token:
            raise CalendarUnavailableError(
                f"Tushare Token 未配置，无法获取严格交易日历（market={market}）"
            )
        try:
            df = self._get_transport().call(
                "trade_cal",
                exchange=_EXCHANGES.get(market, "SSE"),
                start_date=f"{year}0101",
                end_date=f"{year}1231",
                fields=TRADE_CAL_FIELDS,
            )
        except Exception as exc:
            raise CalendarUnavailableError(
                f"严格交易日历获取失败（market={market}, {year}）: {exc}"
            ) from exc
        if df is None or len(df) == 0:
            raise CalendarUnavailableError(
                f"严格交易日历返回为空（market={market}, {year}）"
            )
        by_date: dict[date, tuple[bool, date | None]] = {}
        for row in df.to_dict("records"):
            cal_date = _parse_cal_date(row.get("cal_date"))
            if cal_date is None:
                raise CalendarUnavailableError(
                    f"严格交易日历行 cal_date 不可解析（market={market}, {year}）"
                )
            raw_open = row.get("is_open")
            if raw_open is None:
                raise CalendarUnavailableError(
                    f"严格交易日历行 is_open 为空（market={market}, {year}, {cal_date}）"
                )
            by_date[cal_date] = (_parse_is_open(raw_open), _parse_cal_date(row.get("pretrade_date")))
        days: list[CalendarDayRecord] = []
        day = date(year, 1, 1)
        while day.year == year:
            entry = by_date.get(day)
            if entry is None:
                raise CalendarUnavailableError(
                    f"严格交易日历返回不完整（market={market}, {year}，缺失 {day}）"
                )
            days.append(CalendarDayRecord(trade_date=day, is_open=entry[0], pretrade_date=entry[1]))
            day += timedelta(days=1)
        return days

    def _load_year(self, market: str, year: int) -> None:
        with self.session_factory() as session:
            if TradingCalendarRepository(session).has_year(market, year):
                return

        # 网络请求在写锁外；落库 + 提交持锁串行化（design D6）。
        days = self._fetch_year_from_tushare(market, year)
        if days:
            with write_coordinator.write():
                with self.session_factory() as session:
                    repo = TradingCalendarRepository(session)
                    # 其他任务可能已在网络请求期间完成同一年份的缓存。
                    if repo.has_year(market, year):
                        return
                    try:
                        repo.save_days(market, days)
                        session.commit()
                    except Exception:
                        session.rollback()
                        raise
            logger.info("已缓存 %s 年 %s 交易日历（%d 天）", year, market, len(days))

    def _fetch_year_from_tushare(self, market: str, year: int) -> list[tuple[date, bool]] | None:
        if not self.config.has_tushare_token:
            return None
        try:
            # 经共享 transport：统一 timeout 注入 + 全局请求节奏（技术方案 §30）
            df = self._get_transport().call(
                "trade_cal",
                exchange=_EXCHANGES.get(market, "SSE"),
                start_date=f"{year}0101",
                end_date=f"{year}1231",
            )
        except Exception as exc:
            logger.warning("Tushare 交易日历获取失败（market=%s, %s）: %s", market, year, exc)
            return None

        if df is None or len(df) == 0:
            # 交易所不支持（如 HKEX）等导致空返回：不缓存近似数据
            logger.warning("Tushare 交易日历返回为空（market=%s, %s），不缓存", market, year)
            return None

        calibrate = {
            date(int(str(r.cal_date)[:4]), int(str(r.cal_date)[4:6]), int(str(r.cal_date)[6:8])): bool(r.is_open)
            for r in df.itertuples(index=False)
        }
        days: list[tuple[date, bool]] = []
        start = date(year, 1, 1)
        for i in range(366):
            day = start + timedelta(days=i)
            if day.year != year:
                break
            days.append((day, calibrate.get(day, day.weekday() < 5)))
        return days
