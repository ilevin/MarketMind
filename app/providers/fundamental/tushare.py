"""Tushare 估值 Provider（A股股票 PE/PB/股息率，PRD 第 14 节）。

接口：tushare pro daily_basic
    输入 ts_code（如 600519.SH）+ trade_date，输出 pe_ttm / pb / dv_ttm 等列。
    dv_ttm 为股息率(TTM)，单位 %。

Token 从 config.yaml -> tushare.token 读取（配置对象注入），
缺失/失败降级返回空结果并记日志，绝不让主进程崩溃。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from app.config import AppConfig
from app.models.instrument import Instrument
from app.providers.base import Fundamental
from app.providers.safe_values import safe_float

logger = logging.getLogger(__name__)

SOURCE = "tushare"


def to_ts_code(symbol: str) -> str:
    """A股代码 -> Tushare ts_code（6xxxxx -> SH，其余 -> SZ）。"""
    return f"{symbol}.SH" if symbol.startswith("6") else f"{symbol}.SZ"


class TushareFundamentalProvider:
    def __init__(self, config: AppConfig):
        self.config = config

    def _pro(self):
        if not self.config.has_tushare_token:
            raise RuntimeError("Tushare Token 未配置（config.yaml -> tushare.token）")
        import tushare as ts

        # v0.03：超时由配置注入（技术方案 §27），替换 SDK 默认 30 秒
        return ts.pro_api(
            self.config.tushare.token,
            timeout=self.config.providers.timeout.tushare,
        )

    def get_fundamentals(
        self, instruments: list[Instrument], trade_date: date | None = None
    ) -> dict[str, Fundamental]:
        """仅处理 CN/STOCK；ETF 与指数不请求、不写估值。"""
        stocks = [
            inst for inst in instruments if inst.market == "CN" and inst.asset_type == "STOCK"
        ]
        if not stocks:
            return {}
        if not self.config.has_tushare_token:
            logger.warning("Tushare Token 未配置，跳过估值获取（%d 只股票）", len(stocks))
            return {}

        try:
            df = self._fetch(stocks, trade_date)
        except Exception as exc:
            logger.error("Tushare daily_basic 请求失败: %s", exc)
            return {}

        result: dict[str, Fundamental] = {}
        by_symbol = {inst.symbol: inst for inst in stocks}
        for row in df.itertuples(index=False):
            symbol = str(getattr(row, "ts_code", "")).split(".")[0]
            inst = by_symbol.get(symbol)
            if inst is None:
                continue
            trade_date_str = str(getattr(row, "trade_date", ""))
            try:
                day = date(int(trade_date_str[:4]), int(trade_date_str[4:6]), int(trade_date_str[6:8]))
            except (ValueError, IndexError):
                day = trade_date or date.today()
            existing = result.get(inst.instrument_id)
            if existing is not None and existing.trade_date >= day:
                continue  # 同一股票多行时保留最新一期
            result[inst.instrument_id] = Fundamental(
                instrument_id=inst.instrument_id,
                trade_date=day,
                pe_ttm=safe_float(getattr(row, "pe_ttm", None)),
                pb=safe_float(getattr(row, "pb", None)),
                dividend_yield_ttm=safe_float(getattr(row, "dv_ttm", None)),
                source=SOURCE,
            )
        return result

    def _fetch(self, stocks: list[Instrument], trade_date: date | None):
        pro = self._pro()
        if trade_date is not None:
            return pro.daily_basic(
                trade_date=trade_date.strftime("%Y%m%d"),
                fields="ts_code,trade_date,pe_ttm,pb,dv_ttm",
            )
        # 未指定日期：逐只按 ts_code 查询最近估值（限定窗口；不带日期会返回全部历史）
        end = date.today()
        start = end - timedelta(days=400)
        frames = []
        for inst in stocks:
            frames.append(
                pro.daily_basic(
                    ts_code=to_ts_code(inst.symbol),
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    fields="ts_code,trade_date,pe_ttm,pb,dv_ttm",
                )
            )
        import pandas as pd

        return pd.concat(frames, ignore_index=True) if frames else None
