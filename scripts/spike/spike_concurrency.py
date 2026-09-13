"""Phase 0 验证 2.4：同进程多连接并发写的事务冲突行为（冲突异常类型与粒度）。

验证点（design.md Open Question 2，校准 WriteCoordinator 重试的捕获范围）：
- 场景 A：两个连接并发写同表不同行 —— 是否冲突；
- 场景 B：两个连接并发写同表同一行 —— 是否冲突；
- 场景 C：一个连接长写事务进行中，另一连接提交写 —— 冲突异常的确切类型；
- 场景 D：写事务进行中另一连接读（MVCC 读不阻塞写）。

运行：.venv/bin/python scripts/spike/spike_concurrency.py
"""

from __future__ import annotations

import tempfile
import threading
import time
import traceback
from pathlib import Path

from sqlalchemy import Column, Integer, String, create_engine, text


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="spike_conc_"))
    engine = create_engine(f"duckdb:///{tmpdir / 'conc.duckdb'}")

    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, val VARCHAR)"))
        conn.execute(text("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c'), (4, 'd')"))

    def writer(name: str, row_id: int, val: str, hold: float, out: list) -> None:
        """独立连接 + 独立事务写一行，hold 秒后再 commit（制造并发窗口）。"""
        try:
            conn = engine.connect()
            trans = conn.begin()
            conn.execute(text("UPDATE t SET val = :v WHERE id = :i"), {"v": val, "i": row_id})
            time.sleep(hold)
            trans.commit()
            conn.close()
            out.append((name, "OK", None))
        except Exception as e:  # noqa: BLE001
            out.append((name, f"{type(e).__name__}", f"orig={type(getattr(e, 'orig', None)).__name__ if getattr(e, 'orig', None) else None} {str(e)[:200]}"))
            traceback.print_exc()

    print("=== 场景 A：并发写同表不同行（0.3s 重叠窗口）===")
    out: list = []
    t1 = threading.Thread(target=writer, args=("w1", 1, "a1", 0.3, out))
    t2 = threading.Thread(target=writer, args=("w2", 2, "b1", 0.3, out))
    t1.start(); time.sleep(0.05); t2.start(); t1.join(); t2.join()
    for r in out:
        print(f"  {r}")
    conflict_a = any(r[1] != "OK" for r in out)

    print("=== 场景 B：并发写同表同一行（0.3s 重叠窗口）===")
    out_b: list = []
    t1 = threading.Thread(target=writer, args=("w1", 3, "c1", 0.3, out_b))
    t2 = threading.Thread(target=writer, args=("w2", 3, "c2", 0.3, out_b))
    t1.start(); time.sleep(0.05); t2.start(); t1.join(); t2.join()
    for r in out_b:
        print(f"  {r}")
    conflict_b = any(r[1] != "OK" for r in out_b)

    print("=== 场景 C：长写事务进行中，另一连接提交写 ===")
    out_c: list = []
    t1 = threading.Thread(target=writer, args=("slow", 4, "d1", 1.0, out_c))
    t1.start(); time.sleep(0.2)
    # 快写者尝试在慢写者持有写事务期间提交
    writer("fast", 4, "d2", 0.0, out_c)
    t1.join()
    for r in out_c:
        print(f"  {r}")
    conflict_c = any(r[1] != "OK" for r in out_c)

    print("=== 场景 D：写事务进行中另一连接读（MVCC）===")
    read_result: list = []

    def reader() -> None:
        try:
            conn = engine.connect()
            rows = conn.execute(text("SELECT count(*) FROM t")).scalar()
            conn.close()
            read_result.append(("read-during-write", "OK", f"count={rows}"))
        except Exception as e:  # noqa: BLE001
            read_result.append(("read-during-write", type(e).__name__, str(e)[:200]))

    out_d: list = []
    t1 = threading.Thread(target=writer, args=("w", 1, "a2", 0.5, out_d))
    t1.start(); time.sleep(0.1)
    reader()
    t1.join()
    for r in read_result + out_d:
        print(f"  {r}")

    print()
    print("=== 结论（回写 design.md Open Question 2）===")
    print(f"- 不同行并发写: {'冲突' if conflict_a else '不冲突'}")
    print(f"- 同一行并发写: {'冲突' if conflict_b else '不冲突'}")
    print(f"- 长事务期间另提交写: {'冲突' if conflict_c else '不冲突'}")
    if conflict_a or conflict_b or conflict_c:
        types = {r[1] for r in out + out_b + out_c if r[1] != "OK"}
        print(f"- 冲突异常类型: {types}（WriteCoordinator 的捕获范围据此校准）")
    print(f"- 写期间读: {read_result[0][1] if read_result else '未执行'}（MVCC 读不阻塞写验证）")


if __name__ == "__main__":
    main()
