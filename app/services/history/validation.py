"""历史数据 Domain 校验（a-share-historical-data，技术方案 §35~§39）。

两层校验（§35）中的第二层：对 ``ProviderBatch``（已是内部标准模型）检查
通用规则与各数据集专项规则；Provider 边界校验（结构/解析）已在具体
Tushare Provider 内完成。校验失败抛带 ``error_code`` 的异常（§51.3），
由 Service 决定重试/等待/失败——本层不做任何修复。

空结果语义（§34）：历史日期 0 行抛 ``EMPTY_RESULT``；当前最新交易日的
WAITING_SOURCE 判定属 Service（结合 AvailabilityPolicy），调用方对
``allow_empty=True`` 的批次跳过非零行检查。
"""

from __future__ import annotations

import dataclasses
import math
from typing import Callable

from app.models.history_sync import DatasetName
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

__all__ = [
    "HistoryValidationError",
    "EmptyResultError",
    "TruncationRiskError",
    "DuplicateKeyError",
    "TradeDateMismatchError",
    "HistoryUnknownInstrumentError",
    "InvalidValueError",
    "LIMIT_STATUS_MIN",
    "LIMIT_STATUS_MAX",
    "validate_batch",
]

# daily_basic.limit_status 的合法范围（Tushare 收盘涨跌状态，NULL 合法）
LIMIT_STATUS_MIN = 0
LIMIT_STATUS_MAX = 6


class HistoryValidationError(Exception):
    """Domain 校验失败；error_code 为标准化错误码（§51.3）。"""

    error_code = "INVALID_VALUE"

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class EmptyResultError(HistoryValidationError):
    error_code = "EMPTY_RESULT"


class TruncationRiskError(HistoryValidationError):
    error_code = "TRUNCATION_RISK"


class DuplicateKeyError(HistoryValidationError):
    error_code = "DUPLICATE_KEY"


class TradeDateMismatchError(HistoryValidationError):
    error_code = "TRADE_DATE_MISMATCH"


class HistoryUnknownInstrumentError(HistoryValidationError):
    """batch 内 instrument_id 不在证券主档（§35.1，禁止占位证券）。"""

    error_code = "UNKNOWN_INSTRUMENT"


class InvalidValueError(HistoryValidationError):
    error_code = "INVALID_VALUE"


def _key(dataset: DatasetName | str) -> str:
    return dataset.value if isinstance(dataset, DatasetName) else str(dataset)


def _check_finite(record, context: str) -> None:
    """NaN/Inf 不能安全落库（§35.7；Provider 的 safe_float 已滤，此处双保险）。"""
    for f in dataclasses.fields(record):
        value = getattr(record, f.name)
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidValueError(f"{context}: 字段 {f.name} 为 NaN/Inf")


def _check_common(
    batch: ProviderBatch,
    *,
    context: str,
    allow_empty: bool,
    known_instrument_ids: set[str] | None,
    expected_trade_date=None,
    require_trade_date: bool = False,
    duplicate_key_fields: Callable | None = None,
) -> None:
    """通用 Domain 校验（§35）：截断、非零行、键非空、日期一致、唯一、可映射。"""
    if batch.truncation_risk:
        raise TruncationRiskError(f"{context}: 返回命中接口行数上限，不能提交")
    if not allow_empty and not batch.records:
        raise EmptyResultError(f"{context}: 返回 0 行")
    seen: set = set()
    for record in batch.records:
        if not record.instrument_id:
            raise InvalidValueError(f"{context}: instrument_id 为空")
        if require_trade_date:
            if record.trade_date is None:
                raise InvalidValueError(f"{context}: trade_date 为空")
            if expected_trade_date is not None and record.trade_date != expected_trade_date:
                raise TradeDateMismatchError(
                    f"{context}: 记录日期 {record.trade_date} 与请求日期 "
                    f"{expected_trade_date} 不一致"
                )
        key = duplicate_key_fields(record) if duplicate_key_fields else (
            (record.instrument_id, record.trade_date)
        )
        if key in seen:
            raise DuplicateKeyError(f"{context}: 唯一键重复 {key}")
        seen.add(key)
        if (
            known_instrument_ids is not None
            and record.instrument_id not in known_instrument_ids
        ):
            raise HistoryUnknownInstrumentError(
                f"{context}: instrument_id 不在证券主档 {record.instrument_id}"
            )
        _check_finite(record, context)


