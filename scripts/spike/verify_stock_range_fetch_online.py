"""在线验证：四数据集「按股票区间拉取」的真实返回行为。

对应 per-stock-history-sync tasks 1.1（v0.4.0 design.md Open Questions 1/2）——
**调度改造合并前必须跑一次**：

    不要假设 ``pro.daily(ts_code=…, start_date=…, end_date=…)`` 的区间形态
    在 2026 年的行为与按 trade_date 形态完全相同。

本脚本回答的问题（全部只读，不写任何数据库、不改任何文件）：

1. 正常股 16 年同步区间（000001.SZ，2010-01-04 → 最近已完成交易日）的
   行数、字段完整性、行序（trade_date 升/降）、停牌日是否天然缺行；
2. 旧代码股（000022.SZ → 001872.SZ 别名）区间请求返回旧码、新码还是都有；
3. 退市股末段区间（在线从 stock_basic list_status='D' 选样）是否有数据、
   退市日后请求是否合法为空；
4. 全市场休市窗口（在线从 trade_cal 找最近节假日）区间请求是否无异常返回 0 行
   ——个股停牌区间空结果语义的同构证据；
5. 未来日期区间请求是否异常还是合法为空；
6. 超长区间（000001.SZ 自 1991 年上市全历史，约 8600 交易日）是否被
   单次行数上限截断（上限行为实测：完整返回还是停在 6000 行附近）；
7. moneyflow 对早期年份/该股是否全区间覆盖（非覆盖证券的空结果形态）。

运行：

    .venv/bin/python scripts/spike/verify_stock_range_fetch_online.py

Token 来源：``config.yaml -> tushare.token``（由 ``load_config`` 决定）。
**本脚本只打印"是否配置"，绝不打印 Token 本身**；异常文本经
``TushareError`` 归一化，不含 Token。

退出码：0 = 全部检查完成（即使某场景返回 0 行）；1 = 无法访问上游或无 Token。

结论回填：把各场景 [观察] 行的输出记录到
``openspec/changes/per-stock-history-sync/design.md`` Open Questions 1/2，
并作为离线测试 fake 响应基线（tasks 1.2）。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from app.config import load_config
from app.providers.history.tushare import (
    ADJ_FACTOR_FIELDS,
    DAILY_BASIC_FIELDS,
    DAILY_FIELDS,
    MONEYFLOW_FIELDS,
    STOCK_BASIC_FIELDS,
)
from app.providers.tushare_common import DAILY_ROW_CAP, TushareTransport

NORMAL = "000001.SZ"  # 平安银行：1991-04-03 上市，16 年同步区间稳定存在
LEGACY = "000022.SZ"  # 深赤湾A → 2018-12-26 变更为 001872.SZ（招商港口）
HISTORY_START = date(2010, 1, 4)  # v0.3.0 起历史数据起点（严格交易日）
FULL_HISTORY_START = date(1991, 4, 3)  # 000001.SZ 上市日（超长区间观察）

# endpoint -> 显式 fields（与 Provider 口径一致，避免依赖 Tushare 默认列）
ENDPOINTS: dict[str, tuple[str, ...]] = {
    "daily": DAILY_FIELDS,
    "adj_factor": ADJ_FACTOR_FIELDS,
    "daily_basic": DAILY_BASIC_FIELDS,
    "moneyflow": MONEYFLOW_FIELDS,
}


def _yyyymmdd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _parse_yyyymmdd(text: str) -> date:
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _row_order(dates: list[date]) -> str:
    """返回行序观察结论（升序 / 降序 / 乱序）。"""
    if dates == sorted(dates):
        return "升序"
    if dates == sorted(dates, reverse=True):
        return "降序"
    return "乱序（实现需自行排序，不可依赖上游行序）"


def _check_fields(df, fields: tuple[str, ...]) -> str:
    columns = set(df.columns)
    missing = [name for name in fields if name not in columns]
    if missing:
        return f"缺字段 {missing[:5]}"
    return "字段完整"


def _fetch(
    transport: TushareTransport,
    endpoint: str,
    ts_code: str,
    start: date,
    end: date,
):
    """一次区间请求，返回 (DataFrame | None, 失败文本 | None)。"""
    return transport.call(
        endpoint,
        ts_code=ts_code,
        start_date=_yyyymmdd(start),
        end_date=_yyyymmdd(end),
        fields=",".join(ENDPOINTS[endpoint]),
    )


def _observe(
    transport: TushareTransport,
    endpoint: str,
    label: str,
    ts_code: str,
    start: date,
    end: date,
) -> dict:
    """单场景单 endpoint 请求并打印 [观察] 行；返回观察结果 dict。"""
    try:
        df = _fetch(transport, endpoint, ts_code, start, end)
    except Exception as exc:  # 归一化异常的文本不含 Token
        print(f"  [{label}] {endpoint} {ts_code} "
              f"[{start}..{end}]: 请求失败 {type(exc).__name__} {str(exc)[:100]}")
        return {"label": label, "endpoint": endpoint, "error": str(exc)[:100]}
    rows = 0 if df is None else len(df)
    result: dict = {
        "label": label,
        "endpoint": endpoint,
        "rows": rows,
    }
    if rows == 0:
        print(f"  [{label}] {endpoint} {ts_code} [{start}..{end}]: "
              "0 行、无异常返回（→ 合法空结果，OQ1 证据）")
        return result
    records = df.to_dict("records")
    dates = [_parse_yyyymmdd(str(r["trade_date"])) for r in records]
    codes = sorted({str(r["ts_code"]) for r in records})
    result.update(
        {
            "dates": (min(dates), max(dates)),
            "codes": codes,
            "order": _row_order(dates),
        }
    )
    print(
        f"  [{label}] {endpoint} {ts_code} [{start}..{end}]: "
        f"{rows} 行（距 {DAILY_ROW_CAP} 上限 {DAILY_ROW_CAP - rows}）、"
        f"日期范围 {min(dates)}..{max(dates)}、行序{result['order']}、"
        f"{_check_fields(df, ENDPOINTS[endpoint])}、ts_code 集合 {codes[:3]}"
    )
    return result


def _last_completed_trade_day(transport: TushareTransport) -> date:
    """最近一个已完成交易日（经 trade_cal 实查，保守取今天之前）。"""
    end = date.today()
    start = end - timedelta(days=20)
    df = transport.call(
        "trade_cal",
        exchange="SSE",
        start_date=_yyyymmdd(start),
        end_date=_yyyymmdd(end),
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    assert len(df) > 0
    open_days = [
        _parse_yyyymmdd(str(row["cal_date"]))
        for row in df.to_dict("records")
        if int(row["is_open"]) == 1 and str(row["cal_date"]) < _yyyymmdd(end)
    ]
    assert open_days, "近 20 天内应存在已完成交易日"
    return max(open_days)


def _holiday_window(transport: TushareTransport) -> tuple[date, date] | None:
    """最近一个全市场休市连续窗口（≥3 自然日，如春节/国庆）。

    个股"停牌区间空结果"的同构证据：休市窗口内任何 ts_code 的区间请求
    都应无异常返回 0 行。
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=240)
    df = transport.call(
        "trade_cal",
        exchange="SSE",
        start_date=_yyyymmdd(start),
        end_date=_yyyymmdd(end),
        fields="exchange,cal_date,is_open",
    )
    calendar = {
        _parse_yyyymmdd(str(row["cal_date"])): int(row["is_open"])
        for row in df.to_dict("records")
    }
    closed = sorted(day for day, is_open in calendar.items() if is_open == 0)
    best: tuple[date, date] | None = None
    run_start = closed[0] if closed else None
    previous = closed[0] if closed else None
    for day in closed[1:]:
        if (day - previous).days == 1:
            previous = day
            continue
        if run_start is not None and previous is not None:
            span = (previous - run_start).days + 1
            if span >= 3:
                best = (run_start, previous)
        run_start, previous = day, day
    if run_start is not None and previous is not None:
        span = (previous - run_start).days + 1
        if span >= 3:
            best = (run_start, previous)
    return best


