"""Tushare 历史 ts_code 别名规范化（UNKNOWN_INSTRUMENT 修复）。

**为什么需要这一层**

Tushare 的历史事实接口返回的 ``ts_code`` 是该证券**当时的代码**，而
``stock_basic`` 只反映当前状态。证券代码发生变更后（深交所/上交所代码
调整、重组更名），两者永久性地不一致：

- 旧代码永远不出现在 ``stock_basic`` 的任何 ``exchange × list_status``
  分片中（本项目已取全部 15 片，见 tushare.py 的 ``STOCK_BASIC_EXCHANGES``
  / ``STOCK_BASIC_LIST_STATUSES``），刷新主档无法恢复；
- 于是 ``000022.SZ``（深赤湾A）这类历史代码在事实数据里存在、在主档里
  不存在，映射必然失败。

已经出现过的实例：``000022.SZ`` 于 2018-12-26 变更为 ``001872.SZ``
（深赤湾A → 招商港口），2010 年的 daily/adj_factor/daily_basic/moneyflow
仍以旧代码返回，导致首次回填卡在 2010-01-04 的 ``UNKNOWN_INSTRUMENT``。

**这一层做什么 / 不做什么**

只改写**已登记**的代码变更：一个明确的旧代码 → 该证券今天的规范代码。
它不是"宽松放行未知证券"，也不按 ``.SZ/.SH/.BJ`` 后缀或代码段猜测（那会
静默污染主档与事实表）。未登记的未知代码照旧抛 ``UNKNOWN_INSTRUMENT``。

不在此层处理的（有意为之）：

- ``stock_basic`` / ``stock_company`` / ``namechange``：主档是 instrument 的
  来源，改写主档会凭空造出第二条证券记录；
- 非别名的重复行：交给既有 ``DUPLICATE_KEY`` 校验，本层不做通用去重；
- 冲突时的"择一保留"：宁可阻止水位推进，也不静默丢弃不一致数据。

边界（技术方案 §31.3）：只处理 Tushare DataFrame，不越过 Provider 边界，
不碰 Repository / DB / 水位。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.providers.tushare_common import TushareError

logger = logging.getLogger(__name__)

# 已登记的 ts_code 代码变更：旧代码 -> 该证券当前的规范代码。
#
# 每条都必须能回答"这是同一法人主体的代码变更"（真实代码变更，而非两只
# 证券）。加入前须经权威来源核实（交易所公告 / Tushare 主档变更记录），
# 不得凭代码相近推断。
TUSHARE_TS_CODE_ALIASES: dict[str, str] = {
    # 深赤湾A 000022.SZ -> 招商港口 001872.SZ（2018-12-26 代码变更，资产重组）
    "000022.SZ": "001872.SZ",
}

# 与 tushare.py 的 _EMPTY_TEXT 同口径：这些文本一律视为"空值"
_EMPTY_TEXT = {"", "-", "--", "nan", "NaN", "None", "NaT"}

# 比较两行数值时允许的极小相对容差：吸收浮点序列化尾差（1 与 1.0、
# 12.3 与 12.300000000000001），不吸收真实差异。
_VALUE_TOLERANCE = 1e-9

# 冲突比较时跳过的身份列（ts_code 已被规范化改写，逐行比较无意义）
_EXCLUDED_COLUMNS = frozenset({"ts_code"})


class HistoricalAliasConflictError(TushareError):
    """同一规范代码的两行（旧代码与规范代码）业务字段不一致。

    宁可阻止该交易日提交（水位不推进），也不静默择一或丢弃——两种取值
    并存说明上游对同一证券同一交易日给出了互相矛盾的事实。
    """

    error_code = "ALIAS_CONFLICT"


def canonical_ts_code(ts_code: str) -> str:
    """旧代码 -> 规范代码；未登记的代码原样返回。"""
    return TUSHARE_TS_CODE_ALIASES.get(ts_code, ts_code)


def _is_missing(value) -> bool:
    """空值判定：None / 空文本 / NaN / NaT 统一为"缺"（与 _cell 同口径）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() in _EMPTY_TEXT
    try:
        result = value != value  # NaN / NaT 与自身不等
    except (TypeError, ValueError):
        return False
    if isinstance(result, bool):
        return result
    # numpy 标量（np.float64('nan') / np.bool_）的 != 返回 numpy 布尔而非
    # Python bool；数组则 bool() 抛 ValueError。两者都要正确处理。
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def _normalize_value(value):
    """比较用的归一化：空值 -> None，日期 -> YYYYMMDD 整数，数值 -> float。

    按数值语义合并 ``1`` 与 ``1.0``、``"12.3"`` 与 ``12.3``（Tushare 同一
    字段在不同批次可能分别以 int / float / str 返回）；日期统一为
    YYYYMMDD 整数，使 ``date(2010,1,4)`` 与 ``"20100104"`` 可比。
    """
    if _is_missing(value):
        return None
    if isinstance(value, bool):  # bool 是 int 子类，先排除
        return value
    if isinstance(value, datetime):  # datetime 是 date 子类，先判
        return int(value.strftime("%Y%m%d"))
    if isinstance(value, date):
        return int(value.strftime("%Y%m%d"))
    if isinstance(value, (int, float, Decimal)):
        try:
            return float(value)
        except (TypeError, ValueError, InvalidOperation, OverflowError):
            # 超大 int（10**400）转 float 抛 OverflowError；这类脏值无法参与
            # 数值比较，按"无法证明相等"处理（保守失败），但不能让裸异常
            # 穿透到 TushareError 体系之外。
            return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value.strip()
    return value


