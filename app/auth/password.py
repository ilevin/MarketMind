"""密码安全存储（multi-user-auth design D2）：Argon2id 哈希与校验。

- 明文/可逆形式禁止入库，日志禁止输出密码；
- 占位哈希 PLACEHOLDER_HASH 用于迁移产生的 legacy owner（不可登录，
  直到经 /setup、CLI 或管理接口设置真实密码）。
"""

from __future__ import annotations

from pwdlib import PasswordHash

_hasher = PasswordHash.recommended()  # pwdlib[argon2] 默认即 Argon2id


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(plain, hashed)
    except Exception:
        # 非法哈希格式（如占位值）一律视为不匹配，不向上抛错
        return False


# 时序抹平哑哈希：模块加载时计算一次（真实 Argon2 格式）。
# 用户不存在时也执行一次同代价校验，消除"用户名不存在→响应更快"的
# 用户名枚举侧信道；PLACEHOLDER_HASH 为非法格式走异常路径（~0ms），不适用。
_DUMMY_HASH = _hasher.hash("timing-equalizer-marketmind")


def dummy_verify(password: str) -> None:
    """对哑哈希执行一次真实 Argon2 校验（结果丢弃），仅用于登录时序对齐。"""
    _hasher.verify(password, _DUMMY_HASH)


# 迁移 legacy owner 的占位密码哈希：非 Argon2 格式，任何明文都无法通过校验
PLACEHOLDER_HASH = "!unloginable-placeholder"

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


class WeakPasswordError(Exception):
    """新密码不满足长度要求。"""


def validate_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"密码长度不能少于 {MIN_PASSWORD_LENGTH} 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(f"密码长度不能超过 {MAX_PASSWORD_LENGTH} 位")
