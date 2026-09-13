"""Phase 0 验证 2.2：CREATE SEQUENCE + DEFAULT nextval 与 Alembic DDL 渲染。

验证点（design.md Open Question 5）：
1. 手工 CREATE SEQUENCE + 列 DEFAULT nextval('seq_tag_id') 后，ORM 不带主键值插入能否自动取号；
2. SQLAlchemy 模型侧 Sequence('seq_tag_id') 的 DDL 渲染（create_all 与 alembic op.create_sequence）；
3. 两个连续插入的 tag_id 递增。

运行：.venv/bin/python scripts/spike/spike_sequence.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import Column, Integer, Sequence, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


def main() -> None:
    # duckdb-sqlalchemy 无内建 Alembic 支持：注册默认 impl（与 alembic/env.py 同法）
    from alembic.ddl.impl import DefaultImpl

    class DuckDBImpl(DefaultImpl):
        __dialect__ = "duckdb"
        transactional_ddl = True

    tmpdir = Path(tempfile.mkdtemp(prefix="spike_seq_"))
    engine = create_engine(f"duckdb:///{tmpdir / 'seq.duckdb'}")

    # --- 路线 A：手工 DDL（模拟基线迁移 op.execute 的产物） ---
    with engine.begin() as conn:
        conn.execute(text("CREATE SEQUENCE seq_tag_id START 1"))
        conn.execute(text("""
            CREATE TABLE tag (
                tag_id BIGINT DEFAULT nextval('seq_tag_id') PRIMARY KEY,
                name VARCHAR NOT NULL UNIQUE
            )
        """))

    with Session(engine) as s:
        s.execute(text("INSERT INTO tag (name) VALUES ('高股息')"))
        s.execute(text("INSERT INTO tag (name) VALUES ('科技')"))
        s.commit()
        rows = s.execute(text("SELECT tag_id, name FROM tag ORDER BY tag_id")).all()
    print(f"[A] 手工 DDL + DEFAULT nextval 插入: {rows}")
    ok_a = [r[0] for r in rows] == [1, 2]
    print(f"    {'PASS' if ok_a else 'FAIL'} 自动取号递增（tag_id 应为 [1, 2]）")

    # --- 路线 B：SQLAlchemy 模型 Sequence + create_all 渲染 ---
    class TagModel(Base):
        __tablename__ = "tag_model"
        tag_id = Column(Integer, Sequence("seq_tag_model"), primary_key=True)
        name = Column(String(50), nullable=False)

    engine_b = create_engine(f"duckdb:///{tmpdir / 'seq_b.duckdb'}")
    Base.metadata.create_all(engine_b)
    # create_all 是否为模型渲染了 sequence + DEFAULT nextval？
    with engine_b.connect() as conn:
        cols = conn.execute(text(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_name = 'tag_model' AND column_name = 'tag_id'"
        )).all()
        seqs = conn.execute(text(
            "SELECT sequence_name FROM duckdb_sequences()"
        )).all()
    print(f"[B] create_all 渲染: 列默认={cols} sequences={seqs}")

    with Session(engine_b) as s:
        t1 = TagModel(name="a")
        t2 = TagModel(name="b")
        s.add_all([t1, t2])
        s.commit()
        print(f"    ORM 插入后 tag_id: {t1.tag_id}, {t2.tag_id}")
    ok_b = t1.tag_id is not None and t2.tag_id is not None and t2.tag_id > t1.tag_id
    print(f"    {'PASS' if ok_b else 'FAIL'} 模型侧 Sequence 经 create_all 可用")

    # --- 路线 C：alembic op.create_sequence + op.create_table 渲染 ---
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine_c = create_engine(f"duckdb:///{tmpdir / 'seq_c.duckdb'}")
    with engine_c.connect() as conn:
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        op.execute("CREATE SEQUENCE seq_tag_alembic START 1")
        op.create_table(
            "tag_alembic",
            Column("tag_id", Integer, Sequence("seq_tag_alembic"), primary_key=True),
            Column("name", String(50), nullable=False),
        )
        defaults = conn.execute(text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'tag_alembic' AND column_name = 'tag_id'"
        )).scalar()
        seq_names = conn.execute(text("SELECT sequence_name FROM duckdb_sequences()")).all()
    print(f"[C] alembic op.create_sequence: 列默认={defaults!r} sequences={seq_names}")
    ok_c = defaults is not None and "nextval" in str(defaults)
    print(f"    {'PASS' if ok_c else 'FAIL'} alembic op 渲染 DEFAULT nextval")

    print()
    print("=== 结论（回写 design.md Open Question 5）===")
    print(f"- 手工 DDL 路线: {'可用' if ok_a else '不可用'}")
    print(f"- 模型 Sequence + create_all: {'可用' if ok_b else '不可用'}（注意 create_all 产物是否带 DEFAULT，需与基线迁移对齐）")
    print(f"- alembic op.create_sequence: {'可用' if ok_c else '不可用'}")


if __name__ == "__main__":
    main()
