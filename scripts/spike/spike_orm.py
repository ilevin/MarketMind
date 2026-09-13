"""Phase 0 验证 2.1：duckdb-sqlalchemy ORM CRUD、复合主键、TIMESTAMPTZ aware 往返。
以及 2.2 的外键部分：FK RESTRICT 语义与违反时的异常类型。

运行：.venv/bin/python scripts/spike/spike_orm.py
产物：stdout 的 PASS/FAIL 与结论行（回写 design.md Open Questions）。
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, create_engine, delete, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session

BEIJING = timezone(timedelta(hours=8))


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instrument"
    instrument_id = Column(String, primary_key=True)
    name = Column(String)


class Fundamental(Base):
    """模拟 fundamental_snapshot：复合主键 (instrument_id, trade_date)。"""

    __tablename__ = "fundamental_snapshot"
    instrument_id = Column(String, ForeignKey("instrument.instrument_id"), primary_key=True)
    trade_date = Column(String, primary_key=True)  # DATE 用 String 规避类型差异，单独验证 DATE
    pe_ttm = Column(Numeric(20, 6))
    fetched_at = Column(DateTime(timezone=True))


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="spike_orm_"))
    db_path = tmpdir / "orm.duckdb"
    engine = create_engine(f"duckdb:///{db_path}")
    print(f"[setup] 数据库: {db_path}")

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        results.append((name, ok, note))
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {note}" if note else ""))

    Base.metadata.create_all(engine)

    # --- 1. ORM CRUD + 复合主键 ---
    with Session(engine) as s:
        s.add(Instrument(instrument_id="cn:stock:600519", name="贵州茅台"))
        s.commit()
    with Session(engine) as s:
        row = s.get(Instrument, "cn:stock:600519")
        check("ORM 插入+主键查询", row is not None and row.name == "贵州茅台")

    with Session(engine) as s:
        s.execute(update(Instrument).where(Instrument.instrument_id == "cn:stock:600519").values(name="贵州茅台A股"))
        s.commit()
    with Session(engine) as s:
        row = s.get(Instrument, "cn:stock:600519")
        check("ORM 更新", row is not None and row.name == "贵州茅台A股")

    # 复合主键 CRUD
    aware_now = datetime.now(BEIJING)
    with Session(engine) as s:
        s.add(Fundamental(instrument_id="cn:stock:600519", trade_date="2026-09-11", pe_ttm=25.5, fetched_at=aware_now))
        s.commit()
    with Session(engine) as s:
        row = s.get(Fundamental, {"instrument_id": "cn:stock:600519", "trade_date": "2026-09-11"})
        check("复合主键 get", row is not None and float(row.pe_ttm) == 25.5)

    # 复合主键幂等：重复插入同键 → IntegrityError
    with Session(engine) as s:
        s.add(Fundamental(instrument_id="cn:stock:600519", trade_date="2026-09-11", pe_ttm=26.0))
        try:
            s.commit()
            check("复合主键冲突拒绝", False, "重复插入未报错！")
        except IntegrityError as e:
            check("复合主键冲突拒绝", True, f"IntegrityError: {type(e.orig).__name__}")

    # --- 2. TIMESTAMPTZ aware 往返 ---
    with Session(engine) as s:
        row = s.execute(
            select(Fundamental.fetched_at).where(Fundamental.instrument_id == "cn:stock:600519")
        ).scalar_one()
        ok = row.tzinfo is not None and row.astimezone(BEIJING) == aware_now.astimezone(BEIJING)
        check("TIMESTAMPTZ aware 往返", ok, f"写入 {aware_now!r} 读出 {row!r}（tzinfo={'有' if row.tzinfo else '无'}）")

    # UTC 偏移保真：写入 +08:00 时刻，读出时刻一致
    aware_utc = datetime(2026, 9, 11, 1, 30, 0, tzinfo=timezone.utc)  # = 北京 09:30
    with Session(engine) as s:
        s.execute(
            update(Fundamental).where(Fundamental.instrument_id == "cn:stock:600519").values(fetched_at=aware_utc)
        )
        s.commit()
        row = s.execute(
            select(Fundamental.fetched_at).where(Fundamental.instrument_id == "cn:stock:600519")
        ).scalar_one()
        check(
            "UTC 写入保真（不丢时刻）",
            row is not None and row.astimezone(timezone.utc) == aware_utc,
            f"读出 {row!r}",
        )

    # --- 3. FK RESTRICT ---
    with Session(engine) as s:
        try:
            s.execute(delete(Instrument).where(Instrument.instrument_id == "cn:stock:600519"))
            s.commit()
            check("FK RESTRICT 阻止删除被引用父行", False, "删除竟然成功了！")
        except SQLAlchemyError as e:
            orig = getattr(e, "orig", None)
            check(
                "FK RESTRICT 阻止删除被引用父行",
                True,
                f"{type(e).__name__}(orig={type(orig).__name__ if orig else None}): {str(e.orig if orig else e)[:160]}",
            )

    # 无引用时可正常删除（RESTRICT 不误伤）
    with Session(engine) as s:
        s.execute(delete(Fundamental).where(Fundamental.instrument_id == "cn:stock:600519"))
        s.commit()
        s.execute(delete(Instrument).where(Instrument.instrument_id == "cn:stock:600519"))
        s.commit()
        n = s.execute(func_count()).scalar()
        check("无引用时父行可删", n == 0)

    engine.dispose()
    print()
    print("=== 结论（回写 design.md）===")
    for name, ok, note in results:
        print(f"- [{'OK' if ok else 'NG'}] {name}: {note}")


def func_count():
    from sqlalchemy import func

    return func.count().select().select_from(Instrument)


if __name__ == "__main__":
    main()
