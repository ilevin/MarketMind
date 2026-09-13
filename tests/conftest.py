"""tests 共享 fixture：DuckDB 临时文件库（全部 DB 相关测试的统一入口）。

设计（design.md D10 / tasks 9.1）：
- DuckDB ``:memory:`` 每个连接是独立实例，无法像 SQLite 内存库那样跨 Session
  共享，统一改用 ``tmp_path`` 临时文件库（每个测试独立文件，互不污染）。
- 建表走 ``init_db``（create_all）：生产由 Alembic 负责，迁移正确性由
  ``test_migrations`` 单独验证；其余测试只关心 schema 语义。
- ``session_factory`` 与生产 ``app.db.make_session_factory`` 同参
  （``autoflush=False, expire_on_commit=False``）。
- ``client_factory`` 供 integration 测试：建 app 并替换 session_factory /
  name_provider（lifespan 不会重建这两项）。refresh_service /
  session_service 等会被 lifespan 挂载真实实例的 state，由测试在
  TestClient with 块内覆盖为假件（沿用原模式）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import create_db_engine, init_db, make_session_factory


@pytest.fixture()
def duckdb_path(tmp_path):
    """每个测试独立的 DuckDB 数据库文件路径。"""
    return tmp_path / "test.duckdb"


@pytest.fixture()
def duckdb_url(duckdb_path) -> str:
    return f"duckdb:///{duckdb_path}"


@pytest.fixture()
def engine(duckdb_url):
    eng = create_db_engine(duckdb_url)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture()
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture()
def client_factory(session_factory, duckdb_url):
    """TestClient 工厂：建 app 并注入共享 session_factory 与假 name_provider。"""

    def _make(name_provider):
        from app.config import AppConfig, DatabaseConfig
        from app.main import create_app

        config = AppConfig(database=DatabaseConfig(url=duckdb_url))
        app = create_app(config)
        app.state.session_factory = session_factory
        app.state.name_provider = name_provider
        return TestClient(app)

    return _make
