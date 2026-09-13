"""Phase 0 验证 2.3：INSERT ... ON CONFLICT DO UPDATE（含 SQLAlchemy 方言 on_conflict_do_update 可用性）。

验证点（design.md Open Question 1，决定 QuoteSnapshotRepository.upsert 首选/兜底方案）：
1. 原生 SQL `INSERT ... ON CONFLICT (pk) DO UPDATE SET ...` 在 DuckDB 的行为；
2. SQLAlchemy `sqlalchemy.dialects.postgresql.insert().on_conflict_do_update(...)`（duckdb-sqlalchemy 基于 PG 方言）；
3. 单列主键（quote_snapshot）与复合主键（fundamental_snapshot）两种形态；
4. upsert 幂等：两次写入同一键后行数恒为 1、值为最新。

运行：.venv/bin/python scripts/spike/spike_upsert.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import Column, MetaData, Numeric, String, Table, create_engine, text
from sqlalchemy.dialects import postgresql


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="spike_upsert_"))
    engine = create_engine(f"duckdb:///{tmpdir / 'upsert.duckdb'}")

    metadata = MetaData()
    quote = Table(
        "quote_snapshot",
        metadata,
        Column("instrument_id", String, primary_key=True),
        Column("price", Numeric(20, 6)),
        Column("previous_close", Numeric(20, 6)),
    )
    fundamental = Table(
        "fundamental_snapshot",
        metadata,
        Column("instrument_id", String, primary_key=True),
        Column("trade_date", String, primary_key=True),
        Column("pe_ttm", Numeric(20, 6)),
    )
    metadata.create_all(engine)

    # --- 1. 原生 SQL ON CONFLICT（单列主键） ---
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO quote_snapshot (instrument_id, price, previous_close) "
            "VALUES ('cn:stock:600519', 1500.5, 1490.0) "
            "ON CONFLICT (instrument_id) DO UPDATE SET price = excluded.price, previous_close = excluded.previous_close"
        ))
        conn.execute(text(
            "INSERT INTO quote_snapshot (instrument_id, price, previous_close) "
            "VALUES ('cn:stock:600519', 1510.25, 1500.5) "
            "ON CONFLICT (instrument_id) DO UPDATE SET price = excluded.price, previous_close = excluded.previous_close"
        ))
        rows = conn.execute(text("SELECT instrument_id, price FROM quote_snapshot")).all()
    ok1 = len(rows) == 1 and float(rows[0][1]) == 1510.25
    print(f"[1] 原生 SQL ON CONFLICT（单列主键）: {rows}  {'PASS' if ok1 else 'FAIL'}")

    # --- 2. 方言 on_conflict_do_update（单列主键） ---
    try:
        stmt = postgresql.insert(quote).values(
            instrument_id="cn:stock:600519", price=1520.0, previous_close=1510.25
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[quote.c.instrument_id],
            set_={"price": stmt.excluded.price, "previous_close": stmt.excluded.previous_close},
        )
        with engine.begin() as conn:
            conn.execute(stmt)
            n = conn.execute(text("SELECT count(*), max(price) FROM quote_snapshot")).one()
        ok2 = n[0] == 1 and float(n[1]) == 1520.0
        print(f"[2] postgresql.insert().on_conflict_do_update（单列主键）: {tuple(n)}  {'PASS' if ok2 else 'FAIL'}")
    except Exception as e:  # noqa: BLE001
        ok2 = False
        print(f"[2] FAIL 方言 on_conflict_do_update 不可用: {type(e).__name__}: {e}")

    # --- 3. 方言 on_conflict_do_update（复合主键） ---
    try:
        stmt = postgresql.insert(fundamental).values(
            instrument_id="cn:stock:600519", trade_date="2026-09-11", pe_ttm=25.5
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[fundamental.c.instrument_id, fundamental.c.trade_date],
            set_={"pe_ttm": stmt.excluded.pe_ttm},
        )
        with engine.begin() as conn:
            conn.execute(stmt)
            conn.execute(stmt.values(pe_ttm=26.75))  # type: ignore[union-attr]
            n = conn.execute(text("SELECT count(*), max(pe_ttm) FROM fundamental_snapshot")).one()
        ok3 = n[0] == 1 and float(n[1]) == 26.75
        print(f"[3] on_conflict_do_update（复合主键）: {tuple(n)}  {'PASS' if ok3 else 'FAIL'}")
    except Exception as e:  # noqa: BLE001
        ok3 = False
        print(f"[3] FAIL 复合主键 on_conflict_do_update 不可用: {type(e).__name__}: {e}")

    # --- 4. ON CONFLICT DO NOTHING（trading_calendar 幂等场景） ---
    try:
        with engine.begin() as conn:
            for _ in range(3):
                conn.execute(text(
                    "INSERT INTO fundamental_snapshot (instrument_id, trade_date, pe_ttm) "
                    "VALUES ('hk:stock:00700', '2026-09-11', 30.0) "
                    "ON CONFLICT DO NOTHING"
                ))
            n = conn.execute(text("SELECT count(*) FROM fundamental_snapshot WHERE instrument_id='hk:stock:00700'")).scalar()
        ok4 = n == 1
        print(f"[4] ON CONFLICT DO NOTHING（无目标列）: count={n}  {'PASS' if ok4 else 'FAIL'}")
    except Exception as e:  # noqa: BLE001
        ok4 = False
        print(f"[4] FAIL ON CONFLICT DO NOTHING: {type(e).__name__}: {e}")

    print()
    print("=== 结论（回写 design.md Open Question 1）===")
    print(f"- 原生 SQL ON CONFLICT DO UPDATE: {'可用' if ok1 else '不可用'}")
    print(f"- 方言 on_conflict_do_update 单列/复合主键: {'可用' if ok2 and ok3 else '不可用'}")
    print(f"- ON CONFLICT DO NOTHING: {'可用' if ok4 else '不可用'}")
    if ok2 and ok3:
        print("→ QuoteSnapshotRepository.upsert 采用首选方案：Core on_conflict_do_update")


if __name__ == "__main__":
    main()
