"""管理员用户管理 API 集成测试（multi-user-auth tasks 7.3）。

覆盖 user-management spec 各 Requirement 的 HTTP 行为：

- 「管理员用户管理 API」：列表 / 创建 / PATCH / 重置密码；普通用户 403、
  匿名 401；列表 SHALL NOT 返回 password_hash；第一阶段无 DELETE 端点（405）；
- 「用户名规则与唯一性」：3~32 字符、仅 [A-Za-z0-9_-]、唯一性大小写不敏感
  （Alice 与 alice 冲突）；
- 「禁用优先于删除」：禁用即撤销全部 Session（既有 Cookie 立即 401）、
  禁用账户无法登录（403）、重新启用后可凭原密码再次登录；
- 「最后一个管理员保护」：仅剩一个有效 admin 时禁用 / 降级自己 409，
  存在第二个有效 admin 时操作放行。

统一以 root 管理员视角操作（AuthedClient 写请求自动注入 X-CSRF-Token）；
被操作用户经 client_factory 真实登录，验证 Session 撤销语义。
"""

from __future__ import annotations

import pytest


class FakeNameProvider:
    """名称识别假件：用户管理测试不依赖标的名称。"""

    def get_name(self, market, asset_type, symbol):
        return None


@pytest.fixture()
def admin(client_factory):
    """root 管理员视角（真实登录 + 写请求自动带 X-CSRF-Token）。"""
    return client_factory(FakeNameProvider(), login_as="root", role="admin")


def _get_user(admin, username: str) -> dict:
    """从 GET /api/admin/users 列表按用户名查找，返回该用户条目。"""
    items = admin.get("/api/admin/users").json()["items"]
    for item in items:
        if item["username"] == username:
            return item
    raise AssertionError(f"用户列表中不存在用户: {username}")


# ---- 创建：201 / 重名 409 / 非法用户名与 role 422 ----


