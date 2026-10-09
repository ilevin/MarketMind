"""东方财富 ETF 历史数据 Provider（etf-data-module）。

数据源：akshare（延迟 import，离线测试可完全避开）：
- ETF universe：``fund_etf_category_sina(symbol='ETF基金')``，返回全部上市 ETF
  （含代码/名称/市场前缀 sz/sh，不含上市日期——list_date 保存 NULL）；
- ETF 日线：``fund_etf_hist_em(symbol, period='daily', start_date, end_date,
  adjust='')``，不复权原始日线（adjust='' 实测为不复权，按 spike 固化）。

单位口径（在线实测固化，design D6）：
- 成交量 volume：手（与 Tushare vol 单位一致）；
- 成交额 amount：元（原始返回，与 Tushare 千元不同——存储时保持原样，换算在查询层）；
- 换手率 turnover_rate：百分比数值（5.23 表示 5.23%，与 Tushare 一致）。

行数上限（design D6）：单次请求最大 10000 行，超出视为 truncation_risk。

异常分类：akshare 异常经 eastmoney_common.classify_eastmoney_exception 归一化
为带 error_code 的 EastmoneyProviderError 子类（EASTMONEY_TIMEOUT /
SCHEMA_MISMATCH / UNKNOWN_INSTRUMENT / EASTMONEY_API_ERROR），向 Service 传播。

接缝设计：``get_etf_universe()`` 与 ``get_history_by_stock()`` 接收 Service
传入的 Instrument 快照（不自行访问数据库），直接返回 ProviderBatch；akshare
调用前经 gate.acquire() 节流。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from app.config import AppConfig
from app.models.instrument import Instrument
from app.providers.base import (
    AdjFactor,
    EtfDailyBar,
    EtfUniverseRecord,
    ProviderBatch,
)
from app.providers.eastmoney_common import (
    ETF_HIST_ROW_CAP,
    EastmoneyRequestGate,
    EastmoneySchemaMismatchError,
    EastmoneyUnknownInstrumentError,
    classify_eastmoney_exception,
    get_shared_gate,
)
from app.providers.safe_values import safe_date, safe_float, safe_int, safe_str

logger = logging.getLogger(__name__)

SOURCE = "eastmoney"


def _safe_float(val: Any) -> float | None:
    """包装 safe_float：空字符串/NaN 返回 None。"""
    result = safe_float(val)
    return None if result is None else result


def _safe_int(val: Any) -> int | None:
    """包装 safe_int：空字符串/NaN 返回 None。"""
    result = safe_int(val)
    return None if result is None else result


class EastmoneyEtfHistoryProvider:
    """东财 ETF 历史数据 Provider（etf-data-module）。

    职责：
    - 获取当前上市 ETF 列表（fund_etf_category_sina）；
    - 按单只 ETF 区间拉取日线历史（fund_etf_hist_em，不复权）。

    不实现 adj_factor 数据集（由 TushareHistoryProvider 扩展 fund_adj 提供）。
    """

    def __init__(
        self,
        config: AppConfig,
        gate: EastmoneyRequestGate | None = None,
    ):
        self._config = config
        self._gate = gate

    @property
    def gate(self) -> EastmoneyRequestGate:
        if self._gate is None:
            self._gate = get_shared_gate()
        return self._gate

    def get_etf_universe(self) -> ProviderBatch[EtfUniverseRecord]:
        """获取当前上市 ETF 列表（fund_etf_category_sina，返回全部上市 ETF）。

        返回字段：代码 symbol（6 位）、名称 name、市场前缀（sz/sh）。
        上市日期 list_date 接口不返回，保存为 NULL（由 planner 回退
        history.start_date）。
        """
        try:
            self.gate.acquire()
            import akshare as ak

            df = ak.fund_etf_category_sina(symbol="ETF基金")
        except Exception as exc:
            raise classify_eastmoney_exception(exc) from exc

        if df is None or df.empty:
            return ProviderBatch(records=[], source=SOURCE, raw_row_count=0)

        required = ["代码", "名称"]
        if not all(c in df.columns for c in required):
            raise EastmoneySchemaMismatchError(
                f"fund_etf_category_sina 缺失必需列: {required}, 实际: {list(df.columns)}"
            )

        records: list[EtfUniverseRecord] = []
        for _, row in df.iterrows():
            code_with_prefix = safe_str(row.get("代码", ""))
            name = safe_str(row.get("名称"))
            if not code_with_prefix:
                continue

            # 解析市场前缀：sz600000 / sh510300
            if len(code_with_prefix) >= 8:
                prefix = code_with_prefix[:2].lower()
                symbol = code_with_prefix[2:8]
            else:
                # 无前缀或格式异常：跳过
                continue

            if prefix not in ("sz", "sh"):
                continue

            exchange = "SZSE" if prefix == "sz" else "SSE"
            instrument_id = f"CN:ETF:{symbol}"

            records.append(
                EtfUniverseRecord(
                    ts_code=f"{symbol}.{'SZ' if prefix == 'sz' else 'SH'}",
                    symbol=symbol,
                    instrument_id=instrument_id,
                    name=name,
                    exchange=exchange,
                    list_date=None,  # 接口不返回，保存 NULL
                )
            )

        return ProviderBatch(
            records=records,
            source=SOURCE,
            raw_row_count=len(df),
        )

    def get_history_by_stock(
        self,
        dataset: str,
        instrument: Instrument,
        start_date: date,
        end_date: date,
    ) -> ProviderBatch[EtfDailyBar]:
        """按单只 ETF 区间拉取日线历史（fund_etf_hist_em，不复权）。

        dataset 仅支持 'etf_daily'（adj_factor 由 Tushare 提供）。
        """
        if dataset != "etf_daily":
            raise ValueError(f"EastmoneyEtfHistoryProvider 不支持数据集: {dataset}")

        symbol = instrument.symbol
        try:
            self.gate.acquire()
            import akshare as ak

            df = ak.fund_etf_hist_em(
                symbol=symbol,
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="",  # 不复权
            )
        except Exception as exc:
            raise classify_eastmoney_exception(exc) from exc

        if df is None or df.empty:
            # 空结果：停牌/新上市/接口无数据
            return ProviderBatch(records=[], source=SOURCE, raw_row_count=0)

        required = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]
        if not all(c in df.columns for c in required):
            raise EastmoneySchemaMismatchError(
                f"fund_etf_hist_em 缺失必需列: {required}, 实际: {list(df.columns)}"
            )

        records: list[EtfDailyBar] = []
        for _, row in df.iterrows():
            trade_date = safe_date(row.get("日期"))
            if trade_date is None:
                continue

            records.append(
                EtfDailyBar(
                    instrument_id=instrument.instrument_id,
                    ts_code=f"{symbol}.{'SZ' if instrument.exchange == 'SZSE' else 'SH'}",
                    trade_date=trade_date,
                    open=_safe_float(row.get("开盘")),
                    high=_safe_float(row.get("最高")),
                    low=_safe_float(row.get("最低")),
                    close=_safe_float(row.get("收盘")),
                    volume=_safe_int(row.get("成交量")),  # 手
                    amount=_safe_float(row.get("成交额")),  # 元（原始单位）
                    turnover_rate=_safe_float(row.get("换手率")),  # 百分比数值
                )
            )

        raw_count = len(df)
        truncation_risk = raw_count >= ETF_HIST_ROW_CAP

        return ProviderBatch(
            records=records,
            source=SOURCE,
            raw_row_count=raw_count,
            truncation_risk=truncation_risk,
        )
