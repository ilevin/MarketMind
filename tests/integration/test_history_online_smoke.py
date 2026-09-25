"""@pytest.mark.online：Tushare 历史接口真实冒烟（tasks 3.10 / 9.2）。

默认排除（pyproject ``-m 'not online'``）；运行：

    .venv/bin/python -m pytest tests/integration/test_history_online_smoke.py -m online -s

只读外部接口、不写任何数据库；无有效 Token 时 skip。
Provider 的 normalize/映射逻辑已由离线单测覆盖，本文件验证上游真实行为
与设计假设一致：权限、显式 fields、YYYYMMDD 日期、BSE ts_code 格式、
各接口行数与上限距离，以及 namechange 规范键唯一性观察
（design.md Open Questions 回填依据，结论随 -s 输出记录）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config import load_config
from app.providers.history.tushare import (
    ADJ_FACTOR_FIELDS,
    DAILY_BASIC_FIELDS,
    DAILY_FIELDS,
    MONEYFLOW_FIELDS,
    NAMECHANGE_FIELDS,
    STOCK_BASIC_FIELDS,
    STOCK_BASIC_REQUIRED,
    STOCK_COMPANY_FIELDS,
    STOCK_COMPANY_ROW_CAP,
)
from app.providers.tushare_common import DAILY_ROW_CAP, TushareTransport

# 模块级 online 标记：默认 addopts 的 ``-m 'not online'`` 才能排除本文件。
# 缺了它，本文件的所有用例都会进入默认全量运行——配了 Token 的机器上
# 会真实访问 Tushare（违反 §76.1「在单元测试中访问真实网络」），
# 未配 Token 时则表现为"恰好 skip"，掩盖该标记缺失。
pytestmark = pytest.mark.online


@pytest.fixture(scope="module")
def transport() -> TushareTransport:
    config = load_config()
    if not config.has_tushare_token:
        pytest.skip("需要真实 Tushare Token（config.yaml -> tushare.token）")
    return TushareTransport(config)


def _parse_yyyymmdd(text: str) -> date:
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


@pytest.fixture(scope="module")
def last_trade_day(transport) -> date:
    """最近一个已完成交易日（经 trade_cal 实查，保守取今天之前）。"""
    end = date.today()
    start = end - timedelta(days=20)
    df = transport.call(
        "trade_cal",
        exchange="SSE",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    assert len(df) > 0
    open_days = [
        _parse_yyyymmdd(str(row["cal_date"]))
        for row in df.to_dict("records")
        if int(row["is_open"]) == 1 and str(row["cal_date"]) < end.strftime("%Y%m%d")
    ]
    assert open_days, "近 20 天内应存在已完成交易日"
    return max(open_days)


# ---- 主档 ----


def test_stock_basic_sse_listed_shard(transport):
    df = transport.call(
        "stock_basic",
        exchange="SSE",
        list_status="L",
        fields=",".join(STOCK_BASIC_FIELDS),
    )
    assert len(df) > 0
    for col in STOCK_BASIC_REQUIRED:
        assert col in df.columns, f"缺少必需字段 {col}"
    rows = df.to_dict("records")
    sample = rows[:200]
    assert all(str(r["ts_code"]).endswith(".SH") for r in sample)
    assert all(str(r["list_status"]) == "L" for r in sample)
    assert all(str(r["symbol"]) == str(r["ts_code"]).split(".")[0] for r in sample)
    print(f"[观察] stock_basic SSE/L 行数 {len(df)}（距 6000 上限 {DAILY_ROW_CAP - len(df)}）")


def test_stock_basic_bse_ts_code_format(transport):
    df = transport.call(
        "stock_basic",
        exchange="BSE",
        list_status="L",
        fields=",".join(STOCK_BASIC_FIELDS),
    )
    rows = df.to_dict("records")
    print(f"[观察] stock_basic BSE/L 行数 {len(rows)}")
    if rows:
        assert all(str(r["ts_code"]).endswith(".BJ") for r in rows[:200]), "BSE ts_code 应为 .BJ 后缀"


def test_stock_company_sse_exchange(transport):
    df = transport.call(
        "stock_company",
        exchange="SSE",
        fields=",".join(STOCK_COMPANY_FIELDS),
    )
    assert len(df) > 0
    assert "ts_code" in df.columns and "com_name" in df.columns
    rows = df.to_dict("records")
    assert all(str(r["ts_code"]).endswith(".SH") for r in rows[:200])
    print(
        f"[观察] stock_company SSE 行数 {len(rows)}"
        f"（距 4500 上限 {STOCK_COMPANY_ROW_CAP - len(rows)}）"
    )


# ---- 日历 ----


def test_trade_cal_recent_window(transport):
    end = date.today()
    start = end - timedelta(days=30)
    df = transport.call(
        "trade_cal",
        exchange="SSE",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        fields="exchange,cal_date,is_open,pretrade_date",
    )
    assert len(df) > 0
    rows = df.to_dict("records")
    for row in rows:
        _parse_yyyymmdd(str(row["cal_date"]))  # 全部可解析
    opened = [r for r in rows if int(r["is_open"]) == 1]
    assert opened, "30 天窗口内应存在交易日"
    pretrade_sample = next(
        (r for r in opened if r.get("pretrade_date")), None
    )
    if pretrade_sample is not None:
        _parse_yyyymmdd(str(pretrade_sample["pretrade_date"]))
        print(f"[观察] pretrade_date 示例: {pretrade_sample['pretrade_date']}")


# ---- 日级四数据集（最近已完成交易日） ----


@pytest.mark.parametrize(
    ("endpoint", "fields"),
    [
        ("daily", DAILY_FIELDS),
        ("adj_factor", ADJ_FACTOR_FIELDS),
        ("daily_basic", DAILY_BASIC_FIELDS),
        ("moneyflow", MONEYFLOW_FIELDS),
    ],
)
def test_day_level_recent_trade_date(transport, last_trade_day, endpoint, fields):
    df = transport.call(
        endpoint,
        trade_date=last_trade_day.strftime("%Y%m%d"),
        fields=",".join(fields),
    )
    assert "ts_code" in df.columns and "trade_date" in df.columns
    rows = df.to_dict("records")
    assert len(rows) > 0, f"{endpoint} 最近交易日不应为空"
    for row in rows[:100]:
        assert _parse_yyyymmdd(str(row["trade_date"])) == last_trade_day
        assert "." in str(row["ts_code"])
    bse = [r for r in rows if str(r["ts_code"]).endswith(".BJ")]
    suffixes = {str(r["ts_code"]).split(".")[1] for r in rows}
    print(
        f"[观察] {endpoint} {last_trade_day} 行数 {len(rows)}"
        f"（距 6000 上限 {DAILY_ROW_CAP - len(rows)}）；"
        f"ts_code 后缀分布 {sorted(suffixes)}；BSE {len(bse)} 行"
    )


# ---- namechange 规范键唯一性观察（design.md Open Question） ----


def test_namechange_known_stock_key_uniqueness(transport):
    # 000001.SZ（平安银行，历史深发展多次更名）为高置信度非空样本
    df = transport.call(
        "namechange",
        ts_code="000001.SZ",
        fields=",".join(NAMECHANGE_FIELDS),
    )
    rows = df.to_dict("records")
    print(f"[观察] namechange 000001.SZ 事件数 {len(rows)}")
    if not rows:
        return
    assert "ts_code" in rows[0] and "name" in rows[0]
    for row in rows[:50]:
        if row.get("start_date"):
            _parse_yyyymmdd(str(row["start_date"]))
    keys = [
        (str(r["ts_code"]), str(r.get("name")), str(r.get("start_date")))
        for r in rows
    ]
    duplicates = len(keys) - len(set(keys))
    print(
        f"[观察] 规范键 (ts_code, name, start_date) 重复条数 {duplicates}；"
        f"end_date/ann_date 字段非空率 "
        f"{sum(1 for r in rows if r.get('end_date'))}/{len(rows)}、"
        f"{sum(1 for r in rows if r.get('ann_date'))}/{len(rows)}"
    )
    assert duplicates == 0, (
        "同一 (ts_code, name, start_date) 存在多条不同事件——"
        "需按 design.md 预案把规范键扩展为含 end_date/ann_date 的五字段"
    )


# ---- 个股区间拉取（per-stock-history-sync tasks 10.1；样本与
#       scripts/spike/verify_stock_range_fetch_online.py 一致） ----

RANGE_NORMAL = "000001.SZ"  # 平安银行：1991 上市，16 年同步区间稳定存在
RANGE_LEGACY = "000022.SZ"  # 深赤湾A → 2018-12-26 变更为 001872.SZ（招商港口）
RANGE_START = date(2010, 1, 4)  # v0.3.0 起历史数据起点（严格交易日）
RANGE_END = date(2026, 1, 1)


@pytest.fixture(scope="module")
def delisted_sample(transport) -> tuple[str, date]:
    """在线选一只退市样本股（SSE、delist_date 在 2015~2025 之间）。

    与 spike 脚本同一选样逻辑；选不到时 skip（不影响其余冒烟）。
    """
    df = transport.call(
        "stock_basic",
        exchange="SSE",
        list_status="D",
        fields=",".join(STOCK_BASIC_FIELDS),
    )
    rows = [
        (str(r["ts_code"]), _parse_yyyymmdd(str(r["delist_date"])))
        for r in df.to_dict("records")
        if r.get("delist_date") and "2015" <= str(r["delist_date"])[:4] <= "2025"
    ]
    if not rows:
        pytest.skip("SSE 退市样本（2015~2025 delist_date）不可得")
    return max(rows, key=lambda pair: pair[1])


def _range_call(transport, endpoint, ts_code, start, end):
    return transport.call(
        endpoint,
        ts_code=ts_code,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        fields=",".join(
            {
                "daily": DAILY_FIELDS,
                "adj_factor": ADJ_FACTOR_FIELDS,
                "daily_basic": DAILY_BASIC_FIELDS,
                "moneyflow": MONEYFLOW_FIELDS,
            }[endpoint]
        ),
    )


@pytest.mark.parametrize(
    "endpoint", ["daily", "adj_factor", "daily_basic", "moneyflow"]
)
def test_stock_range_full_history_normal(transport, endpoint):
    """正常股 16 年全区间：无截断、日期落区间内、ts_code 单一。

    行数上限语义（OQ2）：16 年约 3900 个交易日，单次区间请求应显著
    低于 6000 上限——若逼近上限说明上游区间行为与设计假设不符。
    """
    df = _range_call(transport, endpoint, RANGE_NORMAL, RANGE_START, RANGE_END)
    rows = df.to_dict("records")
    assert len(rows) > 0, f"{endpoint} 16 年区间不应为空"
    assert len(rows) < DAILY_ROW_CAP, (
        f"{endpoint} 16 年区间 {len(rows)} 行逼近上限 {DAILY_ROW_CAP}"
        "——个股区间拉取存在截断风险"
    )
    dates = [_parse_yyyymmdd(str(r["trade_date"])) for r in rows]
    assert all(RANGE_START <= d <= RANGE_END for d in dates), "区间外日期"
    assert {str(r["ts_code"]) for r in rows} == {RANGE_NORMAL}, "正常股应单一 ts_code"
    print(
        f"[观察] {endpoint} {RANGE_NORMAL} 16 年区间 {len(rows)} 行"
        f"（距 {DAILY_ROW_CAP} 上限 {DAILY_ROW_CAP - len(rows)}）、"
        f"日期 {min(dates)}..{max(dates)}、"
        f"行序{'升' if dates == sorted(dates) else '非升序'}"
    )


def test_stock_range_legacy_code_alias(transport):
    """旧代码股区间：返回旧码/新码/混合均可，但别名组内应一致。"""
    df = _range_call(transport, "daily", RANGE_LEGACY, RANGE_START, RANGE_END)
    rows = df.to_dict("records")
    assert len(rows) > 0
    codes = {str(r["ts_code"]) for r in rows}
    assert codes <= {RANGE_LEGACY, "001872.SZ"}, (
        f"旧代码股区间返回了组外代码: {codes}"
    )
    dates = [_parse_yyyymmdd(str(r["trade_date"])) for r in rows]
    print(
        f"[观察] daily {RANGE_LEGACY} 16 年区间 {len(rows)} 行、"
        f"ts_code 集合 {sorted(codes)}、日期 {min(dates)}..{max(dates)}"
    )


def test_stock_range_suspended_window_empty(transport):
    """停牌/休市窗口（2021 春节）：空结果无异常返回（OQ1 证据）。"""
    df = _range_call(
        transport, "daily", RANGE_NORMAL,
        date(2021, 2, 11), date(2021, 2, 17),
    )
    assert len(df) == 0, "春节休市窗口应为 0 行（个股空结果语义）"
    print("[观察] daily 春节休市窗口 0 行、无异常（合法空结果）")


def test_stock_range_delisted_tail_and_after(transport, delisted_sample):
    """退市股末段有数据、退市日后合法为空（生命周期边界，design D8）。"""
    ts_code, delist_date = delisted_sample
    tail_start = delist_date - timedelta(days=30)

    df_tail = _range_call(transport, "daily", ts_code, tail_start, delist_date)
    print(
        f"[观察] daily 退市股 {ts_code} 末段 [{tail_start}..{delist_date}] "
        f"{len(df_tail)} 行"
    )
    if len(df_tail):
        dates = [_parse_yyyymmdd(str(r["trade_date"])) for r in df_tail.to_dict("records")]
        assert max(dates) <= delist_date, "退市日后不应有数据"

    df_after = _range_call(
        transport, "daily", ts_code,
        delist_date + timedelta(days=1), delist_date + timedelta(days=30),
    )
    assert len(df_after) == 0, "退市日后区间应为 0 行（合法空结果）"
    print(
        f"[观察] daily 退市股 {ts_code} 退市日后区间 0 行、无异常"
    )
