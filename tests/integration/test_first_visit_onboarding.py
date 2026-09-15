"""首次访问初始化集成测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import select

from app.auth.password import PLACEHOLDER_HASH, verify_password
from app.models import (
    AppUser,
    IndexWatchlist,
    Instrument,
    Tag,
    Watchlist,
    WatchlistTag,
)
from app.services.auth_service import AuthService

from tests.integration.test_auth_api import FakeNameProvider


def test_setup_creates_named_admin_and_session(client_factory, session_factory):
    """首次访问可自定义首用户名称，成功后自动登录并拥有管理员权限。"""
    with client_factory(FakeNameProvider()) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
        assert client.get("/login", follow_redirects=False).headers["location"] == "/setup"
        assert client.get("/setup").status_code == 200
        assert client.get("/static/style.css").status_code == 200

        response = client.post(
            "/api/auth/setup",
            json={
                "username": "first-owner",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"username": "first-owner", "role": "admin"}
        cookie = response.headers["set-cookie"].lower()
        assert "marketmind_session=" in cookie
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "path=/" in cookie
        assert client.get("/api/auth/me").json()["username"] == "first-owner"
        assert client.get("/admin/users").status_code == 200
        # 首页在初始化后必须能完整读取其并行加载的三组数据。
        assert client.get("/").status_code == 200
        assert client.get("/api/tags").status_code == 200
        assert client.get("/api/quotes").status_code == 200
        assert client.get("/api/indices").status_code == 200

    with session_factory() as session:
        user = session.query(AppUser).one()
        assert user.username == "first-owner"
        assert user.role == "admin"
        assert user.is_active is True
        assert verify_password("password123", user.password_hash)


def test_setup_cookie_honors_secure_config(client_factory):
    """HTTPS 部署配置启用时，setup 与登录一样下发 Secure Session Cookie。"""
    with client_factory(FakeNameProvider()) as client:
        client.app.state.config.auth.session.cookie_secure = True
        response = client.post(
            "/api/auth/setup",
            json={
                "username": "secure-owner",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()


def test_setup_rejects_extra_fields_and_repeat_setup(client_factory):
    with client_factory(FakeNameProvider()) as client:
        response = client.post(
            "/api/auth/setup",
            json={
                "username": "owner",
                "password": "password123",
                "password_confirmation": "password123",
                "role": "user",
            },
        )
        assert response.status_code == 422

        response = client.post(
            "/api/auth/setup",
            json={
                "username": "owner",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 200

        response = client.post(
            "/api/auth/setup",
            json={
                "username": "another-owner",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 409
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_setup_validates_password_and_username(client_factory):
    with client_factory(FakeNameProvider()) as client:
        for payload in (
            {"username": "ab", "password": "password123", "password_confirmation": "password123"},
            {"username": "owner", "password": "short", "password_confirmation": "short"},
            {"username": "owner", "password": "password123", "password_confirmation": "different"},
        ):
            response = client.post("/api/auth/setup", json=payload)
            assert response.status_code == 422


def test_placeholder_owner_is_claimed_with_custom_username(
    client_factory, session_factory
):
    with session_factory() as session:
        user = AppUser(
            username="admin",
            password_hash=PLACEHOLDER_HASH,
            role="admin",
            is_active=True,
            must_change_password=True,
        )
        session.add(user)
        session.commit()
        user_id = user.user_id

    with client_factory(FakeNameProvider()) as client:
        response = client.post(
            "/api/auth/setup",
            json={
                "username": "migrated-owner",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 200
        assert response.json()["username"] == "migrated-owner"

    with session_factory() as session:
        user = session.get(AppUser, user_id)
        assert user.username == "migrated-owner"
        assert user.user_id == user_id
        assert user.password_hash != PLACEHOLDER_HASH
        assert user.must_change_password is False


def test_placeholder_claim_preserves_all_private_data(client_factory, session_factory):
    """占位 owner 改名后 user_id 外键不变，四张私有表数据仍可由新会话读取。"""
    with session_factory() as session:
        owner = AppUser(
            username="admin",
            password_hash=PLACEHOLDER_HASH,
            role="admin",
            is_active=True,
            must_change_password=True,
        )
        stock = Instrument(
            instrument_id="CN:STOCK:600519",
            symbol="600519",
            name="贵州茅台",
            market="CN",
            asset_type="STOCK",
        )
        index = Instrument(
            instrument_id="CN:INDEX:000001",
            symbol="000001",
            name="上证指数",
            market="CN",
            asset_type="INDEX",
        )
        session.add_all([owner, stock, index])
        session.flush()
        tag = Tag(user_id=owner.user_id, name="长期持有")
        session.add_all(
            [
                Watchlist(
                    user_id=owner.user_id,
                    instrument_id=stock.instrument_id,
                    sort_order=7,
                ),
                IndexWatchlist(
                    user_id=owner.user_id,
                    instrument_id=index.instrument_id,
                    sort_order=3,
                ),
                tag,
            ]
        )
        session.flush()
        session.add(
            WatchlistTag(
                user_id=owner.user_id,
                instrument_id=stock.instrument_id,
                tag_id=tag.tag_id,
            )
        )
        session.commit()
        owner_id = owner.user_id
        tag_id = tag.tag_id

    with client_factory(FakeNameProvider()) as client:
        response = client.post(
            "/api/auth/setup",
            json={
                "username": "portfolio-owner",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 200
        assert client.get("/api/watchlist").json()["items"] == [
            {
                "instrument_id": "CN:STOCK:600519",
                "symbol": "600519",
                "name": "贵州茅台",
                "market": "CN",
                "asset_type": "STOCK",
                "sort_order": 7,
                "tags": [{"id": tag_id, "name": "长期持有"}],
            }
        ]
        assert client.get("/api/index-watchlist").json()["items"] == [
            {
                "instrument_id": "CN:INDEX:000001",
                "symbol": "000001",
                "name": "上证指数",
                "market": "CN",
                "asset_type": "INDEX",
                "sort_order": 3,
                "tags": [],
            }
        ]
        assert client.get("/api/tags").json()["items"] == [
            {"id": tag_id, "name": "长期持有", "usage_count": 1}
        ]

    with session_factory() as session:
        assert session.scalar(select(Watchlist.user_id)) == owner_id
        assert session.scalar(select(IndexWatchlist.user_id)) == owner_id
        assert session.scalar(select(Tag.user_id)) == owner_id
        assert session.scalar(select(WatchlistTag.user_id)) == owner_id


def test_setup_rate_limit_is_per_ip_and_runs_before_hashing(client_factory, monkeypatch):
    """更换用户名不能绕过同 IP 配额，429 在昂贵 Argon2 哈希前返回。"""
    with client_factory(FakeNameProvider()) as client:
        for index in range(5):
            response = client.post(
                "/api/auth/setup",
                json={
                    "username": f"owner-{index}",
                    "password": "short",
                    "password_confirmation": "short",
                },
            )
            assert response.status_code == 422

        def fail_if_called(_password):
            raise AssertionError("限速请求不应执行 Argon2 哈希")

        monkeypatch.setattr("app.services.auth_service.hash_password", fail_if_called)
        response = client.post(
            "/api/auth/setup",
            json={
                "username": "different-owner",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 429


def test_placeholder_claim_rejects_existing_username_without_mutation(
    client_factory, session_factory
):
    """占位账户认领时用户名冲突返回 409，既有账户和占位账户均不变。"""
    with session_factory() as session:
        session.add_all(
            [
                AppUser(
                    username="admin",
                    password_hash=PLACEHOLDER_HASH,
                    role="admin",
                    is_active=True,
                    must_change_password=True,
                ),
                AppUser(
                    username="taken-name",
                    password_hash="existing-hash",
                    role="user",
                    is_active=True,
                    must_change_password=False,
                ),
            ]
        )
        session.commit()

    with client_factory(FakeNameProvider()) as client:
        response = client.post(
            "/api/auth/setup",
            json={
                "username": "TAKEN-NAME",
                "password": "password123",
                "password_confirmation": "password123",
            },
        )
        assert response.status_code == 409

    with session_factory() as session:
        users = {
            user.username: (user.password_hash, user.role, user.must_change_password)
            for user in session.scalars(select(AppUser)).all()
        }
        assert users == {
            "admin": (PLACEHOLDER_HASH, "admin", True),
            "taken-name": ("existing-hash", "user", False),
        }


def test_setup_rolls_back_user_when_session_creation_fails(
    client_factory, session_factory, monkeypatch, caplog
):
    """Session 写入失败时用户创建与占位认领均不得部分提交。"""
    def fail_create(*_args, **_kwargs):
        raise RuntimeError("injected-session-failure")

    monkeypatch.setattr(
        "app.repositories.user_session.UserSessionRepository.create", fail_create
    )
    with client_factory(FakeNameProvider()) as client:
        response = client.post(
            "/api/auth/setup",
            json={
                "username": "owner",
                "password": "secret-pass-123",
                "password_confirmation": "secret-pass-123",
            },
        )
        assert response.status_code == 500
        assert response.json() == {"detail": "初始化失败，请稍后重试"}
        assert "secret-pass-123" not in caplog.text
        assert "injected-session-failure" not in caplog.text
        assert "password_hash" not in caplog.text

    with session_factory() as session:
        assert session.scalars(select(AppUser)).all() == []


def test_concurrent_setup_creates_only_one_admin(
    client_factory, session_factory, monkeypatch
):
    """两个 HTTP 请求同时完成哈希后，只有一个返回 200，另一个返回 409。"""
    import app.services.auth_service as auth_service_module

    original_hash_password = auth_service_module.hash_password
    barrier = threading.Barrier(2)

    def synchronized_hash(password):
        hashed = original_hash_password(password)
        barrier.wait(timeout=10)
        return hashed

    monkeypatch.setattr(auth_service_module, "hash_password", synchronized_hash)

    def setup(username):
        client = client_factory(FakeNameProvider())
        try:
            return client.post(
                "/api/auth/setup",
                json={
                    "username": username,
                    "password": "password123",
                    "password_confirmation": "password123",
                },
            )
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(setup, ("owner-a", "owner-b")))

    assert sorted(response.status_code for response in responses) == [200, 409]
    with session_factory() as session:
        users = session.scalars(select(AppUser)).all()
        assert len(users) == 1
        assert users[0].username in {"owner-a", "owner-b"}
        assert users[0].role == "admin"
