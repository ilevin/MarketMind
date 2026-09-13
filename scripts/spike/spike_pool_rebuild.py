"""Phase 0 验证 2.5：Engine 默认连接池行为与复杂表 rebuild migration 的事务语义。

验证点（design.md Open Question 4 + D2 迁移纪律）：
1. duckdb:// engine 的默认 pool 类型（QueuePool/SingletonThreadPool/NullPool？）；
2. 多线程并发从 engine 取连接是否安全（连接复用语义）；
3. rebuild migration（建新表 → INSERT SELECT 搬数据 → 校验 → DROP 旧表 → RENAME）在单事务内的原子性；
4. rebuild 中途失败（模拟校验失败抛错）→ 整体回滚，不残留半成品新表。

运行：.venv/bin/python scripts/spike/spike_pool_rebuild.py
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from sqlalchemy import create_engine, text


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="spike_pool_"))
    engine = create_engine(f"duckdb:///{tmpdir / 'pool.duckdb'}")

    # --- 1. 默认连接池类型 ---
    print(f"[1] engine.pool 类型: {type(engine.pool).__name__}")
    print(f"    engine.pool 大小参数: {engine.pool.size() if hasattr(engine.pool, 'size') else 'N/A'}")

    # 多线程并发借还连接（默认池下 4 线程并发 SELECT）
    errors: list = []

    def query() -> None:
        try:
            for _ in range(20):
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=query) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"    4 线程 × 20 次并发连接: {'PASS 无异常' if not errors else 'FAIL ' + str(errors[:2])}")

    # --- 2. rebuild migration 成功路径（单事务原子） ---
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE old_t (id INTEGER PRIMARY KEY, val VARCHAR)"))
        conn.execute(text("INSERT INTO old_t VALUES (1, 'a'), (2, 'b'), (3, 'c')"))

    rebuild_ok = False
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE new_t (id INTEGER PRIMARY KEY, val VARCHAR, extra INTEGER DEFAULT 0)"))
            conn.execute(text("INSERT INTO new_t (id, val) SELECT id, val FROM old_t"))
            moved = conn.execute(text("SELECT count(*) FROM new_t")).scalar()
            src = conn.execute(text("SELECT count(*) FROM old_t")).scalar()
            if moved != src:
                raise RuntimeError(f"行数校验失败: moved={moved} src={src}")
            conn.execute(text("DROP TABLE old_t"))
            conn.execute(text("ALTER TABLE new_t RENAME TO old_t"))
        with engine.connect() as conn:
            n = conn.execute(text("SELECT count(*) FROM old_t")).scalar()
            leftover = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('new_t', 'old_t')")
            ).scalar()
        rebuild_ok = n == 3 and leftover == 1
        print(f"[2] rebuild 成功路径: count={n}, tables={leftover}  {'PASS' if rebuild_ok else 'FAIL'}")
    except Exception as e:  # noqa: BLE001
        print(f"[2] FAIL rebuild 成功路径异常: {type(e).__name__}: {e}")

    # --- 3. rebuild 失败路径（校验失败 → 整体回滚） ---
    fail_rollback_ok = False
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE new_t2 (id INTEGER PRIMARY KEY)"))
            conn.execute(text("INSERT INTO new_t2 SELECT id FROM old_t"))
            # 模拟业务键校验失败
            raise RuntimeError("业务键校验失败（模拟）")
    except RuntimeError:
        pass  # 预期失败
    except Exception as e:  # noqa: BLE001
        print(f"    （非预期异常类型: {type(e).__name__}: {e}）")
    try:
        with engine.connect() as conn:
            n_new = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_name = 'new_t2'")
            ).scalar()
            n_old = conn.execute(text("SELECT count(*) FROM old_t")).scalar()
        fail_rollback_ok = n_new == 0 and n_old == 3
        print(f"[3] rebuild 失败回滚: new_t2 存在={bool(n_new)}, old_t 行数={n_old}  "
              f"{'PASS 原子回滚' if fail_rollback_ok else 'FAIL 残留半成品！'}")
    except Exception as e:  # noqa: BLE001
        print(f"[3] FAIL 检查回滚时异常: {type(e).__name__}: {e}")

    # --- 4. 单独验证 DROP + RENAME 语法 ---
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE tmp_x (a INTEGER)"))
            conn.execute(text("DROP TABLE tmp_x"))
        print("[4] CREATE/DROP TABLE in transaction: PASS")
    except Exception as e:  # noqa: BLE001
        print(f"[4] FAIL CREATE/DROP TABLE in transaction: {type(e).__name__}: {e}")

    print()
    print("=== 结论（回写 design.md Open Question 4）===")
    print(f"- 默认连接池: {type(engine.pool).__name__}（决定是否需要显式 NullPool/单连接）")
    print(f"- rebuild 原子性: {'PASS' if rebuild_ok else 'FAIL'} / 失败回滚: {'PASS' if fail_rollback_ok else 'FAIL'}")


if __name__ == "__main__":
    main()
