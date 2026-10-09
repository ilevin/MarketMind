"""ETF 历史数据统一查询（etf-data-module，design D10）。

``get_etf_daily(symbol, start_date, end_date, adjust)``：项目首个事实表读
路径——``etf_daily``/``etf_adj_factor`` 行级 SELECT（升序），raw 直读；
qfq/hfq 在查询时动态计算，**复权价绝不落库**：

- ``hfq = raw × factor_t``
- ``qfq = raw × factor_t / factor_latest``（factor_latest = 该 ETF 全库
  最新 adj_factor，与券商行情软件口径一致——当前价=真实价；历史区间
  回测注意基准随新因子滚动，文档明示）
- 复权只作用于价格字段（open/high/low/close），NULL 原样保留；
  volume/amount/turnover_rate 不变。

因子覆盖语义（spike 前设计基线）：假设 fund_adj 每交易日一行（同股票
adj_factor）。若 spike 证实"仅除权事件日有行"，改 ``_factor_at`` 单点为
"按 ≤t 的最近因子行 forward-fill"——公式不变、取因子方式变化，spike
结论只改这一处。

跨源一致性（PRD §8 落地，design D10）：
- 区间内日线有行而因子缺失 → 明确报错列出缺失日（不静默回退 raw、
  不用相邻日因子外推掩盖缺口）；
- 因子全空（该 ETF 全库无因子行）→ "复权因子未同步"报错；
- 因子存在而日线缺失（停牌日有因子行）→ 该日无行情输出，因子仍参与
  factor_latest 基准；
- raw 查询不受因子表状态影响。

V1 不做区间行数硬限制（单 ETF 单年约 250 行，16 年约 4000 行，
design D10）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.history_fact import HISTORY_FACT_TABLES
from app.models.history_market import CnEtfBasic
from app.models.instrument import Instrument
from app.providers.base import EtfDailyBar

logger = logging.getLogger(__name__)

# 合法 adjust 取值（API 层 Literal 校验之外的防御）
ADJUST_MODES = ("raw", "qfq", "hfq")

# 复权作用的价格字段（volume/amount/turnover_rate 不参与复权）
_PRICE_FIELDS = ("open", "high", "low", "close")


class EtfDataError(Exception):
    """ETF 数据查询业务异常基类。"""


class UnknownEtfError(EtfDataError):
    """未知 ETF 代码（instrument 主档中不存在）。"""


class AdjustFactorNotSyncedError(EtfDataError):
    """复权因子未同步（该 ETF 全库无因子行）。"""


class AdjustFactorMissingDaysError(EtfDataError):
    """区间内日线有行而因子缺失的交易日存在；missing_days 为升序日期列表。"""

    def __init__(self, message: str, *, missing_days: list[date]):
        super().__init__(message)
        self.missing_days = missing_days


@dataclass(frozen=True)
class EtfDailyQueryResult:
    """get_etf_daily 查询结果（API 响应体直接序列化）。"""

    symbol: str
    name: str | None
    ts_code: str
    instrument_id: str
    adjust: str
    items: list[EtfDailyBar]


class EtfDataService:
    """ETF 历史数据查询（读路径；session 由调用方管理）。"""

    def __init__(self, session: Session):
        self.session = session

    # ---- 唯一入口 ----

    def get_etf_daily(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        adjust: str = "raw",
    ) -> EtfDailyQueryResult:
        """统一查询：raw 直读；qfq/hfq 动态复权（design D10）。

        异常：UnknownEtfError（未知代码）/ AdjustFactorNotSyncedError
        （因子全空）/ AdjustFactorMissingDaysError（区间内因子缺口）。
        """
        if adjust not in ADJUST_MODES:
            raise ValueError(f"adjust 仅允许 {ADJUST_MODES}，收到 {adjust!r}")
        if start_date > end_date:
            raise ValueError(f"start_date({start_date}) 不能晚于 end_date({end_date})")

        basic = self._locate(symbol)
        instrument_id = basic.instrument_id

        raw_items = self._read_etf_daily(instrument_id, start_date, end_date)

        if adjust == "raw":
            return EtfDailyQueryResult(
                symbol=basic.symbol,
                name=self._instrument_name(instrument_id),
                ts_code=basic.ts_code,
                instrument_id=instrument_id,
                adjust=adjust,
                items=raw_items,
            )

        # qfq/hfq：读该 ETF 全部因子行（含区间外——factor_latest 基准用）
        factors, factor_latest = self._read_factors(instrument_id)
        if not factors:
            raise AdjustFactorNotSyncedError(
                f"复权因子未同步：{basic.ts_code} 全库无 etf_adj_factor 行"
                f"（需 Tushare fund_adj 数据源，检查 etf_adj_factor 数据集同步状态）"
            )

        # 跨源一致性：区间内日线有行而因子缺失 → 明确报错列出缺失日
        # （每日一行语义下 _factor_at 直接命中；检查在复权计算前完成）
        missing_days = [bar.trade_date for bar in raw_items if bar.trade_date not in factors]
        if missing_days:
            raise AdjustFactorMissingDaysError(
                f"复权因子缺失 {len(missing_days)} 个交易日: "
                f"{', '.join(d.isoformat() for d in missing_days[:10])}"
                f"{'…' if len(missing_days) > 10 else ''}"
                f"（etf_adj_factor 水位未追平或 fund_adj 不覆盖 {basic.ts_code}）",
                missing_days=missing_days,
            )

        if adjust == "hfq":
            def ratio_at(t: date) -> float:
                return self._factor_at(factors, t)
        else:  # qfq
            def ratio_at(t: date) -> float:
                return self._factor_at(factors, t) / factor_latest

        items = [self._apply_adjust(bar, ratio_at(bar.trade_date)) for bar in raw_items]
        return EtfDailyQueryResult(
            symbol=basic.symbol,
            name=self._instrument_name(instrument_id),
            ts_code=basic.ts_code,
            instrument_id=instrument_id,
            adjust=adjust,
            items=items,
        )

    # ---- 因子取法单点 ----

    @staticmethod
    def _factor_at(factors: dict[date, float], t: date) -> float:
        """取 t 交易日的复权因子（因子取法单点，design D10）。

        每日一行基线（fund_adj 每交易日一行，同股票 adj_factor）——
        调用方缺失日检查已保证 t 在 factors 中。spike 证实"仅除权事件日
        有行"时改此单点为"按 ≤t 的最近因子行 forward-fill"，公式不变。
        """
        return factors[t]

    # ---- 读路径 ----

    def _locate(self, symbol: str) -> CnEtfBasic:
        """symbol（6 位）→ cn_etf_basic 主档；未知代码明确报错。"""
        row = self.session.scalar(
            select(CnEtfBasic).where(CnEtfBasic.symbol == symbol)
        )
        if row is None:
            raise UnknownEtfError(
                f"未知 ETF 代码: {symbol}（不在 ETF 主档中，"
                f"请确认代码正确或等待 universe 刷新）"
            )
        return row

    def _instrument_name(self, instrument_id: str) -> str | None:
        inst = self.session.get(Instrument, instrument_id)
        return inst.name if inst else None

    def _read_etf_daily(
        self, instrument_id: str, start_date: date, end_date: date
    ) -> list[EtfDailyBar]:
        """区间 raw 日线（升序；项目首个事实表读路径，design D10）。"""
        table = HISTORY_FACT_TABLES["etf_daily"]
        rows = self.session.execute(
            select(table)
            .where(
                table.c.instrument_id == instrument_id,
                table.c.trade_date >= start_date,
                table.c.trade_date <= end_date,
            )
            .order_by(table.c.trade_date.asc())
        ).mappings()
        return [
            EtfDailyBar(
                instrument_id=row["instrument_id"],
                ts_code=row["ts_code"],
                trade_date=row["trade_date"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                amount=row["amount"],
                turnover_rate=row["turnover_rate"],
            )
            for row in rows
        ]

    def _read_factors(self, instrument_id: str) -> tuple[dict[date, float], float | None]:
        """读该 ETF 全部因子行（含区间外，factor_latest 基准用）。

        返回 ({trade_date: adj_factor}, 全库最新因子)。无行时 ({}, None)。
        """
        table = HISTORY_FACT_TABLES["etf_adj_factor"]
        rows = self.session.execute(
            select(table.c.trade_date, table.c.adj_factor)
            .where(table.c.instrument_id == instrument_id)
            .order_by(table.c.trade_date.asc())
        ).all()
        factors = {row.trade_date: row.adj_factor for row in rows}
        factor_latest = factors[max(factors)] if factors else None
        return factors, factor_latest

    # ---- 复权计算 ----

    @staticmethod
    def _apply_adjust(bar: EtfDailyBar, ratio: float) -> EtfDailyBar:
        """价格字段乘 ratio（NULL 保留）；volume/amount/turnover_rate 不变。"""
        prices = {
            f: (getattr(bar, f) * ratio if getattr(bar, f) is not None else None)
            for f in _PRICE_FIELDS
        }
        return EtfDailyBar(
            instrument_id=bar.instrument_id,
            ts_code=bar.ts_code,
            trade_date=bar.trade_date,
            open=prices["open"],
            high=prices["high"],
            low=prices["low"],
            close=prices["close"],
            volume=bar.volume,
            amount=bar.amount,
            turnover_rate=bar.turnover_rate,
        )
