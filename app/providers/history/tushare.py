"""Tushare 历史行情数据 Provider（a-share-historical-data，技术方案 §31.3、§32~§35）。

职责（Provider 边界内，§35）：
- 调 Tushare——经 ``app.providers.tushare_common`` 共享 transport（统一超时、
  全局请求节奏、SDK 异常归一化）；
- 每个数据集显式声明 EXPECTED/REQUIRED 字段常量并显式传 ``fields``（§32，
  不依赖 Tushare 默认返回列）；
- 返回结构校验（必需列存在、身份键可解析、原始数值可转换）；
- YYYYMMDD 日期解析；ts_code → instrument_id 映射按 symbol 匹配主档
  instruments——不按代码首位推断交易所（§7.1）；
- 行数上限截断识别（6000/4500，§33）；
- normalize 为内部标准模型并返回 ``ProviderBatch``。

明确不负责（§31.3）：水位推进、重试编排、DB 事务、决定下一交易日、
吞掉异常返回空集合——历史连续性依赖异常向 Service 传播。

Tushare DataFrame 到本模块为止，不越过 Provider 边界。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import date, datetime

from app.config import AppConfig
from app.models.instrument import Instrument
from app.providers.base import (
    AdjFactor,
    DailyBar,
    DailyBasic,
    MoneyFlow,
    ProviderBatch,
    StockBasicRecord,
    StockCompanyRecord,
    StockNameChangeRecord,
)
from app.providers.history.tushare_aliases import (
    TUSHARE_TS_CODE_ALIASES,
    HistoricalAliasConflictError,
    canonical_ts_code,
    normalize_historical_aliases,
)
from app.providers.safe_values import safe_float
from app.providers.tushare_common import (
    DAILY_ROW_CAP,
    STOCK_BASIC_ROW_CAP,
    STOCK_COMPANY_ROW_CAP,
    TushareError,
    TushareTransport,
)

logger = logging.getLogger(__name__)

__all__ = [
    "TUSHARE_TS_CODE_ALIASES",
    "HistoricalAliasConflictError",
    "TushareHistoricalMarketDataProvider",
    "TushareHistorySchemaError",
    "UnknownInstrumentError",
    "canonical_ts_code",
    "normalize_historical_aliases",
]

SOURCE = "tushare"

# instrument_id 生成规则（§7.1）：CN:STOCK:<symbol>，与交易所无关
CN_STOCK_INSTRUMENT_PREFIX = "CN:STOCK:"

# exchange -> ts_code 后缀（§7.1：直接使用 Tushare 的 exchange，不按代码首位推断）
_EXCHANGE_SUFFIX = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}

# ---- 字段常量（§32：全部显式声明）----
# 每个常量为该数据集的 EXPECTED 字段（请求与解析口径）；
# *_REQUIRED 为身份字段——列缺失或值不可解析即 SCHEMA_MISMATCH。

STOCK_BASIC_FIELDS: tuple[str, ...] = (
    "ts_code", "symbol", "name", "area", "industry", "fullname", "enname",
    "cnspell", "market", "exchange", "curr_type", "list_status", "list_date",
    "delist_date", "is_hs", "act_name", "act_ent_type",
)
STOCK_BASIC_REQUIRED: tuple[str, ...] = ("ts_code", "symbol")

STOCK_COMPANY_FIELDS: tuple[str, ...] = (
    "ts_code", "com_name", "com_id", "exchange", "chairman", "manager",
    "secretary", "reg_capital", "setup_date", "province", "city",
    "introduction", "website", "email", "office", "employees",
    "main_business", "business_scope",
)
STOCK_COMPANY_REQUIRED: tuple[str, ...] = ("ts_code",)

NAMECHANGE_FIELDS: tuple[str, ...] = (
    "ts_code", "name", "start_date", "end_date", "ann_date", "change_reason",
)
NAMECHANGE_REQUIRED: tuple[str, ...] = ("ts_code",)

DAILY_FIELDS: tuple[str, ...] = (
    "ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
    "change", "pct_chg", "vol", "amount", "ah_vol", "ah_amount",
)
DAILY_REQUIRED: tuple[str, ...] = ("ts_code", "trade_date")

ADJ_FACTOR_FIELDS: tuple[str, ...] = ("ts_code", "trade_date", "adj_factor")
ADJ_FACTOR_REQUIRED: tuple[str, ...] = ("ts_code", "trade_date", "adj_factor")

DAILY_BASIC_FIELDS: tuple[str, ...] = (
    "ts_code", "trade_date", "close", "turnover_rate", "turnover_rate_f",
    "volume_ratio", "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio",
    "dv_ttm", "total_share", "float_share", "free_share", "total_mv",
    "circ_mv", "limit_status",
)
DAILY_BASIC_REQUIRED: tuple[str, ...] = ("ts_code", "trade_date")

MONEYFLOW_FIELDS: tuple[str, ...] = (
    "ts_code", "trade_date",
    "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount",
    "buy_md_vol", "buy_md_amount", "sell_md_vol", "sell_md_amount",
    "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount",
    "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
    "net_mf_vol", "net_mf_amount",
)
MONEYFLOW_REQUIRED: tuple[str, ...] = ("ts_code", "trade_date")

# 主档分片矩阵（§8.3 / §9.2）
STOCK_BASIC_EXCHANGES: tuple[str, ...] = ("SSE", "SZSE", "BSE")
STOCK_BASIC_LIST_STATUSES: tuple[str, ...] = ("L", "D", "P", "G", "UN")
STOCK_COMPANY_EXCHANGES: tuple[str, ...] = ("SSE", "SZSE", "BSE")


class TushareHistorySchemaError(TushareError):
    """返回结构不符合声明（必需列缺失 / 身份键不可解析，§35 边界校验）。"""

    error_code = "SCHEMA_MISMATCH"


class UnknownInstrumentError(TushareError):
    """事实数据出现主档之外的 ts_code，或主档缺 exchange 无法构造 ts_code（§35.1）。"""

    error_code = "UNKNOWN_INSTRUMENT"


# ---- 解析工具（Provider 边界内私有） ----

_EMPTY_TEXT = {"", "-", "--", "nan", "NaN", "None", "NaT"}


def _yyyymmdd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _fields_param(fields: tuple[str, ...]) -> str:
    return ",".join(fields)


def _cell(row: dict, field: str):
    """取单元格；None/NaN/常见空文本统一归 None。"""
    value = row.get(field)
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and value.strip() in _EMPTY_TEXT:
        return None
    return value


def _required_cell(row: dict, field: str, context: str) -> str:
    value = _cell(row, field)
    if value is None:
        raise TushareHistorySchemaError(f"{context}: 必需字段 {field} 为空")
    return str(value).strip()


def _opt_str(row: dict, field: str) -> str | None:
    value = _cell(row, field)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_date(value, field: str, context: str, *, required: bool = False) -> date | None:
    """Tushare YYYYMMDD 字符串 -> date；空值归 None（required 时抛结构错误）。"""
    if value is None:
        if required:
            raise TushareHistorySchemaError(f"{context}: 必需日期字段 {field} 为空")
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (ValueError, IndexError):
        raise TushareHistorySchemaError(
            f"{context}: 日期字段 {field} 不可解析: {text!r}"
        ) from None


def _safe_int(value) -> int | None:
    number = safe_float(value)
    return None if number is None else int(number)


def _symbol_of(ts_code: str) -> str:
    return ts_code.split(".", 1)[0]


def _cn_stock_instruments(
    instruments: list[Instrument],
) -> list[Instrument]:
    return [i for i in instruments if i.market == "CN" and i.asset_type == "STOCK"]


def _symbol_map(instruments: list[Instrument]) -> dict[str, Instrument]:
    return {inst.symbol: inst for inst in _cn_stock_instruments(instruments)}


def _instrument_for_ts_code(
    ts_code: str, symbol_map: dict[str, Instrument]
) -> Instrument:
    inst = symbol_map.get(_symbol_of(ts_code))
    if inst is None:
        raise UnknownInstrumentError(f"ts_code 无法映射至证券主档: {ts_code}")
    return inst


def _ts_code_of_instrument(inst: Instrument) -> str:
    """主档 Instrument -> ts_code（按 exchange 显式映射，不按代码首位推断）。"""
    suffix = _EXCHANGE_SUFFIX.get(inst.exchange or "")
    if suffix is None:
        raise UnknownInstrumentError(
            f"证券主档缺少可映射的 exchange，无法构造 ts_code: {inst.instrument_id}"
        )
    return f"{inst.symbol}.{suffix}"


def _check_dataframe(df, *, context: str) -> None:
    if df is None:
        raise TushareHistorySchemaError(f"{context}: 返回结构为 None")


def _require_columns(
    columns, required: tuple[str, ...], *, context: str
) -> None:
    missing = [name for name in required if name not in set(columns)]
    if missing:
        raise TushareHistorySchemaError(f"{context}: 返回缺少必需字段 {missing}")


RowBuilder = Callable[[dict, str], object]


def _normalize_rows(df, *, required: tuple[str, ...], context: str, build: RowBuilder) -> list:
    """逐行构建内部标准模型。

    0 行返回空列表——空结果是合法 ProviderBatch 返回，EMPTY_RESULT /
    WAITING_SOURCE 语义由 Service 判定（§34）；此时跳过列校验（上游空
    返回常不带列结构）。
    """
    if len(df) == 0:
        return []
    _require_columns(df.columns, required, context=context)
    return [build(row, context) for row in df.to_dict("records")]


# ---- 各数据集行构建器（字段显式逐一映射，§32） ----


def _build_stock_basic_row(row: dict, context: str) -> StockBasicRecord:
    ts_code = _required_cell(row, "ts_code", context)
    symbol = _required_cell(row, "symbol", context)
    return StockBasicRecord(
        ts_code=ts_code,
        symbol=symbol,
        instrument_id=CN_STOCK_INSTRUMENT_PREFIX + symbol,
        name=_opt_str(row, "name"),
        area=_opt_str(row, "area"),
        industry=_opt_str(row, "industry"),
        fullname=_opt_str(row, "fullname"),
        enname=_opt_str(row, "enname"),
        cnspell=_opt_str(row, "cnspell"),
        market=_opt_str(row, "market"),
        exchange=_opt_str(row, "exchange"),
        curr_type=_opt_str(row, "curr_type"),
        list_status=_opt_str(row, "list_status"),
        list_date=_parse_date(_cell(row, "list_date"), "list_date", context),
        delist_date=_parse_date(_cell(row, "delist_date"), "delist_date", context),
        is_hs=_opt_str(row, "is_hs"),
        act_name=_opt_str(row, "act_name"),
        act_ent_type=_opt_str(row, "act_ent_type"),
    )


def _build_stock_company_row(row: dict, context: str) -> StockCompanyRecord:
    ts_code = _required_cell(row, "ts_code", context)
    return StockCompanyRecord(
        ts_code=ts_code,
        instrument_id=CN_STOCK_INSTRUMENT_PREFIX + _symbol_of(ts_code),
        com_name=_opt_str(row, "com_name"),
        com_id=_opt_str(row, "com_id"),
        exchange=_opt_str(row, "exchange"),
        chairman=_opt_str(row, "chairman"),
        manager=_opt_str(row, "manager"),
        secretary=_opt_str(row, "secretary"),
        reg_capital=safe_float(_cell(row, "reg_capital")),
        setup_date=_parse_date(_cell(row, "setup_date"), "setup_date", context),
        province=_opt_str(row, "province"),
        city=_opt_str(row, "city"),
        introduction=_opt_str(row, "introduction"),
        website=_opt_str(row, "website"),
        email=_opt_str(row, "email"),
        office=_opt_str(row, "office"),
        employees=_safe_int(_cell(row, "employees")),
        main_business=_opt_str(row, "main_business"),
        business_scope=_opt_str(row, "business_scope"),
    )


def _build_namechange_row(row: dict, context: str) -> StockNameChangeRecord:
    ts_code = _required_cell(row, "ts_code", context)
    return StockNameChangeRecord(
        ts_code=ts_code,
        instrument_id=CN_STOCK_INSTRUMENT_PREFIX + _symbol_of(ts_code),
        name=_opt_str(row, "name"),
        start_date=_parse_date(_cell(row, "start_date"), "start_date", context),
        end_date=_parse_date(_cell(row, "end_date"), "end_date", context),
        ann_date=_parse_date(_cell(row, "ann_date"), "ann_date", context),
        change_reason=_opt_str(row, "change_reason"),
    )


def _build_daily_row(
    row: dict, symbol_map: dict[str, Instrument], context: str
) -> DailyBar:
    ts_code = _required_cell(row, "ts_code", context)
    inst = _instrument_for_ts_code(ts_code, symbol_map)
    return DailyBar(
        instrument_id=inst.instrument_id,
        ts_code=ts_code,
        trade_date=_parse_date(_cell(row, "trade_date"), "trade_date", context, required=True),
        open=safe_float(_cell(row, "open")),
        high=safe_float(_cell(row, "high")),
        low=safe_float(_cell(row, "low")),
        close=safe_float(_cell(row, "close")),
        pre_close=safe_float(_cell(row, "pre_close")),
        change=safe_float(_cell(row, "change")),
        pct_chg=safe_float(_cell(row, "pct_chg")),
        vol=safe_float(_cell(row, "vol")),
        amount=safe_float(_cell(row, "amount")),
        ah_vol=safe_float(_cell(row, "ah_vol")),
        ah_amount=safe_float(_cell(row, "ah_amount")),
    )


def _build_adj_factor_row(
    row: dict, symbol_map: dict[str, Instrument], context: str
) -> AdjFactor:
    ts_code = _required_cell(row, "ts_code", context)
    inst = _instrument_for_ts_code(ts_code, symbol_map)
    factor = safe_float(_cell(row, "adj_factor"))
    if factor is None:
        raise TushareHistorySchemaError(
            f"{context}: adj_factor 为空或不可解析（ts_code={ts_code}）"
        )
    return AdjFactor(
        instrument_id=inst.instrument_id,
        ts_code=ts_code,
        trade_date=_parse_date(_cell(row, "trade_date"), "trade_date", context, required=True),
        adj_factor=factor,
    )


def _build_daily_basic_row(
    row: dict, symbol_map: dict[str, Instrument], context: str
) -> DailyBasic:
    ts_code = _required_cell(row, "ts_code", context)
    inst = _instrument_for_ts_code(ts_code, symbol_map)
    return DailyBasic(
        instrument_id=inst.instrument_id,
        ts_code=ts_code,
        trade_date=_parse_date(_cell(row, "trade_date"), "trade_date", context, required=True),
        close=safe_float(_cell(row, "close")),
        turnover_rate=safe_float(_cell(row, "turnover_rate")),
        turnover_rate_f=safe_float(_cell(row, "turnover_rate_f")),
        volume_ratio=safe_float(_cell(row, "volume_ratio")),
        pe=safe_float(_cell(row, "pe")),
        pe_ttm=safe_float(_cell(row, "pe_ttm")),
        pb=safe_float(_cell(row, "pb")),
        ps=safe_float(_cell(row, "ps")),
        ps_ttm=safe_float(_cell(row, "ps_ttm")),
        dv_ratio=safe_float(_cell(row, "dv_ratio")),
        dv_ttm=safe_float(_cell(row, "dv_ttm")),
        total_share=safe_float(_cell(row, "total_share")),
        float_share=safe_float(_cell(row, "float_share")),
        free_share=safe_float(_cell(row, "free_share")),
        total_mv=safe_float(_cell(row, "total_mv")),
        circ_mv=safe_float(_cell(row, "circ_mv")),
        limit_status=_safe_int(_cell(row, "limit_status")),
    )


def _build_moneyflow_row(
    row: dict, symbol_map: dict[str, Instrument], context: str
) -> MoneyFlow:
    ts_code = _required_cell(row, "ts_code", context)
    inst = _instrument_for_ts_code(ts_code, symbol_map)
    return MoneyFlow(
        instrument_id=inst.instrument_id,
        ts_code=ts_code,
        trade_date=_parse_date(_cell(row, "trade_date"), "trade_date", context, required=True),
        buy_sm_vol=_safe_int(_cell(row, "buy_sm_vol")),
        buy_sm_amount=safe_float(_cell(row, "buy_sm_amount")),
        sell_sm_vol=_safe_int(_cell(row, "sell_sm_vol")),
        sell_sm_amount=safe_float(_cell(row, "sell_sm_amount")),
        buy_md_vol=_safe_int(_cell(row, "buy_md_vol")),
        buy_md_amount=safe_float(_cell(row, "buy_md_amount")),
        sell_md_vol=_safe_int(_cell(row, "sell_md_vol")),
        sell_md_amount=safe_float(_cell(row, "sell_md_amount")),
        buy_lg_vol=_safe_int(_cell(row, "buy_lg_vol")),
        buy_lg_amount=safe_float(_cell(row, "buy_lg_amount")),
        sell_lg_vol=_safe_int(_cell(row, "sell_lg_vol")),
        sell_lg_amount=safe_float(_cell(row, "sell_lg_amount")),
        buy_elg_vol=_safe_int(_cell(row, "buy_elg_vol")),
        buy_elg_amount=safe_float(_cell(row, "buy_elg_amount")),
        sell_elg_vol=_safe_int(_cell(row, "sell_elg_vol")),
        sell_elg_amount=safe_float(_cell(row, "sell_elg_amount")),
        net_mf_vol=_safe_int(_cell(row, "net_mf_vol")),
        net_mf_amount=safe_float(_cell(row, "net_mf_amount")),
    )


class TushareHistoricalMarketDataProvider:
    """``HistoricalMarketDataProvider`` 的 Tushare 实现（§31.3）。"""

    def __init__(self, config: AppConfig, transport: TushareTransport | None = None):
        self.config = config
        if transport is None:
            transport = TushareTransport(config)
        self._transport = transport

    # ---- 主档数据集 ----

    def get_stock_basic(self) -> ProviderBatch[StockBasicRecord]:
        """全量证券主档：exchange × list_status 共 15 分片（§8.3）。

        空分片允许；非空分片命中 6000 行上限则整批 ``truncation_risk=True``
        （主档无 fallback 路径，Service 校验拒绝、不提交为完整快照）。
        """
        records: list[StockBasicRecord] = []
        raw_rows = 0
        truncation = False
        for exchange in STOCK_BASIC_EXCHANGES:
            for list_status in STOCK_BASIC_LIST_STATUSES:
                context = f"stock_basic[{exchange}/{list_status}]"
                df = self._transport.call(
                    "stock_basic",
                    exchange=exchange,
                    list_status=list_status,
                    fields=_fields_param(STOCK_BASIC_FIELDS),
                )
                _check_dataframe(df, context=context)
                rows = len(df)
                raw_rows += rows
                if rows == 0:
                    continue  # 空分片允许（如 G/UN 状态）
                if rows >= STOCK_BASIC_ROW_CAP:
                    logger.warning("%s 返回 %d 行命中接口上限，标记截断风险", context, rows)
                    truncation = True
                records.extend(
                    _normalize_rows(
                        df,
                        required=STOCK_BASIC_REQUIRED,
                        context=context,
                        build=_build_stock_basic_row,
                    )
                )
        return ProviderBatch(
            records=records,
            source=SOURCE,
            raw_row_count=raw_rows,
            truncation_risk=truncation,
        )

    def get_stock_company(self) -> ProviderBatch[StockCompanyRecord]:
        """公司资料：按 exchange 分 3 片规避 4500 行上限（§9.2）。"""
        records: list[StockCompanyRecord] = []
        raw_rows = 0
        truncation = False
        for exchange in STOCK_COMPANY_EXCHANGES:
            context = f"stock_company[{exchange}]"
            df = self._transport.call(
                "stock_company",
                exchange=exchange,
                fields=_fields_param(STOCK_COMPANY_FIELDS),
            )
            _check_dataframe(df, context=context)
            rows = len(df)
            raw_rows += rows
            if rows == 0:
                continue
            if rows >= STOCK_COMPANY_ROW_CAP:
                logger.warning("%s 返回 %d 行命中接口上限，标记截断风险", context, rows)
                truncation = True
            records.extend(
                _normalize_rows(
                    df,
                    required=STOCK_COMPANY_REQUIRED,
                    context=context,
                    build=_build_stock_company_row,
                )
            )
        return ProviderBatch(
            records=records,
            source=SOURCE,
            raw_row_count=raw_rows,
            truncation_risk=truncation,
        )

    def get_name_changes(
        self,
        *,
        ts_code: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ProviderBatch[StockNameChangeRecord]:
        """历史名称事件（§10）：bootstrap 逐只（ts_code）或增量按日期窗口。"""
        params: dict[str, str] = {"fields": _fields_param(NAMECHANGE_FIELDS)}
        if ts_code is not None:
            params["ts_code"] = ts_code
        if start_date is not None:
            params["start_date"] = _yyyymmdd(start_date)
        if end_date is not None:
            params["end_date"] = _yyyymmdd(end_date)
        context = "namechange" + (f"[{ts_code}]" if ts_code else "")
        df = self._transport.call("namechange", **params)
        _check_dataframe(df, context=context)
        records = _normalize_rows(
            df, required=NAMECHANGE_REQUIRED, context=context, build=_build_namechange_row
        )
        return ProviderBatch(
            records=records, source=SOURCE, raw_row_count=len(df)
        )

    # ---- 日级数据集 ----

    def get_daily(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBar]:
        """按交易日全市场请求（§33.1）；达 6000 行标记截断风险交 Service fallback。"""
        return self._fetch_day_level(
            endpoint="daily",
            fields=DAILY_FIELDS,
            required=DAILY_REQUIRED,
            trade_date=trade_date,
            instruments=instruments,
            build=_build_daily_row,
        )

    def get_daily_for_instruments(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBar]:
        """截断 fallback：逐证券细粒度请求（§33.2——未验证多代码参数前保守逐只）。"""
        return self._fetch_day_level_per_instrument(
            endpoint="daily",
            fields=DAILY_FIELDS,
            required=DAILY_REQUIRED,
            trade_date=trade_date,
            instruments=instruments,
            build=_build_daily_row,
        )

    def get_adj_factors(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[AdjFactor]:
        """复权因子：文档无明确 6000 行上限，按同一阈值保守防护（§33.5）。"""
        return self._fetch_day_level(
            endpoint="adj_factor",
            fields=ADJ_FACTOR_FIELDS,
            required=ADJ_FACTOR_REQUIRED,
            trade_date=trade_date,
            instruments=instruments,
            build=_build_adj_factor_row,
        )

    def get_daily_basic(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[DailyBasic]:
        return self._fetch_day_level(
            endpoint="daily_basic",
            fields=DAILY_BASIC_FIELDS,
            required=DAILY_BASIC_REQUIRED,
            trade_date=trade_date,
            instruments=instruments,
            build=_build_daily_basic_row,
        )

    def get_daily_basic_for_instruments(
        self,
        trade_date: date,
        instruments: list[Instrument],
        *,
        missing_ts_codes: list[str] | None = None,
    ) -> ProviderBatch[DailyBasic]:
        """截断 fallback：只对缺失证券逐只补齐（§33.2）。

        daily_basic **不支持多代码参数**（逗号分隔的多个 ts_code 会静默返回
        空结果，已用真实 Token 在线验证），因此不得复用其他接口的 multi-code
        fallback；只做逐只查询。``missing_ts_codes`` 由 Service 按
        ``候选集 - 已返回代码`` 给出；``None`` 表示全部证券逐只查询。

        每个缺失证券都必须得到"有记录"或"明确空结果"（空值同样合法：停牌
        等自然缺失）；任一请求异常直接向上抛，由 Service 判定该交易日不
        COMPLETE、水位不推进。
        """
        if missing_ts_codes is None:
            targets = _cn_stock_instruments(instruments)
        else:
            by_ts_code = {
                _ts_code_of_instrument(inst): inst
                for inst in _cn_stock_instruments(instruments)
            }
            # 主档缺失的 ts_code 不静默丢弃：_ts_code_of_instrument 已在构造
            # 映射时抛 UNKNOWN_INSTRUMENT，此处缺失说明候选集与主档不一致。
            targets = []
            for ts_code in missing_ts_codes:
                inst = by_ts_code.get(ts_code)
                if inst is None:
                    raise UnknownInstrumentError(
                        f"截断 fallback 候选证券不在主档: {ts_code}"
                    )
                targets.append(inst)
        return self._fetch_day_level_per_instrument(
            endpoint="daily_basic",
            fields=DAILY_BASIC_FIELDS,
            required=DAILY_BASIC_REQUIRED,
            trade_date=trade_date,
            instruments=targets,
            build=_build_daily_basic_row,
        )

    def get_moneyflow(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[MoneyFlow]:
        return self._fetch_day_level(
            endpoint="moneyflow",
            fields=MONEYFLOW_FIELDS,
            required=MONEYFLOW_REQUIRED,
            trade_date=trade_date,
            instruments=instruments,
            build=_build_moneyflow_row,
        )

    def get_moneyflow_for_instruments(
        self, trade_date: date, instruments: list[Instrument]
    ) -> ProviderBatch[MoneyFlow]:
        return self._fetch_day_level_per_instrument(
            endpoint="moneyflow",
            fields=MONEYFLOW_FIELDS,
            required=MONEYFLOW_REQUIRED,
            trade_date=trade_date,
            instruments=instruments,
            build=_build_moneyflow_row,
        )

    # ---- 内部：日级请求两条路径 ----

    def _fetch_day_level(
        self,
        *,
        endpoint: str,
        fields: tuple[str, ...],
        required: tuple[str, ...],
        trade_date: date,
        instruments: list[Instrument],
        build: Callable[[dict, dict[str, Instrument], str], object],
    ) -> ProviderBatch:
        """按 trade_date 全市场请求（主路径）。

        结构检查之后、映射之前把已登记的历史 ts_code 改写为规范代码：上游
        按"当时的代码"返回历史事实，而主档只有今天的代码，不改写则必然
        UNKNOWN_INSTRUMENT（见 ``tushare_aliases``）。``raw_rows`` 在改写前
        取值，监控口径仍是上游真实返回行数。
        """
        context = f"{endpoint}[{_yyyymmdd(trade_date)}]"
        df = self._transport.call(
            endpoint,
            trade_date=_yyyymmdd(trade_date),
            fields=_fields_param(fields),
        )
        _check_dataframe(df, context=context)
        raw_rows = len(df)
        df = normalize_historical_aliases(
            df, endpoint=endpoint, trade_date=trade_date
        )
        symbol_map = _symbol_map(instruments)

        def _build(row: dict, ctx: str):
            return build(row, symbol_map, ctx)

        records = _normalize_rows(df, required=required, context=context, build=_build)
        truncation = raw_rows >= DAILY_ROW_CAP
        if truncation:
            logger.warning("%s 返回 %d 行达到接口上限，标记截断风险", context, raw_rows)
        return ProviderBatch(
            records=records,
            source=SOURCE,
            raw_row_count=raw_rows,
            truncation_risk=truncation,
        )

    def _fetch_day_level_per_instrument(
        self,
        *,
        endpoint: str,
        fields: tuple[str, ...],
        required: tuple[str, ...],
        trade_date: date,
        instruments: list[Instrument],
        build: Callable[[dict, dict[str, Instrument], str], object],
    ) -> ProviderBatch:
        """截断 fallback：逐证券请求后合并（§33.2）。

        单证券单日至多一行，不存在截断；重复键检测属 Domain 校验
        （DUPLICATE_KEY，Validator 职责），本层不做去重。

        与主路径共用同一别名规范化：逐只请求用的是证券**今天的**代码，但
        上游对历史日期仍可能回旧代码，不处理则会在补齐路径重新出现身份
        不一致（主路径已修好、fallback 又破功）。

        已知边界（不在本层解决）：主档同时含新旧两码时，别名改写会把响应
        代码换成被请求子集之外的规范代码，与主路径已产出的记录形成重复键
        （DUPLICATE_KEY）。根因是候选集与 Provider 输出用了两套身份口径，
        须在 Service 层统一（见 openspec design 的 Known Limitation）。
        """
        context = f"{endpoint}[{_yyyymmdd(trade_date)}] per-instrument"
        symbol_map = _symbol_map(instruments)
        records: list = []
        raw_rows = 0
        for inst in sorted(_cn_stock_instruments(instruments), key=lambda i: i.symbol):
            ts_code = _ts_code_of_instrument(inst)
            df = self._transport.call(
                endpoint,
                ts_code=ts_code,
                trade_date=_yyyymmdd(trade_date),
                fields=_fields_param(fields),
            )
            _check_dataframe(df, context=f"{context} {ts_code}")
            raw_rows += len(df)
            df = normalize_historical_aliases(
                df, endpoint=endpoint, trade_date=trade_date
            )
            records.extend(
                _normalize_rows(
                    df,
                    required=required,
                    context=f"{context} {ts_code}",
                    build=lambda row, ctx, _map=symbol_map: build(row, _map, ctx),
                )
            )
        return ProviderBatch(
            records=records, source=SOURCE, raw_row_count=raw_rows
        )
