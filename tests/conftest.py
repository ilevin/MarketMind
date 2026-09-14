"""tests 共享 fixture：DuckDB 临时文件库（全部 DB 相关测试的统一入口）。

设计（design.md D10 / tasks 9.1 + multi-user-auth tasks 10.1）：
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
- multi-user-auth：``login_as="用户名"`` 走真实登录（UserService 幂等建号
  -> POST /api/auth/login -> Set-Cookie），返回 AuthedClient 包装——
  写请求自动注入 X-CSRF-Token，存量测试的调用形态不变；不传则匿名。
- 登录限速器为进程级单例：每个测试前后清零，避免跨测试的 IP+username
  失败计数串扰（一个测试故意失败 5 次后，后续测试的登录 429）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import create_db_engine, init_db, make_session_factory

DEFAULT_TEST_PASSWORD = "password123"


class AuthedClient:
    """已登录 TestClient 包装：写请求自动注入 X-CSRF-Token，其余能力透传。

    存量 API 测试的调用形态（client.post/get/put/delete、client.app.state）
    保持不变；CSRF 头由包装层注入（token 取自该用户当前有效 Session）。
    """

    UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, client: TestClient, csrf_token: str):
        self._client = client
        self._csrf_token = csrf_token

    def _inject(self, method: str, headers):
        headers = dict(headers or {})
        headers.setdefault("X-CSRF-Token", self._csrf_token)
        return headers

    def get(self, url, **kwargs):
        return self._client.get(url, **kwargs)

    def post(self, url, headers=None, **kwargs):
        return self._client.post(url, headers=self._inject("POST", headers), **kwargs)

    def put(self, url, headers=None, **kwargs):
        return self._client.put(url, headers=self._inject("PUT", headers), **kwargs)

    def patch(self, url, headers=None, **kwargs):
        return self._client.patch(url, headers=self._inject("PATCH", headers), **kwargs)

    def delete(self, url, headers=None, **kwargs):
        return self._client.delete(url, headers=self._inject("DELETE", headers), **kwargs)

    def request(self, method: str, url, headers=None, **kwargs):
        if method.upper() in self.UNSAFE_METHODS:
            headers = self._inject(method.upper(), headers)
        return self._client.request(method, url, headers=headers, **kwargs)

    def __getattr__(self, name):
        # app.state / cookies / headers 等属性透传
        return getattr(self._client, name)

    def __enter__(self):
        self._client.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._client.__exit__(*exc_info)


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """登录限速单例按测试隔离（失败计数不跨测试累积）。"""
    from app.auth.rate_limit import login_rate_limiter

    login_rate_limiter.clear()
    yield
    login_rate_limiter.clear()


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
def user_factory(session_factory):
    """经 UserService 直接建用户（幂等）：返回 (username, user_id)。

    供隔离 / 管理类测试在 HTTP 之外准备用户（client_factory 的 login_as
    内部也用它）。
    """

    def _make(username: str, password: str = DEFAULT_TEST_PASSWORD, role: str = "user"):
        from app.services.user_service import UserNotFoundError, UserService

        with session_factory() as s:
            service = UserService(s)
            try:
                user = service.get_by_username(username)
            except UserNotFoundError:
                user = service.create_user(username=username, password=password, role=role)
            return {"username": user.username, "user_id": user.user_id, "role": user.role}

    return _make


def _current_csrf_token(session_factory, username: str) -> str:
    """取该用户最新有效 Session 的 csrf_token（conftest 与 app 共用同一 factory）。"""
    from app.models import AppUser, UserSession

    with session_factory() as s:
        return s.execute(
            select(UserSession.csrf_token)
            .join(AppUser, AppUser.user_id == UserSession.user_id)
            .where(AppUser.username == username, UserSession.revoked_at.is_(None))
            .order_by(UserSession.created_at.desc())
        ).scalar_one()


@pytest.fixture()
def client_factory(session_factory, duckdb_url, user_factory):
    """TestClient 工厂：注入共享 session_factory 与假 name_provider。

    - ``login_as=None``：裸 TestClient（匿名）；
    - ``login_as="alice"``（可选 ``role``）：幂等建号 + 真实登录，
      返回 AuthedClient（写请求自动带 X-CSRF-Token）。
    """

    def _make(name_provider, *, login_as=None, role="user"):
        from app.config import AppConfig, DatabaseConfig
        from app.main import create_app

        config = AppConfig(database=DatabaseConfig(url=duckdb_url))
        app = create_app(config)
        app.state.session_factory = session_factory
        app.state.name_provider = name_provider
        client = TestClient(app)
        if login_as is None:
            return client
        user_factory(login_as, role=role)
        resp = client.post(
            "/api/auth/login", json={"username": login_as, "password": DEFAULT_TEST_PASSWORD}
        )
        assert resp.status_code == 200, f"测试登录失败: {resp.status_code} {resp.text}"
        return AuthedClient(client, _current_csrf_token(session_factory, login_as))

    return _make
