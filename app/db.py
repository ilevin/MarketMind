"""数据库连接与初始化：DuckDB 自动确保数据目录；结构升级由 Alembic 负责。

并发模型（DuckDB 为嵌入式单写者数据库，乐观并发下同表并发写事务会冲突）：
- 生产部署单进程（uvicorn --workers 1），后台任务与 Web 请求共享同一进程与 Engine；
- 全部写事务经 ``write_coordinator`` 序列化提交（进程内 RLock + 有限重试）；
- 读路径不加锁（DuckDB MVCC 读不阻塞写）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def ensure_duckdb_dir(url: str) -> None:
    """duckdb:///./data/marketmind.duckdb 形式的路径：确保父目录存在，否则建库会失败。

    公开给 alembic/env.py 复用——迁移路径的 engine_from_config 不经
    create_db_engine，data/ 目录缺失时 duckdb.connect 会直接失败。
    """
    if not url.startswith("duckdb:///"):
        return
    db_path = url.removeprefix("duckdb:///")
    if db_path and db_path != ":memory:":
        parent = Path(db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: str):
    ensure_duckdb_dir(url)
    return create_engine(url)


class WriteCoordinator:
    """进程级写事务协调器（技术方案 §6.1）。

    DuckDB 乐观并发下同表并发写事务会冲突；单进程内以 RLock 把"开启事务到
    commit"的整段代码串行化，配合有限重试兜底残余冲突（如未来多连接场景）。
    锁粒度为整个写事务而非单条语句；网络请求不得放进锁内。
    """

    def __init__(self, retries: int = 3, backoff_seconds: float = 0.05):
        self._lock = threading.RLock()
        self._retries = max(1, retries)
        self._backoff_seconds = backoff_seconds

    @contextmanager
    def write(self):
        """写事务上下文：持锁执行，包裹"事务开启到 commit"的整段代码。"""
        with self._lock:
            yield

    def with_retry(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """写锁内执行 fn；失败按短暂退避重试（1~3 次），重试耗尽向上抛出原异常。

        fn 必须是可从头重放的完整写事务（内部自带 rollback/重新开始）。
        冲突异常的捕获范围由 Phase 0 验证（tasks 2.4）校准，当前按通用异常处理。
        """
        last_exc: BaseException | None = None
        for attempt in range(self._retries):
            with self._lock:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    logger.warning("写事务失败（第 %d/%d 次）：%s", attempt + 1, self._retries, exc)
            time.sleep(self._backoff_seconds * (attempt + 1))
        assert last_exc is not None
        raise last_exc


write_coordinator = WriteCoordinator()
"""模块级单例：全部写路径统一经此协调（database-persistence spec：写事务协调）。"""


def init_db(engine) -> None:
    """建表（幂等，仅测试使用）。生产路径的建表/升级由 Alembic 迁移负责（0001_duckdb_baseline）。模型必须先导入注册到 Base.metadata。"""
    import app.models  # noqa: F401  确保模型注册

    Base.metadata.create_all(bind=engine)
    logger.info("数据库表已就绪")


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def check_database(session: Session) -> bool:
    try:
        session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("数据库健康检查失败")
        return False
