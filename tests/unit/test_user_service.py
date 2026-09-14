"""UserService 单元测试（multi-user-auth 2.5）：用户名规则、唯一性、最后一个管理员保护。"""

from __future__ import annotations

import pytest

from app.services.user_service import (
    DuplicateUsernameError,
    InvalidRoleError,
    LastAdminError,
    UserNotFoundError,
    UsernameRuleError,
    UserService,
    WeakPasswordError,
)


def _create(svc: UserService, username="alice", password="password123", role="user"):
    return svc.create_user(username=username, password=password, role=role)


# ---- 创建与用户名规则 ----


def test_create_user_hashes_password(session):
    user = _create(UserService(session))
    assert user.user_id > 0
    assert user.is_active is True
    assert user.password_hash.startswith("$argon2id$")
    assert user.password_hash != "password123"


def test_username_length_limits(session):
    svc = UserService(session)
    with pytest.raises(UsernameRuleError):
        svc.create_user(username="ab", password="password123")
    with pytest.raises(UsernameRuleError):
        svc.create_user(username="a" * 33, password="password123")


def test_username_charset(session):
    svc = UserService(session)
    for bad in ("张三", "user name", "user@x", "用户1"):
        with pytest.raises(UsernameRuleError):
            svc.create_user(username=bad, password="password123")


def test_username_case_insensitive_unique(session):
    svc = UserService(session)
    _create(svc, username="Alice")
    with pytest.raises(DuplicateUsernameError):
        svc.create_user(username="alice", password="password456")


def test_password_policy(session):
    svc = UserService(session)
    with pytest.raises(WeakPasswordError):
        svc.create_user(username="bob", password="short")
    with pytest.raises(WeakPasswordError):
        svc.create_user(username="bob", password="x" * 129)


def test_invalid_role_rejected(session):
    svc = UserService(session)
    with pytest.raises(InvalidRoleError):
        svc.create_user(username="bob", password="password123", role="superadmin")


# ---- 启用 / 禁用 ----


def test_disable_and_reenable_keeps_data(session):
    svc = UserService(session)
    user = _create(svc)
    svc.set_active(user.user_id, False)
    assert svc.get(user.user_id).is_active is False
    svc.set_active(user.user_id, True)
    assert svc.get(user.user_id).is_active is True


def test_disable_unknown_user(session):
    with pytest.raises(UserNotFoundError):
        UserService(session).set_active(999, False)


# ---- 最后一个管理员保护 ----


def test_cannot_disable_last_admin(session):
    svc = UserService(session)
    admin = _create(svc, username="root", role="admin")
    with pytest.raises(LastAdminError):
        svc.set_active(admin.user_id, False)
    assert svc.get(admin.user_id).is_active is True


def test_cannot_demote_last_admin(session):
    svc = UserService(session)
    admin = _create(svc, username="root", role="admin")
    with pytest.raises(LastAdminError):
        svc.set_role(admin.user_id, "user")
    assert svc.get(admin.user_id).role == "admin"


def test_can_disable_admin_when_another_exists(session):
    svc = UserService(session)
    first = _create(svc, username="root1", role="admin")
    _create(svc, username="root2", role="admin")
    svc.set_active(first.user_id, False)  # 仍有另一个有效管理员
    assert svc.get(first.user_id).is_active is False


def test_can_disable_last_admin_if_already_inactive(session):
    """已禁用的 admin 不计入有效管理员，重复禁用不触发保护（幂等）。"""
    svc = UserService(session)
    admin = _create(svc, username="root", role="admin")
    svc.set_role(admin.user_id, "admin")  # no-op 语义路径
    # 直接绕过保护制造"仅剩一个已禁用 admin"的状态
    from app.models.user import AppUser

    svc.session.query(AppUser).filter_by(user_id=admin.user_id).update({"is_active": False})
    svc.session.commit()
    svc.set_active(admin.user_id, False)  # 不抛错


def test_demote_user_to_admin_allowed(session):
    svc = UserService(session)
    user = _create(svc)
    svc.set_role(user.user_id, "admin")
    assert svc.get(user.user_id).role == "admin"


# ---- 重置密码 ----


def test_reset_password_changes_hash(session):
    from app.auth.password import verify_password

    svc = UserService(session)
    user = _create(svc)
    old_hash = user.password_hash
    svc.reset_password(user.user_id, "newpassword456")
    assert user.password_hash != old_hash
    assert verify_password("newpassword456", user.password_hash)


def test_reset_password_unknown_user(session):
    with pytest.raises(UserNotFoundError):
        UserService(session).reset_password(999, "password123")
