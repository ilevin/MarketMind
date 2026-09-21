"""在线验证：000022.SZ / 001872.SZ 在四个 endpoint 上的真实返回行为。

对应 UNKNOWN_INSTRUMENT 修复方案 §14.1——**修复上线前必须跑一次**：

    不要假设 2019 年 issue 描述的返回行为在 2026 年仍逐字段完全相同。

本脚本回答四个问题（全部只读，不写任何数据库、不改任何文件）：

1. ``stock_basic`` 是否含 000022.SZ / 001872.SZ（含各 exchange × list_status）；
2. 2010-01-04 / 2010-01-05 的 daily / adj_factor / daily_basic / moneyflow
   分别返回的是旧代码、新代码，还是两者都有；
3. 两者都有时，逐字段比较是否一致（决定 ALIAS_CONFLICT 会不会误触发）；
4. 主档里 001872.SZ 的 list_date / delist_date（确认回填目标日它应当在场）。

运行：

    .venv/bin/python scripts/spike/verify_ts_code_alias_online.py

Token 来源：``config.yaml -> tushare.token``（或 ``TUSHARE_TOKEN`` 环境变量，
由 ``load_config`` 决定）。**本脚本只打印"是否配置"与长度，绝不打印 Token
本身**，也不打印任何请求对象；异常文本经 ``TushareError`` 归一化，不含 Token。

退出码：0 = 全部检查完成（即使结论是"两者都返回"）；1 = 无法访问上游。
"""

from __future__ import annotations

import sys
from datetime import date

import pandas as pd

from app.config import load_config
from app.providers.history.tushare import (
    ADJ_FACTOR_FIELDS,
    DAILY_BASIC_FIELDS,
    DAILY_FIELDS,
    MONEYFLOW_FIELDS,
    STOCK_BASIC_EXCHANGES,
    STOCK_BASIC_FIELDS,
    STOCK_BASIC_LIST_STATUSES,
    TUSHARE_TS_CODE_ALIASES,
)
from app.providers.history.tushare_aliases import _rows_equal
from app.providers.tushare_common import TushareTransport

LEGACY = "000022.SZ"
CANONICAL = "001872.SZ"
DAYS = (date(2010, 1, 4), date(2010, 1, 5))

# endpoint -> 显式 fields（与 Provider 口径一致，避免依赖 Tushare 默认列）
ENDPOINTS: dict[str, tuple[str, ...]] = {
    "daily": DAILY_FIELDS,
    "adj_factor": ADJ_FACTOR_FIELDS,
    "daily_basic": DAILY_BASIC_FIELDS,
    "moneyflow": MONEYFLOW_FIELDS,
}


def _yyyymmdd(day: date) -> str:
    return day.strftime("%Y%m%d")


def _codes_in(df) -> list[str]:
    if df is None or len(df) == 0 or "ts_code" not in set(df.columns):
        return []
    return sorted({str(code) for code in df["ts_code"]})


def _verdict(codes: list[str]) -> str:
    has_legacy, has_canonical = LEGACY in codes, CANONICAL in codes
    if has_legacy and has_canonical:
        return "新旧都返回"
    if has_legacy:
        return "只返回旧代码"
    if has_canonical:
        return "只返回新代码"
    return "两者都不返回"


def _compare_two_rows(df) -> str:
    """新旧同时返回时逐字段比较（用修复后的实际比较函数，口径一致）。"""
    rows = {str(row["ts_code"]): row for row in df.to_dict("records")}
    if LEGACY not in rows or CANONICAL not in rows:
        return "（不适用）"
    if _rows_equal(rows[LEGACY], rows[CANONICAL]):
        return "字段一致 → 修复后保留 canonical、丢弃旧代码行"
    diff = sorted(
        key for key in set(rows[LEGACY]) | set(rows[CANONICAL])
        if str(rows[LEGACY].get(key)) != str(rows[CANONICAL].get(key))
    )
    preview = ", ".join(f"{k}" for k in diff[:10])
    return f"字段**不一致** → 修复后会抛 ALIAS_CONFLICT，需人工判定：{preview}"


