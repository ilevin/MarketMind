"""密码 Argon2id 哈希单元测试（multi-user-auth 1.4）。"""

from __future__ import annotations

from app.auth.password import PLACEHOLDER_HASH, hash_password, verify_password


def test_hash_is_argon2id():
    hashed = hash_password("s3cret-密码")
    assert hashed.startswith("$argon2id$")


def test_verify_roundtrip():
    hashed = hash_password("s3cret")
    assert verify_password("s3cret", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_verify_illegal_hash_format():
    """占位/损坏哈希不抛异常，按不匹配处理。"""
    assert verify_password("anything", PLACEHOLDER_HASH) is False
    assert verify_password("anything", "not-a-hash") is False


def test_hash_unique_per_call():
    """Argon2id 自带随机盐：同一明文两次哈希结果不同。"""
    assert hash_password("same") != hash_password("same")