def _non_negative(value, field: str, context: str) -> None:
    if value is not None and value < 0:
        raise InvalidValueError(f"{context}: 字段 {field} 为负数 {value}")


# ---- 日级专项（§36~§39） ----


def _validate_daily(
    batch: ProviderBatch[DailyBar],
    *,
    trade_date,
    known_instrument_ids,
    allow_empty,
) -> None:
    context = f"daily[{trade_date}]"
    _check_common(
        batch,
        context=context,
        allow_empty=allow_empty,
        known_instrument_ids=known_instrument_ids,
        expected_trade_date=trade_date,
        require_trade_date=True,
    )
    for record in batch.records:  # §36 专项
        _non_negative(record.open, "open", context)
        _non_negative(record.high, "high", context)
        _non_negative(record.low, "low", context)
        _non_negative(record.close, "close", context)
        _non_negative(record.vol, "vol", context)
        _non_negative(record.amount, "amount", context)
        # ah_* 允许 NULL（历史时期合法），有值时同非负口径
        _non_negative(record.ah_vol, "ah_vol", context)
        _non_negative(record.ah_amount, "ah_amount", context)
        if all(
            value is not None and value > 0
            for value in (record.open, record.high, record.low, record.close)
        ):
            if record.high < record.open or record.high < record.close or record.high < record.low:
                raise InvalidValueError(
                    f"{context}: high({record.high}) 低于 OHLC 其余值 "
                    f"(open={record.open}, close={record.close}, low={record.low})"
                )
            if record.low > record.open or record.low > record.close:
                raise InvalidValueError(
                    f"{context}: low({record.low}) 高于 open/close "
                    f"(open={record.open}, close={record.close})"
                )


def _validate_adj_factor(
    batch: ProviderBatch[AdjFactor],
    *,
    trade_date,
    known_instrument_ids,
    allow_empty,
) -> None:
    context = f"adj_factor[{trade_date}]"
    _check_common(
        batch,
        context=context,
        allow_empty=allow_empty,
        known_instrument_ids=known_instrument_ids,
        expected_trade_date=trade_date,
        require_trade_date=True,
    )
    for record in batch.records:  # §37：必须 > 0
        if record.adj_factor <= 0:
            raise InvalidValueError(
                f"{context}: adj_factor 必须 > 0，得到 {record.adj_factor}"
            )


def _validate_daily_basic(
    batch: ProviderBatch[DailyBasic],
    *,
    trade_date,
    known_instrument_ids,
    allow_empty,
) -> None:
    context = f"daily_basic[{trade_date}]"
    _check_common(
        batch,
        context=context,
        allow_empty=allow_empty,
        known_instrument_ids=known_instrument_ids,
        expected_trade_date=trade_date,
        require_trade_date=True,
    )
    for record in batch.records:  # §38：股本/市值非负；估值允许 NULL
        for field in (
            "total_share", "float_share", "free_share", "total_mv", "circ_mv",
        ):
            _non_negative(getattr(record, field), field, context)
        # limit_status：NULL 或 Tushare 定义的 0~6 收盘涨跌状态
        # （《历史行情数据产品设计与数据说明》§7.3）。越界几乎只可能是
        # 字段串位/解析错误，属于必须拦截的 schema 级异常。
        if record.limit_status is not None and not (
            LIMIT_STATUS_MIN <= record.limit_status <= LIMIT_STATUS_MAX
        ):
            raise InvalidValueError(
                f"{context}: limit_status 超出枚举范围 "
                f"{record.limit_status}（允许 {LIMIT_STATUS_MIN}~{LIMIT_STATUS_MAX} 或 NULL）"
            )


