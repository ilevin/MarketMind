"""腾讯行情 Provider（qt.gtimg.cn 批量接口）。

背景：本部署环境中 AKShare 的东财（*_em）与新浪接口均被远端断连（实测 2026-08-18），
A股股票改用 akshare 的腾讯通道 stock_zh_a_spot_tx；其余资产（A股ETF、港股、指数）
直接通过 qt.gtimg.cn 批量行情接口获取，数据结构与 akshare 的腾讯通道一致。

腾讯代码规则（仅存在于 Provider 内部）：
    CN STOCK:  6xxxxx -> sh，0/3xxxxx -> sz
    CN ETF:    5xxxxx -> sh，1xxxxx -> sz
    CN INDEX:  399xxx -> sz，其余（000xxx 等）-> sh
    HK STOCK/ETF: hk{symbol}
    HK INDEX:  r_hk{symbol}

行情串字段（~ 分隔，GBK 编码）：
    [1] 名称  [3] 最新价  [4] 昨收  [31] 数据时间  [32] 涨跌额  [33] 涨跌幅(%)
未知代码无对应 v_xxx 行 -> 名称识别失败。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from zoneinfo import ZoneInfo

from app.config import BUSINESS_TZ_NAME
from app.models.instrument import Instrument
from app.providers.base import Quote
from app.providers.safe_values import safe_float

logger = logging.getLogger(__name__)

_QT_URL = "https://qt.gtimg.cn/q="
_LINE_RE = re.compile(r'v_(?P<key>[^=]+)="(?P<value>[^"]*)"')
_BEIJING = ZoneInfo(BUSINESS_TZ_NAME)
_TIMEOUT = 10.0


def to_tencent_code(market: str, asset_type: str, symbol: str) -> str | None:
    """内部 instrument (market/asset_type/symbol) -> 腾讯代码。无法映射返回 None。"""
    if market == "HK":
        return f"r_hk{symbol}" if asset_type == "INDEX" else f"hk{symbol}"
    if market != "CN":
        return None
    if asset_type == "INDEX":
        return f"sz{symbol}" if symbol.startswith("399") else f"sh{symbol}"
    if asset_type == "ETF":
        return f"sh{symbol}" if symbol.startswith("5") else f"sz{symbol}"
    if asset_type == "STOCK":
        return f"sh{symbol}" if symbol.startswith("6") else f"sz{symbol}"
    return None


def _parse_cn_timestamp(text: str) -> datetime | None:
    """A股时间形如 20260818161449；港股形如 2026/08/18 16:08:08。均为北京时间。"""
    text = text.strip()
    if not text:
        return None
    for fmt in ("%Y%m%d%H%M%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_BEIJING)
        except ValueError:
            continue
    return None


class TencentQuoteClient:
    """qt.gtimg.cn 批量行情客户端：超时 + 单次重试。"""

    def __init__(self, timeout: float = _TIMEOUT):
        self.timeout = timeout

    def fetch(self, tencent_codes: list[str]) -> dict[str, list[str]]:
        """返回 {腾讯代码: 字段列表}；请求失败抛异常（由调用方处理）。"""
        if not tencent_codes:
            return {}
        last_exc: Exception | None = None
        for _attempt in range(2):  # 单次任务最多重试 1 次
            try:
                resp = httpx.get(_QT_URL + ",".join(tencent_codes), timeout=self.timeout)
                resp.raise_for_status()
                break
            except Exception as exc:
                last_exc = exc
        else:
            raise last_exc  # type: ignore[misc]

        text = resp.content.decode("gbk", errors="replace")
        result: dict[str, list[str]] = {}
        for match in _LINE_RE.finditer(text):
            fields = match.group("value").split("~")
            if len(fields) > 33:
                result[match.group("key")] = fields
        return result

    def fetch_one(self, tencent_code: str) -> list[str] | None:
        return self.fetch([tencent_code]).get(tencent_code)


def _find_timestamp_index(fields: list[str]) -> int:
    """定位数据时间字段。

    实测字段布局不同（2026-08-19）：
        A股/指数: [31]=时间 20260818161449, [32]=涨跌额, [33]=涨跌幅(%)
        港股:     [30]=时间 2026/08/18 16:08:08, [31]=涨跌额, [32]=涨跌幅(%)
    以时间字段的格式特征定位，再按相对偏移取涨跌字段。
    """
    for i, f in enumerate(fields[:40]):
        text = f.strip()
        if len(text) in (14, 19) and (text[:8].isdigit() or "/" in text):
            if _parse_cn_timestamp(text) is not None:
                return i
            continue
    return -1


def quote_from_fields(instrument_id: str, fields: list[str], delayed: bool) -> Quote:
    """腾讯字段 -> 内部 Quote。脏值经 safe_float 转 None。"""
    ts_idx = _find_timestamp_index(fields)
    source_timestamp = _parse_cn_timestamp(fields[ts_idx]) if ts_idx >= 0 else None
    # 涨跌幅 = 时间字段后第 2 个；无时间字段时回退固定位置（A股布局）
    chg_idx = ts_idx + 2 if ts_idx >= 0 else 33
    return Quote(
        instrument_id=instrument_id,
        price=safe_float(fields[3]),
        change_percent=safe_float(fields[chg_idx]) if chg_idx < len(fields) else None,
        volume_ratio=None,  # 腾讯批量接口不提供量比，A股股票走 akshare 通道
        previous_close=safe_float(fields[4]),
        source="tencent",
        source_timestamp=source_timestamp,
        delayed=delayed,
    )


class TencentQuoteProvider:
    """覆盖 A股ETF、港股股票/ETF、A股/港股指数（腾讯源）。"""

    def __init__(self, client: TencentQuoteClient | None = None):
        self.client = client or TencentQuoteClient()

    def get_quotes(self, instruments: list[Instrument]) -> dict[str, Quote]:
        wanted: dict[str, Instrument] = {}
        for inst in instruments:
            code = to_tencent_code(inst.market, inst.asset_type, inst.symbol)
            if code:
                wanted[code] = inst

        quotes: dict[str, Quote] = {}
        codes = list(wanted)
        # 每次请求最多 60 个代码，防止 URL 过长
        for i in range(0, len(codes), 60):
            batch = codes[i : i + 60]
            try:
                data = self.client.fetch(batch)
            except Exception:
                logger.exception("腾讯行情请求失败（%d 个代码）", len(batch))
                continue
            for code, fields in data.items():
                inst = wanted[code]
                quotes[inst.instrument_id] = quote_from_fields(
                    inst.instrument_id, fields, delayed=inst.market == "HK"
                )
        return quotes