def _values_equal(left, right) -> bool:
    """两个单元格是否等价；任何"无法证明相等"的情况都判为不等（保守失败）。"""
    a = _normalize_value(left)
    b = _normalize_value(right)
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            first, second = float(a), float(b)
        except (OverflowError, TypeError, ValueError):
            # 超大整数无法转 float：退化为精确相等比较（仍不抛裸异常）。
            return a == b
        return abs(first - second) <= _VALUE_TOLERANCE * max(
            1.0, abs(first), abs(second)
        )
    try:
        outcome = a == b
    except (TypeError, ValueError):
        return False
    return outcome if isinstance(outcome, bool) else False


def _rows_equal(left: dict, right: dict) -> bool:
    """逐字段比较两行业务数据（排除 ts_code）——不用 DataFrame.equals：
    NaN/None 语义、dtype、字段顺序都可能制造假冲突。"""
    keys = {key for key in left if key not in _EXCLUDED_COLUMNS}
    keys |= {key for key in right if key not in _EXCLUDED_COLUMNS}
    return all(_values_equal(left.get(key), right.get(key)) for key in keys)


def _describe_differences(left: dict, right: dict, limit: int = 8) -> str:
    """列出冲突字段名与取值，用于诊断（Tushare 事实数据不含任何凭据）。"""
    fields = sorted(
        key
        for key in ({*left, *right} - _EXCLUDED_COLUMNS)
        if not _values_equal(left.get(key), right.get(key))
    )
    preview = ", ".join(
        f"{key}: {left.get(key)!r} != {right.get(key)!r}" for key in fields[:limit]
    )
    if len(fields) > limit:
        preview += f" … 共 {len(fields)} 个字段不一致"
    return preview or "(无可比较字段)"


def normalize_historical_aliases(df, *, endpoint: str, trade_date: date):
    """把历史事实 DataFrame 中的旧 ts_code 改写为其规范代码。

    处理顺序（§10）：

    1. 空 DataFrame 原样返回；
    2. 校验存在 ``ts_code`` 列（缺失即 SCHEMA_MISMATCH，与结构校验同口径）；
    3. **复制后再改**，不修改传入对象；
    4. 逐行计算规范代码；
    5. 同一规范代码出现多行时：
       - 一行旧代码、一行规范代码且业务字段一致 → 保留规范代码行，丢弃旧
         代码行（记 WARNING，含 ``action=drop_legacy`` 便于检索）；
       - 业务字段不一致 → ``ALIAS_CONFLICT``；
       - 多行都是同一旧代码（与别名无关的重复）→ 保持原样，交给
         ``DUPLICATE_KEY``。

    返回值是新 DataFrame；行数可能因"旧+新且一致"而减少，此时调用方在
    规范化**前**取 ``raw_row_count``，监控口径仍是上游真实返回行数。
    """
    if len(df) == 0:
        return df
    if "ts_code" not in set(df.columns):
        # 函数内导入避免模块级循环依赖（本模块被 tushare.py 导入）
        from app.providers.history.tushare import TushareHistorySchemaError

        raise TushareHistorySchemaError(
            f"{endpoint}[{trade_date:%Y%m%d}]: 返回缺少必需字段 ['ts_code']"
        )

    rows = df.to_dict("records")
    if not any(str(row.get("ts_code")) in TUSHARE_TS_CODE_ALIASES for row in rows):
        return df  # 绝大多数交易日无别名行：零改动返回

    out = df.copy(deep=True)
    canonical_values = [canonical_ts_code(str(code)) for code in out["ts_code"]]
    out["ts_code"] = canonical_values

    groups: dict[str, list[int]] = {}
    for position, code in enumerate(canonical_values):
        groups.setdefault(code, []).append(position)

    drop_positions: set[int] = set()
    for code, positions in groups.items():
        if len(positions) < 2:
            continue
        original_codes = {str(rows[position].get("ts_code")) for position in positions}
        canonical_positions = [
            position
            for position in positions
            if str(rows[position].get("ts_code")) == code
        ]
        if canonical_positions:
            # 字面规范代码行作参考；其余（旧代码行）与它逐一比较。
            # canonical_positions 内部的多行（同代码重复）不动，留给 DUPLICATE_KEY。
            reference = rows[canonical_positions[0]]
            droppable = [
                position for position in positions
                if position not in canonical_positions
            ]
        elif len(original_codes) >= 2:
            # 无字面规范行，但组内来自**不同**旧代码：两步别名链（如 A->C 与
            # B->C 都已登记，响应里同时出现 A 与 B）。仍须比较并合并，否则
            # 会退化成 DUPLICATE_KEY——既不检测真正的冲突，又要白等重试。
            reference = rows[positions[0]]
            droppable = positions[1:]
        else:
            # 组内全是同一个旧代码 -> 与别名无关的重复，交给 DUPLICATE_KEY
            continue
        for position in droppable:
            if not _rows_equal(reference, rows[position]):
                raise HistoricalAliasConflictError(
                    f"{endpoint}[{trade_date:%Y%m%d}]: 旧代码 "
                    f"{rows[position].get('ts_code')} 与 {code} 同属一只证券但字段冲突"
                    f"（{_describe_differences(reference, rows[position])}）"
                )
            drop_positions.add(position)
            logger.warning(
                "历史事实含旧代码且与新代码重复，丢弃旧代码行 "
                "action=drop_legacy endpoint=%s trade_date=%s legacy=%s canonical=%s",
                endpoint, trade_date, rows[position].get("ts_code"), code,
            )

    if not drop_positions:
        return out
    keep = [index for index in range(len(out)) if index not in drop_positions]
    return out.iloc[keep].reset_index(drop=True)