def _delisted_sample(transport: TushareTransport) -> tuple[str, date] | None:
    """在线选一只退市样本股（SSE、delist_date 在 2015~2025 之间）。"""
    df = transport.call(
        "stock_basic",
        exchange="SSE",
        list_status="D",
        fields=",".join(STOCK_BASIC_FIELDS),
    )
    if df is None or len(df) == 0:
        return None
    candidates = [
        (str(row["ts_code"]), _parse_yyyymmdd(str(row["delist_date"])))
        for row in df.to_dict("records")
        if row.get("delist_date")
        and date(2015, 1, 1)
        <= _parse_yyyymmdd(str(row["delist_date"]))
        <= date(2025, 12, 31)
    ]
    if not candidates:
        return None
    return candidates[0]


def main() -> int:
    config = load_config()
    configured = bool(getattr(config.tushare, "token", None))
    print("=" * 72)
    print("Tushare 按股票区间拉取在线验证（只读，不写任何数据库）")
    print("=" * 72)
    print(f"Token 是否配置: {'是' if configured else '否'}")
    if not configured:
        print("未配置 Token：请先在 config.yaml 填写 tushare.token 后重跑。")
        return 1

    transport = TushareTransport(config)
    last_trade_day = _last_completed_trade_day(transport)
    print(f"最近已完成交易日: {last_trade_day}")
    print()

    # ---- 1. 正常股 16 年同步区间 ----
    print("-" * 72)
    print(f"1) 正常股 {NORMAL} 16 年同步区间 [{HISTORY_START}..{last_trade_day}]")
    print("-" * 72)
    daily_rows: dict[str, int] = {}
    for endpoint in ENDPOINTS:
        result = _observe(
            transport, endpoint, "正常股16年", NORMAL, HISTORY_START, last_trade_day
        )
        daily_rows[endpoint] = result.get("rows", -1)
    print()

    # ---- 2. 旧代码股（别名行为）----
    print("-" * 72)
    print(f"2) 旧代码股 {LEGACY}（→ 001872.SZ 别名）同一区间")
    print("-" * 72)
    for endpoint in ENDPOINTS:
        _observe(
            transport, endpoint, "旧代码股", LEGACY, HISTORY_START, last_trade_day
        )
    print()

    # ---- 3. 退市股末段 ----
    print("-" * 72)
    print("3) 退市股末段区间（在线选样）")
    print("-" * 72)
    delisted = _delisted_sample(transport)
    if delisted is None:
        print("  在线 stock_basic(D) 未找到 2015~2025 退市样本，跳过本场景。")
    else:
        delist_code, delist_date = delisted
        # 退市前后各一段：末段应有数据、退市日后应合法为空
        tail_start = delist_date - timedelta(days=180)
        after_start = delist_date + timedelta(days=1)
        after_end = delist_date + timedelta(days=30)
        print(f"  样本: {delist_code} delist_date={delist_date}")
        for endpoint in ENDPOINTS:
            _observe(
                transport, endpoint, "退市末段", delist_code, tail_start, delist_date
            )
        for endpoint in ENDPOINTS:
            _observe(
                transport, endpoint, "退市日后", delist_code, after_start, after_end
            )
    print()

    # ---- 4. 全市场休市窗口（空结果同构证据）----
    print("-" * 72)
    print("4) 全市场休市窗口区间请求（个股停牌空结果的同构证据）")
    print("-" * 72)
    window = _holiday_window(transport)
    if window is None:
        print("  近 240 天未找到 ≥3 天连续休市窗口，跳过本场景。")
    else:
        holiday_start, holiday_end = window
        print(f"  休市窗口: {holiday_start}..{holiday_end}")
        for endpoint in ENDPOINTS:
            _observe(
                transport,
                endpoint,
                "休市窗口",
                NORMAL,
                holiday_start,
                holiday_end,
            )
    print()

    # ---- 5. 未来日期区间 ----
    print("-" * 72)
    print("5) 未来日期区间（未发布/未发生）")
    print("-" * 72)
    future_start = date.today() + timedelta(days=1)
    future_end = future_start + timedelta(days=7)
    for endpoint in ENDPOINTS:
        _observe(
            transport, endpoint, "未来区间", NORMAL, future_start, future_end
        )
    print()

    # ---- 6. 超长区间（行数上限实测）----
    print("-" * 72)
    print(
        f"6) 超长全历史区间 {NORMAL} "
        f"[{FULL_HISTORY_START}..{last_trade_day}]（约 8600 交易日，上限行为）"
    )
    print("-" * 72)
    for endpoint in ENDPOINTS:
        result = _observe(
            transport,
            endpoint,
            "超长区间",
            NORMAL,
            FULL_HISTORY_START,
            last_trade_day,
        )
        rows = result.get("rows", -1)
        dates = result.get("dates")
        if rows >= DAILY_ROW_CAP:
            print(
                f"      → 行数达到上限 {DAILY_ROW_CAP}"
                f"（max_date={dates[1] if dates else '?'}）：超长区间被截断，"
                "同步区间必须按数据集起点约束（本项目 history.start_date=2010-01-01 "
                "天然规避）"
            )
        elif dates and dates[1] < last_trade_day:
            print(
                f"      → 行数 {rows} 未达上限但 max_date={dates[1]} 早于请求终点"
                "（异常形态，需复查）"
            )
    print()

    # ---- 7. 停牌缺口观察（正常股近一年）----
    print("-" * 72)
    print(f"7) {NORMAL} 近一年停牌缺口（daily 返回行数 vs 日历应有交易日数）")
    print("-" * 72)
    year_start = last_trade_day - timedelta(days=365)
    df_cal = transport.call(
        "trade_cal",
        exchange="SSE",
        start_date=_yyyymmdd(year_start),
        end_date=_yyyymmdd(last_trade_day),
        fields="exchange,cal_date,is_open",
    )
    expected = sum(
        1
        for row in df_cal.to_dict("records")
        if int(row["is_open"]) == 1
        and year_start
        <= _parse_yyyymmdd(str(row["cal_date"]))
        <= last_trade_day
    )
    df_daily = _fetch(
        transport, "daily", NORMAL, year_start, last_trade_day
    )
    actual = 0 if df_daily is None else len(df_daily)
    print(
        f"  日历应有 {expected} 个交易日、daily 实际返回 {actual} 行、"
        f"差额 {expected - actual} = 停牌缺行（区间响应=该区间全部有数据交易日，"
        "缺口不报错、直接缺行 → 与'空结果合法推进水位'语义一致）"
    )
    print()

    # ---- 结论 ----
    print("=" * 72)
    print("结论与回填动作（design.md Open Questions 1/2）")
    print("=" * 72)
    print(
        "  OQ1 空结果语义: 休市窗口/退市日后/未来区间若全部「0 行、无异常返回」，"
    )
    print(
        "    则确认——无异常 0 行 = 有效响应 → 推进水位（fake 基线按此固化）。"
    )
    print(
        f"  OQ2 行数上限: 正常股 16 年 ≈ 4000 行 << {DAILY_ROW_CAP}；"
        "超长全历史若完整返回（行数 > 上限）"
    )
    print(
        "    则区间形态单次上限高于 6000，4000 行场景无截断风险；若恰停在 6000，"
        "同步区间按 history.start_date 约束即可规避（D8 既有防护保留）。"
    )
    print(
        f"  moneyflow 覆盖: 正常股 16 年 daily {daily_rows.get('daily', '?')} 行 vs "
        f"moneyflow {daily_rows.get('moneyflow', '?')} 行，"
    )
    print(
        "    差额即非覆盖交易日（早期年份/停牌），确认「非覆盖证券/年份合法为空」。"
    )
    print(
        "  旧代码股: 若区间请求 000022.SZ 返回 001872.SZ（或旧码行经别名改写），"
        "确认区间形态下别名层口径与单日形态一致（D5）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