def main() -> int:
    config = load_config()
    configured = bool(getattr(config.tushare, "token", None))
    print("=" * 72)
    print("Tushare ts_code 别名在线验证（只读，不写任何数据库）")
    print("=" * 72)
    print(f"Token 是否配置: {'是' if configured else '否'}")
    if not configured:
        print("未配置 Token：请先在 config.yaml 填写 tushare.token 后重跑。")
        return 1

    print(f"当前登记的别名表: {TUSHARE_TS_CODE_ALIASES}")
    print()
    transport = TushareTransport(config)
    advisories: list[str] = []

    # ---- 1. stock_basic 分片扫描 ----
    print("-" * 72)
    print("1) stock_basic：旧代码是否真的不在主档（全部 exchange × list_status）")
    print("-" * 72)
    found: dict[str, list[str]] = {LEGACY: [], CANONICAL: []}
    canonical_meta: dict = {}
    for exchange in STOCK_BASIC_EXCHANGES:
        for list_status in STOCK_BASIC_LIST_STATUSES:
            try:
                df = transport.call(
                    "stock_basic",
                    exchange=exchange,
                    list_status=list_status,
                    fields=",".join(STOCK_BASIC_FIELDS),
                )
            except Exception as exc:  # 归一化异常的文本不含 Token
                print(f"  {exchange}/{list_status}: 请求失败 {type(exc).__name__}"
                      f" {str(exc)[:120]}")
                return 1
            codes = set()
            if df is not None and len(df) > 0 and "ts_code" in set(df.columns):
                codes = {str(code) for code in df["ts_code"]}
            for target in (LEGACY, CANONICAL):
                if target in codes:
                    found[target].append(f"{exchange}/{list_status}")
                    if target == CANONICAL:
                        row = next(
                            r for r in df.to_dict("records")
                            if str(r["ts_code"]) == CANONICAL
                        )
                        canonical_meta = {
                            "name": row.get("name"),
                            "exchange": row.get("exchange"),
                            "list_status": row.get("list_status"),
                            "list_date": row.get("list_date"),
                            "delist_date": row.get("delist_date"),
                        }
    print(f"  {LEGACY}  命中分片: {found[LEGACY] or '无（与预期一致）'}")
    print(f"  {CANONICAL} 命中分片: {found[CANONICAL] or '无（异常！需人工确认）'}")
    if canonical_meta:
        print(f"  {CANONICAL} 主档信息: {canonical_meta}")
        list_date = str(canonical_meta.get("list_date") or "")
        if list_date and list_date > "20100104":
            advisories.append(
                f"注意：主档 list_date={list_date} 晚于回填目标日 2010-01-04。"
                "daily_basic 截断补齐的候选集会漏掉该证券（候选集按 list_date 过滤），"
                "但主路径不受影响。"
            )
    if found[LEGACY]:
        advisories.append(
            f"{LEGACY} 仍出现在 stock_basic 的 {found[LEGACY]}——"
            "若主档同时含旧代码，说明 Tushare 把它当作独立证券管理，"
            "需重新评估是否应登记为别名（可能造成事实重复）。"
        )
    if not found[CANONICAL]:
        # 规范代码不在主档：别名改写后的记录将无处映射 instrument，
        # UNKNOWN_INSTRUMENT 依旧会卡住水位——本修复救不了这种情况。
        advisories.append(
            f"【停止部署】规范代码 {CANONICAL} 不在 stock_basic 的任何一个分片。"
            "别名改写后仍无法映射到证券主档，回填会继续以 UNKNOWN_INSTRUMENT 卡住。"
            "请先排查主档为何缺失该证券（权限/上市状态/上游异常），"
            "不要仅凭别名表上线。"
        )
    if not found[LEGACY] and not found[CANONICAL]:
        advisories.append(
            f"【注意】{LEGACY} 与 {CANONICAL} 都未出现在 stock_basic——"
            "无法用主档交叉验证别名登记的正确性，请人工核对权威来源。"
        )
    print()

    # ---- 2~3. 四个 endpoint × 两个日期 ----
    for endpoint, fields in ENDPOINTS.items():
        print("-" * 72)
        print(f"2) {endpoint}：真实返回与新旧比较")
        print("-" * 72)
        for day in DAYS:
            try:
                df = transport.call(
                    endpoint, trade_date=_yyyymmdd(day), fields=",".join(fields)
                )
            except Exception as exc:
                print(f"  {day}: 请求失败 {type(exc).__name__} {str(exc)[:120]}")
                return 1
            rows_total = 0 if df is None else len(df)
            codes = _codes_in(df)
            relevant = [code for code in codes if code in (LEGACY, CANONICAL)]
            print(f"  {day}: 全市场 {rows_total} 行；本次关注代码 {relevant or '无'}"
                  f" → {_verdict(codes)}")
            if LEGACY in relevant and CANONICAL in relevant:
                print(f"      比较结论: {_compare_two_rows(df)}")
            elif LEGACY in relevant:
                print("      比较结论: 仅旧代码 → 修复后改写为 canonical，映射到 001872")
            actual = canonical_meta.get("exchange") if canonical_meta else None
            print(f"      （接口不按 exchange 过滤；{CANONICAL} exchange={actual}）")
        print()

    # ---- 4. 结论 ----
    print("=" * 72)
    print("结论与后续动作")
    print("=" * 72)
    print("  - 上面若出现「只返回旧代码」：当前别名表足够，修复后可直接回填。")
    print("  - 上面若出现「新旧都返回 + 字段一致」：修复会保留 canonical 并丢弃旧行，")
    print("    监控日志会打 action=drop_legacy（WARNING），属预期。")
    print("  - 上面若出现「新旧都返回 + 字段不一致」：修复会抛 ALIAS_CONFLICT，")
    print("    该交易日不推进——不要把别名表改成「随便选一个」，需人工用交易所")
    print("    公告判定哪一套是权威数据。")
    for index, note in enumerate(advisories, start=1):
        print(f"  [{index}] {note}")
    if not advisories:
        print("  （无额外提醒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