def _validate_moneyflow(
    batch: ProviderBatch[MoneyFlow],
    *,
    trade_date,
    known_instrument_ids,
    allow_empty,
) -> None:
    context = f"moneyflow[{trade_date}]"
    _check_common(
        batch,
        context=context,
        allow_empty=allow_empty,
        known_instrument_ids=known_instrument_ids,
        expected_trade_date=trade_date,
        require_trade_date=True,
    )
    # §39：buy_*/sell_* 非 NULL 时非负；net_mf_* 允许负；不自行计算替代官方值
    non_negative_fields = (
        "buy_sm_vol", "buy_sm_amount", "sell_sm_vol", "sell_sm_amount",
        "buy_md_vol", "buy_md_amount", "sell_md_vol", "sell_md_amount",
        "buy_lg_vol", "buy_lg_amount", "sell_lg_vol", "sell_lg_amount",
        "buy_elg_vol", "buy_elg_amount", "sell_elg_vol", "sell_elg_amount",
    )
    for record in batch.records:
        for field in non_negative_fields:
            _non_negative(getattr(record, field), field, context)


# ---- 主档专项 ----


def _validate_stock_basic(
    batch: ProviderBatch[StockBasicRecord],
    *,
    trade_date,
    known_instrument_ids,
    allow_empty,
) -> None:
    context = "stock_basic"
    _check_common(
        batch,
        context=context,
        allow_empty=allow_empty,
        known_instrument_ids=None,  # 主档是 instrument 的来源，不做已知集合检查
        duplicate_key_fields=lambda r: r.instrument_id,
    )
    for record in batch.records:
        if not record.symbol:
            raise InvalidValueError(f"{context}: symbol 为空")
        if record.instrument_id != f"CN:STOCK:{record.symbol}":
            raise InvalidValueError(
                f"{context}: instrument_id 与 symbol 映射不一致 "
                f"({record.instrument_id} vs {record.symbol})"
            )


def _validate_stock_company(
    batch: ProviderBatch[StockCompanyRecord],
    *,
    trade_date,
    known_instrument_ids,
    allow_empty,
) -> None:
    context = "stock_company"
    _check_common(
        batch,
        context=context,
        allow_empty=allow_empty,
        known_instrument_ids=None,
        duplicate_key_fields=lambda r: r.instrument_id,
    )


def _validate_namechange(
    batch: ProviderBatch[StockNameChangeRecord],
    *,
    trade_date,
    known_instrument_ids,
    allow_empty,
) -> None:
    context = "namechange"
    _check_common(
        batch,
        context=context,
        allow_empty=allow_empty,  # 单证券无改名历史合法（Service 决定）
        known_instrument_ids=known_instrument_ids,
        duplicate_key_fields=lambda r: (r.instrument_id, r.name, r.start_date),
    )


_HANDLERS: dict[str, Callable] = {
    "daily": _validate_daily,
    "adj_factor": _validate_adj_factor,
    "daily_basic": _validate_daily_basic,
    "moneyflow": _validate_moneyflow,
    "stock_basic": _validate_stock_basic,
    "stock_company": _validate_stock_company,
    "namechange": _validate_namechange,
}


def validate_batch(
    dataset: DatasetName | str,
    batch: ProviderBatch,
    *,
    trade_date=None,
    known_instrument_ids: set[str] | None = None,
    allow_empty: bool = False,
) -> None:
    """Domain 校验入口：失败抛 HistoryValidationError 子类，通过返回 None。"""
    handler = _HANDLERS.get(_key(dataset))
    if handler is None:
        raise ValueError(f"无校验规则的数据集: {dataset}")
    handler(
        batch,
        trade_date=trade_date,
        known_instrument_ids=known_instrument_ids,
        allow_empty=allow_empty,
    )
