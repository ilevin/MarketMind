"""在线验证：ETF 数据源（东财列表/历史、新浪备选、Tushare fund_adj）真实行为。

对应 etf-data-module tasks 1.1/1.2（design.md Open Questions 1/2/3/4）——
**Provider 实现细节的最终裁决依据**：

    1. 东财 ETF 列表接口 fund_etf_spot_em 可用性与返回列（是否含市场列/
       上市日期列）；失败备选 fund_etf_category_sina（新浪源，当前实现所用）；
    2. 东财 ETF 历史接口 fund_etf_hist_em 可用性、返回列、成交量/成交额/
       换手率单位口径、adjust='' 不复权行为、超大区间行数；失败备选
       fund_etf_hist_sina（新浪源，列与单位差异记录）；
    3. Tushare fund_adj：积分门槛与无权限错误形态、返回字段、**行覆盖语义**
       （每交易日一行 vs 仅除权事件日一行）、单次行数上限、停牌/退市 ETF
       空结果行为（样本：510300 全区间、新上市 ETF、疑似不覆盖 ETF）。

样本选择（OQ 样本口径）：
- 正常 ETF：510300（沪深300ETF，2012-05-28 上市，全区间约 3400+ 交易日）；
- 新上市 ETF：从列表在线挑近一年上市的高代码样本（如 56xxxx/58xxxx）；
- 疑似退市 ETF：510181（华安上证180ETF，已退市——列表不出现、历史或缺失）。

运行：

    .venv/bin/python scripts/spike/verify_etf_sources_online.py

Token 来源：``config.yaml -> tushare.token``（由 ``load_config`` 决定）；
无 Token 时仅实测 3 的「无权限错误形态」一节。**本脚本只读，不写任何
数据库、不改任何文件**；不打印 Token 本身。

退出码：0 = 全部检查完成（即使某接口不可用）；1 = 全部数据源不可达。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

from app.config import load_config

NORMAL_ETF = "510300"  # 沪深300ETF：2012-05-28 上市，全区间稳定存在
NEW_ETF_PREFIXES = ("56", "58")  # 近年新上市代码段（SSE）
DELISTED_ETF = "510181"  # 华安上证180ETF：已退市样本
ETF_HIST_ROW_CAP = 10000  # design D6：东财单次行数上限防护阈值


def _section(title: str) -> None:
    print()
    print("-" * 72)
    print(title)
    print("-" * 72)


def _akshare_version() -> str:
    import akshare as ak

    return ak.__version__


def _probe(fn, label: str):
    """执行一次接口调用，返回 (DataFrame | None, 失败文本 | None)，打印观察行。"""
    try:
        df = fn()
    except Exception as exc:
        text = f"{type(exc).__name__}: {str(exc)[:160]}"
        print(f"  [{label}] 请求失败 {text}")
        return None, text
    rows = 0 if df is None else len(df)
    cols = [] if df is None else list(df.columns)
    print(f"  [{label}] 返回 {rows} 行；列: {cols}")
    return df, None


def _list_observation(df) -> None:
    """列表接口列清单观察：市场列/上市日期列是否存在、前缀形态。"""
    if df is None or len(df) == 0:
        return
    cols = set(df.columns)
    has_market = any("市场" in c for c in cols)
    has_list_date = any("上市" in c or "成立" in c for c in cols)
    print(
        f"    显式市场列: {'有' if has_market else '无'}（代码前缀承载市场信息时按前缀解析）；"
        f"上市日期列: {'有' if has_list_date else '无'}（无则 list_date 存 NULL）"
    )
    sample = df.head(2).to_dict("records")
    for row in sample:
        code = row.get("代码") or row.get("代码") or ""
        print(f"    样本行: 代码={code!r} 名称={row.get('名称')!r}")


def _hist_observation(df, *, label: str, source: str) -> None:
    """历史接口观察：单位口径、adjust 行为、行序、区间覆盖。"""
    if df is None or len(df) == 0:
        return
    records = df.to_dict("records")
    first, last = records[0], records[-1]
    print(f"    首行: {first}")
    print(f"    末行: {last}")
    # 单位口径观察（记录原始值供人工判断量级：手 vs 股、元 vs 千元）
    if source == "eastmoney":
        vol, amt, turnover = (
            first.get("成交量"),
            first.get("成交额"),
            first.get("换手率"),
        )
        print(
            f"    单位观察: 成交量={vol}（东财 ETF 接口口径为手）、"
            f"成交额={amt}（元）、换手率={turnover}（百分比数值）"
        )
    else:
        vol, amt = first.get("volume"), first.get("amount")
        print(
            f"    单位观察: volume={vol}（新浪口径为股）、amount={amt}（元）、"
            f"换手率列: {'无（新浪接口不提供）' if 'turnover' not in str(df.columns) else '有'}"
        )
    dates = [r.get("日期") or r.get("date") for r in records]
    try:
        ordered = dates == sorted(dates)
        reverse = dates == sorted(dates, reverse=True)
        order = "升序" if ordered else ("降序" if reverse else "乱序")
    except TypeError:
        order = "不可比较（日期类型混合）"
    print(f"    行数 {len(records)}（距 {ETF_HIST_ROW_CAP} 上限 "
          f"{ETF_HIST_ROW_CAP - len(records)}）、行序{order}")


def _pick_new_etf(list_df) -> str | None:
    """从列表在线挑一只新上市样本（高代码段 56/58 开头）。"""
    if list_df is None or len(list_df) == 0:
        return None
    codes = [str(r.get("代码", "")) for r in list_df.to_dict("records")]
    stripped = [c[2:8] if len(c) >= 8 and c[:2].lower() in ("sz", "sh") else c for c in codes]
    for prefix in NEW_ETF_PREFIXES:
        candidates = sorted(c for c in stripped if c.startswith(prefix) and len(c) == 6)
        if candidates:
            return candidates[-1]
    return None


def _tushare_fund_adj(token: str, ts_code: str, start: str, end: str):
    """fund_adj 单次请求（requests 直调，异常形态原样捕获）。"""
    import json

    import requests

    payload = {
        "api_name": "fund_adj",
        "token": token,
        "params": {"ts_code": ts_code, "start_date": start, "end_date": end},
        "fields": "ts_code,trade_date,adj_factor",
    }
    resp = requests.post(
        "http://api.tushare.pro",
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    body = resp.json()
    if body.get("code") != 0:
        return None, f"code={body.get('code')} msg={body.get('msg')}"
    data = body.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    return {"fields": fields, "items": items, "rows": len(items)}, None


def _yyyymmdd(day: date) -> str:
    return day.strftime("%Y%m%d")


def main() -> int:
    config = load_config()
    token = getattr(config.tushare, "token", None)
    today = date.today()

    print("=" * 72)
    print("ETF 数据源在线验证（只读，不写任何数据库）")
    print("=" * 72)
    print(f"akshare 版本: {_akshare_version()}")
    print(f"Tushare Token 是否配置: {'是' if token else '否'}")
    print(f"今日: {today}")
    any_reachable = False

    # ---- 1. ETF 列表接口（OQ1：东财首选 fund_etf_spot_em + 备选新浪） ----
    _section("1) ETF 列表接口（fund_etf_spot_em 东财首选 / fund_etf_category_sina 备选）")
    import akshare as ak

    spot_df, spot_err = _probe(
        lambda: ak.fund_etf_spot_em(), "东财 fund_etf_spot_em"
    )
    if spot_err is None:
        any_reachable = True
        _list_observation(spot_df)
    category_df, category_err = _probe(
        lambda: ak.fund_etf_category_sina(symbol="ETF基金"), "新浪 fund_etf_category_sina"
    )
    if category_err is None:
        any_reachable = True
        _list_observation(category_df)
    print(
        "    → 首选裁决: "
        + (
            "东财可用 → 按 D4 用东财列表（显式市场列观察如上）"
            if spot_err is None
            else "东财列表不可用 → 备选新浪可用（代码前缀 sz/sh 承载市场，无上市日期列 → "
            "list_date NULL + 首位推断 5→SSE/1→SZSE，当前实现口径）"
        )
    )
    new_etf = _pick_new_etf(category_df if category_err is None else spot_df)
    print(f"    在线挑选新上市样本: {new_etf or '未找到（跳过该场景）'}")

    # ---- 2. 东财 ETF 历史接口（OQ1：可用性/列/单位/adjust 行为/行数上限） ----
    _section(f"2) ETF 历史接口（fund_etf_hist_em，样本 {NORMAL_ETF} 全区间）")
    hist_df, hist_err = _probe(
        lambda: ak.fund_etf_hist_em(
            symbol=NORMAL_ETF, period="daily",
            start_date="20120528", end_date=_yyyymmdd(today), adjust="",
        ),
        f"东财 {NORMAL_ETF} 全历史",
    )
    if hist_err is None:
        any_reachable = True
        _hist_observation(hist_df, label=NORMAL_ETF, source="eastmoney")
        # adjust='' 不复权对照：510300 有分红 → 复权与不复权价应不同
        try:
            qfq_df = ak.fund_etf_hist_em(
                symbol=NORMAL_ETF, period="daily",
                start_date="20200101", end_date="20200110", adjust="qfq",
            )
            raw_recent = hist_df[
                (hist_df["日期"] >= "2020-01-01") & (hist_df["日期"] <= "2020-01-10")
            ] if "日期" in hist_df.columns else None
            print(
                f"    adjust 行为: qfq 区间 {0 if qfq_df is None else len(qfq_df)} 行 vs "
                f"raw 同区间 {0 if raw_recent is None else len(raw_recent)} 行"
                f"（价格不同即证明 adjust='' 为不复权原始价）"
            )
            if qfq_df is not None and len(qfq_df) > 0 and raw_recent is not None and len(raw_recent) > 0:
                print(
                    f"    样本价对照: raw close={raw_recent.iloc[0].get('收盘')} vs "
                    f"qfq close={qfq_df.iloc[0].get('收盘')}"
                )
        except Exception as exc:
            print(f"    [adjust 对照] 失败 {type(exc).__name__}: {str(exc)[:120]}")
    else:
        print("    → 东财历史通道（push2his.eastmoney.com）不可达：")
        print("      备选观察 fund_etf_hist_sina（新浪，注意列与单位差异）：")
        sina_df, sina_err = _probe(
            lambda: ak.fund_etf_hist_sina(symbol=f"sh{NORMAL_ETF}"),
            f"新浪 sh{NORMAL_ETF} 全历史",
        )
        if sina_err is None:
            any_reachable = True
            _hist_observation(sina_df, label=NORMAL_ETF, source="sina")
            print(
                "      → 新浪历史可用但**无换手率列**且单位口径不同（股 vs 手）；"
                "东财不可用环境中 etf_daily 段将持续 FAILED（失败域隔离设计生效），"
                "换源需新 provider + 配置（registry 已支持按数据集选源）"
            )

    # ---- 3. 样本行为：新上市 ETF / 疑似退市 ETF（东财不可达时跳过） ----
    _section("3) 样本行为：新上市 ETF 与疑似退市 ETF（etf_hist_em）")
    if hist_err is not None:
        print("  东财历史接口不可达，本节跳过（结论记为待东财可用环境补测）。")
    else:
        if new_etf:
            _probe(
                lambda: ak.fund_etf_hist_em(
                    symbol=new_etf, period="daily",
                    start_date="20200101", end_date=_yyyymmdd(today), adjust="",
                ),
                f"新上市 {new_etf} 自 2020",
            )
        _probe(
            lambda: ak.fund_etf_hist_em(
                symbol=DELISTED_ETF, period="daily",
                start_date="20200101", end_date=_yyyymmdd(today), adjust="",
            ),
            f"疑似退市 {DELISTED_ETF} 自 2020（空结果语义观察）",
        )

    # ---- 4. Tushare fund_adj（OQ2/OQ3：积分/字段/行覆盖语义/上限/空结果） ----
    _section("4) Tushare fund_adj（积分门槛/字段/行覆盖语义/上限/空结果）")
    if not token:
        # 无 Token：只固化「无权限错误形态」（OQ2 一部分）
        body, err = _tushare_fund_adj(
            "invalid-token-spike", "510300.SH", "20250901", "20250930"
        )
        print(f"  无 Token 请求错误形态: {err}")
        print("    → 错误码 40101「token 不对」即无权限/无效 Token 形态；")
        print("      有积分门槛时同一通道返回的 code/msg 需有 Token 环境补测。")
    else:
        result, err = _tushare_fund_adj(
            token, "510300.SH", "20120101", _yyyymmdd(today)
        )
        if err is not None:
            print(f"  [510300 全区间] 失败: {err}")
            print("    → 若为积分不足类错误码，记录其 code/msg 形态（OQ2 证据）。")
        else:
            any_reachable = True
            items = result["items"]
            print(
                f"  [510300 全区间] {result['rows']} 行、fields={result['fields']}"
            )
            if items:
                trade_dates = sorted(str(it[1]) for it in items)
                factors = [it[2] for it in items]
                # 行覆盖语义：区间内交易日数 vs 返回行数
                years = (int(_yyyymmdd(today)[:4])) - 2012
                approx_trade_days = years * 244
                print(
                    f"    日期范围 {trade_dates[0]}..{trade_dates[1]}、"
                    f"区间约 {approx_trade_days} 交易日 vs 返回 {result['rows']} 行"
                )
                if result["rows"] >= approx_trade_days * 0.9:
                    print("    → 行覆盖语义: 每交易日一行（factor_at 直接查表）")
                else:
                    print(
                        "    → 行覆盖语义: 仅事件日有行（factor_at 需 forward-fill："
                        "取 <= trade_date 的最近因子）"
                    )
                print(
                    f"    因子值域: min={min(factors)} max={max(factors)}"
                    f"（>0 校验依据）、相邻日重复值观察: "
                    f"前 5 行 {items[:5]}"
                )
                if result["rows"] >= 6000:
                    print(f"    → 行数达到单次上限区间（>= 6000）：截断阈值确认依据")
        # 停牌/退市/新上市样本
        for label, code, start, end in (
            ("新上市 ETF", f"{new_etf}.SH" if new_etf and new_etf.startswith(("5", "1")) else "560000.SH", "20250101", _yyyymmdd(today)),
            ("疑似退市 ETF", f"{DELISTED_ETF}.SH", "20200101", _yyyymmdd(today)),
            ("未来区间", "510300.SH", _yyyymmdd(today + timedelta(days=1)),
             _yyyymmdd(today + timedelta(days=7))),
        ):
            r, e = _tushare_fund_adj(token, code, start, end)
            if e is not None:
                print(f"  [{label}] {code}: 失败 {e}")
            else:
                print(
                    f"  [{label}] {code} [{start}..{end}]: "
                    f"{r['rows']} 行（0 行=合法空结果 → 水位推进，OQ3 证据）"
                )

    # ---- 结论 ----
    _section("结论与回填动作（design.md Open Questions 1/2/3/4）")
    print(
        "  OQ1: 按上述实测——东财列表/历史接口可用性与列清单、单位口径；"
        "新浪备选的可用性与列差异（无上市日期列/无换手率列）。"
    )
    print(
        "  OQ2: fund_adj 行覆盖语义（每日一行 vs 仅事件日）→ 决定 "
        "EtfDataService._factor_at 是否 forward-fill（当前实现为直接查表，"
        "仅此单点需按结论改）。"
    )
    print(
        "  OQ3: 停牌/退市/未来区间空结果语义 → 固化离线 fake 基线。"
    )
    print(
        "  OQ4: cutoff 观察不足时维持默认（etf_daily 16:30 / etf_adj_factor 09:30），"
        "配置可调。"
    )
    if not any_reachable:
        print("\n全部数据源不可达，退出码 1。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
