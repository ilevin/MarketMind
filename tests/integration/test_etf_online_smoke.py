"""@pytest.mark.online：ETF 数据源真实冒烟（etf-data-module，tasks 9.2）。

默认排除（pyproject ``-m 'not online'``）；运行：

    .venv/bin/python -m pytest tests/integration/test_etf_online_smoke.py -m online -s

只读外部接口（东财/新浪列表、东财历史、Tushare fund_adj）；无有效 Token 时
fund_adj 相关用例 skip；东财通道（push2his/push2delay）不可达时相关用例
skip 并附原因（spike 2026-10-09：本环境东财历史/列表接口不可达，新浪列表
接口可用——channel 级失败 skip、行为级失败 assert，在线环境恢复后自动
恢复真实断言强度）。

验证内容（对齐 design.md Open Questions 结论）：
- universe：fund_etf_category_sina 返回规模/前缀/解析（当前实现口径）；
- etf_daily：fund_etf_hist_em 列完整性、单位口径（手/元/百分比）、
  adjust='' 不复权、行数距上限（spike 样本 510300）；
- fund_adj：字段、行覆盖语义观察（每日一行 vs 仅事件日——factor_at 分支
  依据）、因子 > 0、空结果行为；
- get_etf_daily raw/qfq/hfq 端到端抽样比对（真实数据落临时 DuckDB，
  复权公式核验：hfq = raw × factor_t、qfq = raw × factor_t ÷ factor_latest）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config import AppConfig, DatabaseConfig, load_config
from app.providers.eastmoney_common import ETF_HIST_ROW_CAP

pytestmark = pytest.mark.online

NORMAL_ETF = "510300"  # 沪深300ETF：2012-05-28 上市，spike 样本
NEW_ETF_PREFIXES = ("56", "58")
DELISTED_ETF = "510181"  # 华安上证180ETF：已退市（spike 样本）

# fund_etf_hist_em 必需列（与 provider 口径一致）
EM_HIST_REQUIRED = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "换手率"]


def _akshare():
    import akshare as ak

    return ak


def _skip_if_channel_down(exc: Exception) -> None:
    """通道级失败（连接/超时/网关）→ skip 并附原因；其余异常原样抛出。

    通道不可达是环境事实而非行为回归——spike 与本文件共享该判别：
    恢复可达后 skip 分支不再触发，真实断言强度自动恢复。
    """
    import requests

    if isinstance(
        exc,
        (requests.exceptions.ConnectionError, requests.exceptions.Timeout),
    ) or "502" in str(exc) or "503" in str(exc):
        pytest.skip(f"东财通道不可达（channel 级失败）: {type(exc).__name__} {str(exc)[:120]}")
    raise exc


# ---- universe（新浪列表，spike 2026-10-09 实测可用） ----


def test_universe_real_list_size_and_prefix():
    """universe 真实规模与前缀解析（当前实现 fund_etf_category_sina 口径）。"""
    ak = _akshare()
    df = ak.fund_etf_category_sina(symbol="ETF基金")
    assert len(df) > 1000, f"上市 ETF 应在千只量级，实际 {len(df)}"
    rows = df.to_dict("records")
    for row in rows:
        code = str(row["代码"])
        assert len(code) == 8 and code[:2].lower() in ("sz", "sh"), (
            f"代码前缀形态异常: {code!r}"
        )
        assert code[2:8].isdigit(), f"6 位数字代码异常: {code!r}"
    prefixes = {str(r["代码"])[:2].lower() for r in rows}
    print(f"[观察] universe {len(df)} 只；前缀集合 {prefixes}；无上市日期列（list_date NULL）")


# ---- 东财 ETF 历史（fund_etf_hist_em；通道不可达时 skip） ----


def _fetch_etf_hist(symbol: str, start: str, end: str, adjust: str = ""):
    try:
        return _akshare().fund_etf_hist_em(
            symbol=symbol, period="daily",
            start_date=start, end_date=end, adjust=adjust,
        )
    except Exception as exc:
        _skip_if_channel_down(exc)
        raise  # unreachable（_skip_if_channel_down 要么 skip 要么 raise）


def test_etf_hist_columns_and_units():
    """fund_etf_hist_em 列完整性与单位口径（spike 样本 510300 近一月）。"""
    end = date.today()
    start = end - timedelta(days=40)
    df = _fetch_etf_hist(NORMAL_ETF, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    assert len(df) > 0, "510300 近一月不应为空"
    for col in EM_HIST_REQUIRED:
        assert col in df.columns, f"缺少必需列 {col}；实际列 {list(df.columns)}"
    rows = df.to_dict("records")
    for row in rows[:20]:
        # 单位口径断言：成交量/成交额/换手率非负；换手率为百分比数值（0~100 量级）
        assert row["成交量"] > 0
        assert row["成交额"] > 0
        assert 0 <= row["换手率"] <= 100, f"换手率非百分比量级: {row['换手率']}"
    dates = [str(r["日期"]) for r in rows]
    assert dates == sorted(dates), "行序应为升序"
    print(
        f"[观察] 510300 近一月 {len(df)} 行；样本行 {rows[0]}"
        f"（成交量单位手、成交额元、换手率百分比）"
    )


def test_etf_hist_full_history_rows_and_cap():
    """510300 全区间行数与上限距离（truncation_risk 阈值依据）。"""
    end = date.today()
    df = _fetch_etf_hist(NORMAL_ETF, "20120528", end.strftime("%Y%m%d"))
    assert len(df) > 2000, f"全历史应在 3000+ 行量级，实际 {len(df)}"
    assert len(df) < ETF_HIST_ROW_CAP, f"行数逼近上限 {ETF_HIST_ROW_CAP}（截断风险）"
    dates = [str(r["日期"]) for r in df.to_dict("records")]
    print(
        f"[观察] 510300 全历史 {len(df)} 行"
        f"（距 {ETF_HIST_ROW_CAP} 上限 {ETF_HIST_ROW_CAP - len(df)}）、"
        f"日期 {dates[0]}..{dates[-1]}"
    )


def test_etf_hist_adjust_raw_behavior():
    """adjust='' 不复权行为：raw 与 qfq 价格应不同（分红复权差异）。"""
    end = date.today()
    start = end - timedelta(days=40)
    raw = _fetch_etf_hist(NORMAL_ETF, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    qfq = _fetch_etf_hist(
        NORMAL_ETF, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), adjust="qfq"
    )
    assert len(raw) == len(qfq)
    raw_close = float(raw.iloc[0]["收盘"])
    qfq_close = float(qfq.iloc[0]["收盘"])
    print(f"[观察] 同日 raw close={raw_close} vs qfq close={qfq_close}")
    # 510300 历史上有分红：qfq 与 raw 首日价不同即证明 adjust='' 为不复权原始价
    if raw_close != qfq_close:
        print("    → 确认 adjust='' 为不复权原始价（qfq 已复权）")
    else:
        print("    → 区间内无除权事件（raw 与 qfq 重合，不能区分——加长区间复测）")


def test_etf_hist_delisted_etf_empty_or_data():
    """疑似退市 ETF（spike 样本 510181）区间行为观察（OQ3 证据）。"""
    end = date.today()
    df = _fetch_etf_hist(DELISTED_ETF, "20200101", end.strftime("%Y%m%d"))
    rows = 0 if df is None else len(df)
    print(f"[观察] 退市样本 {DELISTED_ETF} 自 2020 起 {rows} 行")
    if rows:
        dates = [str(r["日期"]) for r in df.to_dict("records")]
        print(f"    日期范围 {dates[0]}..{dates[-1]}（退市日后应无数据）")


# ---- Tushare fund_adj（无 Token skip，模式对齐 test_history_online_smoke） ----


@pytest.fixture(scope="module")
def tushare_token() -> str:
    config = load_config()
    if not config.has_tushare_token:
        pytest.skip("需要真实 Tushare Token（config.yaml -> tushare.token）")
    return config.tushare.token


def _fund_adj(token: str, ts_code: str, start: str, end: str) -> dict:
    import json

    import requests

    resp = requests.post(
        "http://api.tushare.pro",
        data=json.dumps(
            {
                "api_name": "fund_adj",
                "token": token,
                "params": {"ts_code": ts_code, "start_date": start, "end_date": end},
                "fields": "ts_code,trade_date,adj_factor",
            }
        ),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    body = resp.json()
    assert body.get("code") == 0, f"fund_adj 失败: {body.get('code')} {body.get('msg')}"
    data = body.get("data") or {}
    return {"fields": data.get("fields") or [], "items": data.get("items") or []}


def test_fund_adj_fields_and_factor_positive(tushare_token):
    """fund_adj 字段与因子正值校验（510300 近一年）。"""
    end = date.today()
    start = end - timedelta(days=400)
    result = _fund_adj(
        tushare_token, f"{NORMAL_ETF}.SH",
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
    )
    assert result["fields"] == ["ts_code", "trade_date", "adj_factor"]
    assert len(result["items"]) > 0, "510300 近一年应有复权因子行"
    for item in result["items"][:100]:
        assert item[2] is not None and float(item[2]) > 0, f"因子非正: {item}"
    print(f"[观察] fund_adj 510300 近一年 {len(result['items'])} 行")


def test_fund_adj_row_coverage_semantics(tushare_token):
    """行覆盖语义观察（OQ2：每日一行 vs 仅事件日）——factor_at 分支依据。

    近 400 自然日 ≈ 270 交易日。行数 ≥ 90% 交易日 → 每日一行（直接查表）；
    显著更少 → 仅除权事件日（需 forward-fill，只改 EtfDataService._factor_at 单点）。
    本用例打印观察并按证据断言当前实现分支是否正确，实测为另一语义时
    明确失败提示改 _factor_at。
    """
    end = date.today()
    span_days = 400
    start = end - timedelta(days=span_days)
    result = _fund_adj(
        tushare_token, f"{NORMAL_ETF}.SH",
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
    )
    rows = len(result["items"])
    approx_trade_days = int(span_days / 365 * 244)
    print(
        f"[观察] fund_adj 510300 近 {span_days} 天 {rows} 行 vs 约 "
        f"{approx_trade_days} 交易日（比率 {rows / approx_trade_days:.2f}）"
    )
    if rows >= approx_trade_days * 0.9:
        print("    → 每日一行语义：_factor_at 直接查表（当前实现）正确")
    elif rows < approx_trade_days * 0.5:
        pytest.fail(
            f"fund_adj 为仅事件日有行（{rows}/{approx_trade_days}）——"
            "EtfDataService._factor_at 需改为 forward-fill（单点修改 + 单测两分支）"
        )
    else:
        print("    → 中间形态（部分覆盖）：结合停牌样本人工判别，暂按每日一行口径")


def test_fund_adj_future_window_empty(tushare_token):
    """未来区间空结果行为（OQ3 证据：无异常 0 行）。"""
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=7)
    result = _fund_adj(
        tushare_token, f"{NORMAL_ETF}.SH",
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
    )
    assert len(result["items"]) == 0, "未来区间应为 0 行（合法空结果）"


# ---- get_etf_daily 端到端抽样比对（真实数据 → 临时 DuckDB → 三种复权） ----


def test_get_etf_daily_adjust_formulas_end_to_end(
    session_factory, tushare_token
):
    """端到端：东财日线 + fund_adj 因子真实数据落临时库，qfq/hfq 公式核验。

    公式（design D10/D11）：
    - hfq 价格 = raw × factor_t（当日因子）；
    - qfq 价格 = raw × factor_t ÷ factor_latest（全库最新因子）；
    - 复权只作用于价格字段，volume/amount/turnover_rate 不变。
    东财通道不可达时 skip（真实日线取不到则无法端到端）。
    """
    from app.models.instrument import Instrument
    from app.models.history_market import CnEtfBasic
    from app.models.history_fact import HISTORY_FACT_TABLES
    from app.services.market_session_service import now_beijing
    from app.services.quant.etf_data import EtfDataService

    # 真实拉取：近 400 自然日日线（含至少一次除权的概率高）与因子
    end = date.today()
    start = end - timedelta(days=400)
    hist = _fetch_etf_hist(NORMAL_ETF, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if len(hist) == 0:
        pytest.skip("东财返回空（通道异常形态）")
    factors = _fund_adj(
        tushare_token, f"{NORMAL_ETF}.SH",
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
    )["items"]

    instrument_id = f"CN:ETF:{NORMAL_ETF}"
    with session_factory() as session:
        session.add(Instrument(
            instrument_id=instrument_id, symbol=NORMAL_ETF,
            name="沪深300ETF", market="CN", asset_type="ETF",
            currency="CNY", exchange="SSE", is_active=True,
        ))
        session.flush()
        session.add(CnEtfBasic(
            instrument_id=instrument_id, ts_code=f"{NORMAL_ETF}.SH",
            symbol=NORMAL_ETF, name="沪深300ETF", exchange="SSE",
            list_date=date(2012, 5, 28),
            source="eastmoney", fetched_at=now_beijing(),
            source_last_seen_at=now_beijing(),
        ))
        hist_rows = []
        for row in hist.to_dict("records"):
            trade_day = date.fromisoformat(str(row["日期"]))
            hist_rows.append({
                "instrument_id": instrument_id, "ts_code": f"{NORMAL_ETF}.SH",
                "trade_date": trade_day,
                "open": float(row["开盘"]), "high": float(row["最高"]),
                "low": float(row["最低"]), "close": float(row["收盘"]),
                "volume": int(row["成交量"]), "amount": float(row["成交额"]),
                "turnover_rate": float(row["换手率"]),
                "source": "eastmoney", "fetched_at": now_beijing(),
            })
        session.execute(
            HISTORY_FACT_TABLES["etf_daily"].insert(), hist_rows
        )
        factor_rows = [
            {
                "instrument_id": instrument_id, "ts_code": f"{NORMAL_ETF}.SH",
                "trade_date": date.fromisoformat(
                    f"{str(it[1])[:4]}-{str(it[1])[4:6]}-{str(it[1])[6:8]}"
                ),
                "adj_factor": float(it[2]),
                "source": "tushare", "fetched_at": now_beijing(),
            }
            for it in factors
        ]
        if factor_rows:
            session.execute(
                HISTORY_FACT_TABLES["etf_adj_factor"].insert(), factor_rows
            )
        session.commit()

    # 查询区间取日线与因子的公共起点（因子晚于日线起步的早期日线日
    # 会触发 AdjustFactorMissingDaysError，端到端比对聚焦公共区间）
    if not factors:
        pytest.skip("fund_adj 无因子行（无法做复权端到端比对）")

    def _factor_day(item) -> date:
        text = str(item[1])
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))

    first_hist_day = min(
        date.fromisoformat(str(row["日期"])) for row in hist.to_dict("records")
    )
    q_start = max(first_hist_day, min(_factor_day(it) for it in factors))

    with session_factory() as session:
        service = EtfDataService(session)
        raw = service.get_etf_daily(NORMAL_ETF, q_start, end, adjust="raw")
        hfq = service.get_etf_daily(NORMAL_ETF, q_start, end, adjust="hfq")
        qfq = service.get_etf_daily(NORMAL_ETF, q_start, end, adjust="qfq")
    assert raw.items, "raw 查询应有数据"
    assert len(raw.items) == len(hfq.items) == len(qfq.items)

    factor_map = {r["trade_date"]: r["adj_factor"] for r in factor_rows}
    factor_latest = max(factor_map.values())
    for bar_raw, bar_hfq, bar_qfq in zip(raw.items, hfq.items, qfq.items):
        t = bar_raw.trade_date
        factor_t = factor_map.get(t)
        if factor_t is None:
            continue  # 缺失日已在 7.3 离线测试覆盖（此处仅比对有因子日）
        ratio_hfq = factor_t
        ratio_qfq = factor_t / factor_latest
        for field in ("open", "high", "low", "close"):
            raw_val, hfq_val, qfq_val = (
                getattr(bar_raw, field), getattr(bar_hfq, field),
                getattr(bar_qfq, field),
            )
            if raw_val is None:
                assert hfq_val is None and qfq_val is None
                continue
            assert hfq_val == pytest.approx(raw_val * ratio_hfq, rel=1e-9), (
                f"{t} {field} hfq 公式不符"
            )
            assert qfq_val == pytest.approx(raw_val * ratio_qfq, rel=1e-9), (
                f"{t} {field} qfq 公式不符"
            )
        # 复权只作用于价格字段
        assert bar_hfq.volume == bar_raw.volume
        assert bar_qfq.amount == bar_raw.amount
    print(
        f"[观察] 端到端 {len(raw.items)} 行核验通过；"
        f"factor_latest={factor_latest}"
    )