def test_create_user_returns_201_and_new_user_can_login(client_factory, admin):
    """创建用户返回 201（含基础字段、不含 password_hash），新用户可登录。"""
    resp = admin.post(
        "/api/admin/users", json={"username": "bob", "password": "password123"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "bob"
    assert body["role"] == "user"
    assert body["is_active"] is True
    assert body["must_change_password"] is False
    assert isinstance(body["created_at"], str) and body["created_at"]
    assert "password_hash" not in body

    # spec：创建成功后新用户可登录
    anon = client_factory(FakeNameProvider())
    login = anon.post(
        "/api/auth/login", json={"username": "bob", "password": "password123"}
    )
    assert login.status_code == 200
    assert login.json() == {"username": "bob", "role": "user"}


def test_create_duplicate_username_returns_409(admin):
    """重名（完全一致与大小写变体）均 409，且不创建新账户。"""
    resp = admin.post(
        "/api/admin/users", json={"username": "Alice", "password": "password123"}
    )
    assert resp.status_code == 201

    # 完全重名
    resp = admin.post(
        "/api/admin/users", json={"username": "Alice", "password": "password123"}
    )
    assert resp.status_code == 409
    assert "已存在" in resp.json()["detail"]

    # 大小写变体：已建 Alice 再建 alice，唯一性按小写比较视为冲突
    resp = admin.post(
        "/api/admin/users", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 409

    # 未创建重复账户：列表中 Alice 仅一条、无小写 alice
    names = [i["username"] for i in admin.get("/api/admin/users").json()["items"]]
    assert names.count("Alice") == 1
    assert "alice" not in names


@pytest.mark.parametrize(
    "username",
    ["ab", "us er", "a@b.com", "张三丰", "a" * 33],
)
def test_create_invalid_username_returns_422(admin, username):
    """非法用户名（过短 / 空格 / @ / 中文 / 超长）422，且不创建。"""
    resp = admin.post(
        "/api/admin/users", json={"username": username, "password": "password123"}
    )
    assert resp.status_code == 422

    usernames = [i["username"] for i in admin.get("/api/admin/users").json()["items"]]
    assert username not in usernames


@pytest.mark.parametrize("role", ["superuser", ""])
def test_create_invalid_role_returns_422(admin, role):
    """role 仅允许 user / admin，其它值 422。"""
    resp = admin.post(
        "/api/admin/users",
        json={"username": "carol", "password": "password123", "role": role},
    )
    assert resp.status_code == 422


# ---- 列表：含全部用户，任何字段不含 password_hash ----


def test_list_users_contains_all_and_leaks_no_password_hash(admin):
    resp = admin.post(
        "/api/admin/users", json={"username": "dave", "password": "password123"}
    )
    assert resp.status_code == 201
    resp = admin.post(
        "/api/admin/users",
        json={"username": "erin", "password": "password123", "role": "admin"},
    )
    assert resp.status_code == 201

    resp = admin.get("/api/admin/users")
    assert resp.status_code == 200
    items = resp.json()["items"]
    usernames = {i["username"] for i in items}
    assert {"root", "dave", "erin"} <= usernames

    # 逐字段断言：任何用户条目都不含 password_hash，且核心字段齐全
    for item in items:
        assert "password_hash" not in item
        assert {"user_id", "username", "role", "is_active", "must_change_password"} <= set(item)
    # 兜底：整个响应文本不出现 password_hash 字样
    assert "password_hash" not in resp.text


# ---- PATCH：is_active / role 修改、不存在 user_id 404 ----


def test_patch_user_toggles_active_and_role(admin):
    resp = admin.post(
        "/api/admin/users", json={"username": "frank", "password": "password123"}
    )
    assert resp.status_code == 201
    user_id = resp.json()["user_id"]

    # 禁用 -> 重新启用
    resp = admin.patch(f"/api/admin/users/{user_id}", json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    resp = admin.patch(f"/api/admin/users/{user_id}", json={"is_active": True})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    # 角色切换（存在 root 这个有效 admin，frank 的升降级不受最后管理员保护限制）
    resp = admin.patch(f"/api/admin/users/{user_id}", json={"role": "admin"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
    resp = admin.patch(f"/api/admin/users/{user_id}", json={"role": "user"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"

    # 列表侧状态一致
    item = _get_user(admin, "frank")
    assert item["is_active"] is True
    assert item["role"] == "user"


def test_patch_nonexistent_user_returns_404(admin):
    """对不存在的 user_id PATCH 返回 404。"""
    resp = admin.patch("/api/admin/users/999999", json={"is_active": False})
    assert resp.status_code == 404


# ---- 禁用：既有 Session 立即失效，禁用账户无法登录，重新启用可再登录 ----


def test_disable_user_revokes_session_immediately(client_factory, admin):
    bob = client_factory(FakeNameProvider(), login_as="bob")
    assert bob.get("/api/auth/me").status_code == 200

    user_id = _get_user(admin, "bob")["user_id"]
    resp = admin.patch(f"/api/admin/users/{user_id}", json={"is_active": False})
    assert resp.status_code == 200

    # 既有 Cookie 立即失效（Session 全部撤销）
    assert bob.get("/api/auth/me").status_code == 401

    # 禁用账户无法登录（403）
    anon = client_factory(FakeNameProvider())
    resp = anon.post(
        "/api/auth/login", json={"username": "bob", "password": "password123"}
    )
    assert resp.status_code == 403
    assert "禁用" in resp.json()["detail"]

    # 重新启用后可凭原密码再次登录（spec「重新启用」Scenario）
    resp = admin.patch(f"/api/admin/users/{user_id}", json={"is_active": True})
    assert resp.status_code == 200
    resp = anon.post(
        "/api/auth/login", json={"username": "bob", "password": "password123"}
    )
    assert resp.status_code == 200


# ---- 重置密码：该用户全部 Session 失效 + 新密码可登录 ----


def test_reset_password_revokes_sessions_and_new_password_works(client_factory, admin):
    alice = client_factory(FakeNameProvider(), login_as="alice")
    assert alice.get("/api/auth/me").status_code == 200

    user_id = _get_user(admin, "alice")["user_id"]
    resp = admin.post(
        f"/api/admin/users/{user_id}/reset-password", json={"new_password": "NewPass_456"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True}

    # 旧 Cookie 立即失效（全部 Session 撤销）
    assert alice.get("/api/auth/me").status_code == 401

    anon = client_factory(FakeNameProvider())
    # 旧密码登录失败（统一文案，不泄露账户存在性）
    resp = anon.post(
        "/api/auth/login", json={"username": "alice", "password": "password123"}
    )
    assert resp.status_code == 401
    assert "用户名或密码错误" in resp.json()["detail"]
    # 新密码可登录
    resp = anon.post(
        "/api/auth/login", json={"username": "alice", "password": "NewPass_456"}
    )
    assert resp.status_code == 200


# ---- 权限三态：普通用户 403、匿名 401 ----

# 全部 /api/admin/users* 端点（方法, 路径, 请求体）；权限依赖先于业务逻辑，
# 路径中的 user_id 取值不影响 401/403 结论。
ADMIN_USERS_REQUESTS = [
    ("get", "/api/admin/users", None),
    ("post", "/api/admin/users", {"username": "mallory", "password": "password123"}),
    ("patch", "/api/admin/users/1", {"is_active": False}),
    ("post", "/api/admin/users/1/reset-password", {"new_password": "password123"}),
]


@pytest.mark.parametrize(("method", "path", "payload"), ADMIN_USERS_REQUESTS)
def test_admin_users_endpoints_forbid_normal_user(client_factory, method, path, payload):
    """普通登录用户访问全部 /api/admin/users* 端点一律 403。"""
    eve = client_factory(FakeNameProvider(), login_as="eve")
    kwargs = {"json": payload} if payload is not None else {}
    resp = getattr(eve, method)(path, **kwargs)
    assert resp.status_code == 403
    assert "管理员" in resp.json()["detail"]


@pytest.mark.parametrize(("method", "path", "payload"), ADMIN_USERS_REQUESTS)
def test_admin_users_endpoints_require_login(client_factory, method, path, payload):
    """匿名访问全部 /api/admin/users* 端点一律 401（匿名写请求不触发 CSRF）。"""
    anon = client_factory(FakeNameProvider())
    kwargs = {"json": payload} if payload is not None else {}
    resp = getattr(anon, method)(path, **kwargs)
    assert resp.status_code == 401


# ---- 最后一个有效管理员保护 ----


def test_last_admin_cannot_disable_self(admin):
    """系统仅剩一个有效 admin（root）时，禁用自己返回 409。"""
    root_id = _get_user(admin, "root")["user_id"]
    resp = admin.patch(f"/api/admin/users/{root_id}", json={"is_active": False})
    assert resp.status_code == 409

    # root 保持启用，其 Session 仍有效
    assert _get_user(admin, "root")["is_active"] is True
    assert admin.get("/api/admin/users").status_code == 200


def test_last_admin_cannot_be_demoted(admin):
    """系统仅剩一个有效 admin（root）时，将其降级为 user 返回 409。"""
    root_id = _get_user(admin, "root")["user_id"]
    resp = admin.patch(f"/api/admin/users/{root_id}", json={"role": "user"})
    assert resp.status_code == 409
    assert _get_user(admin, "root")["role"] == "admin"


def test_disabling_admin_allowed_when_another_admin_exists(admin):
    """存在两个有效 admin 时禁用其中一个成功；回到仅剩一个后保护重新生效。"""
    resp = admin.post(
        "/api/admin/users",
        json={"username": "backup", "password": "password123", "role": "admin"},
    )
    assert resp.status_code == 201
    backup_id = resp.json()["user_id"]

    # root + backup 两个有效 admin：禁用 backup 放行
    resp = admin.patch(f"/api/admin/users/{backup_id}", json={"is_active": False})
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    # 仅剩 root 一个有效 admin：禁用 root 重新被拒绝
    root_id = _get_user(admin, "root")["user_id"]
    resp = admin.patch(f"/api/admin/users/{root_id}", json={"is_active": False})
    assert resp.status_code == 409


# ---- 无物理删除端点 ----


def test_delete_user_endpoint_returns_405(admin):
    """第一阶段不提供物理删除：DELETE /api/admin/users/{id} 返回 405。"""
    root_id = _get_user(admin, "root")["user_id"]
    resp = admin.delete(f"/api/admin/users/{root_id}")
    assert resp.status_code == 405

    # 用户未被删除，仍在列表中
    assert _get_user(admin, "root")["user_id"] == root_id
