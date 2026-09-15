"""多用户与认证（multi-user-auth）：身份域建表 + 用户私有表重建 + 旧数据归属 legacy owner。

结构变更（设计 design.md D4/D9，db-migration spec）：
- 新增 seq_user_id / app_user / user_session；
- watchlist / index_watchlist / tag / watchlist_tag 改为用户私有
  （复合主键含 user_id，watchlist_tag 复合外键 -> watchlist(user_id, instrument_id)）；
- 旧单用户数据全部归属 legacy owner（username=admin，占位密码哈希，
  must_change_password=true；迁移后经 /setup 认领，CLI 为后备路径，迁移内不含明文密码）。

DuckDB 1.5.5 约束（spike 验证结论）：
- 被 FK 引用的表无法 RENAME，也不支持 ALTER TABLE ADD FOREIGN KEY——
  故采用 staging 表方案：旧数据先拷入无 FK 的 staging 表 -> DROP 旧表 ->
  以最终表名建新表（CREATE TABLE 时携带全部 FK）-> 从 staging 回灌；
- DDL 具备事务性：任一校验失败抛异常，alembic 事务整体回滚，
  数据库保持 0001 版本结构，不残留半成品表。

校验（db-migration spec：迁移数据校验）：
- 四张私有表新旧行数一致；
- 新行 user_id 均存在（FK 保证，另行显式校验）；
- watchlist_tag 关联与自选条目同用户（无悬空）；
- 全局表（instrument/quote_snapshot/fundamental_snapshot/trading_calendar/
  job_status/app_setting）行数迁移前后不变。

Revision ID: 0002_multi_user_auth
Revises: 0001_duckdb_baseline
Create Date: 2026-09-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_multi_user_auth"
down_revision: Union[str, None] = "0001_duckdb_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

GLOBAL_TABLES = (
    "instrument",
    "quote_snapshot",
    "fundamental_snapshot",
    "trading_calendar",
    "job_status",
    "app_setting",
)
PRIVATE_TABLES = ("watchlist", "index_watchlist", "tag", "watchlist_tag")
LEGACY_USERNAME = "admin"


class MigrationVerificationError(RuntimeError):
    """校验失败：抛出使 alembic 事务整体回滚。"""


def _count(conn, table: str) -> int:
    return conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise MigrationVerificationError(f"迁移校验失败: {message}")


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. sequence 与身份域表 ---
    op.execute("CREATE SEQUENCE seq_user_id START 1")

    op.create_table(
        "app_user",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            server_default=sa.text("nextval('seq_user_id')"),
            nullable=False,
        ),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=8), server_default=sa.text("'user'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("user_id"),
        # username 不设 UNIQUE：沿用 tag.name 的写锁内查重模式（design D4）
    )

    op.create_table(
        "user_session",
        sa.Column("session_token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.user_id"], name="fk_user_session_user"
        ),
        sa.PrimaryKeyConstraint("session_token_hash"),
    )

    # --- 2. legacy owner：占位哈希不可登录，由 /setup 认领或停服后通过 CLI 设置 ---
    op.execute(
        sa.text(
            "INSERT INTO app_user (username, password_hash, role, is_active, "
            "must_change_password, created_at, updated_at) "
            "VALUES (:username, :password_hash, 'admin', true, true, now(), now())"
        ).bindparams(username=LEGACY_USERNAME, password_hash="!unloginable-placeholder")
    )
    legacy_id = conn.execute(
        sa.text("SELECT user_id FROM app_user WHERE username = :username").bindparams(
            username=LEGACY_USERNAME
        )
    ).scalar_one()

    # --- 3. 迁移前基线计数 ---
    old_counts = {t: _count(conn, t) for t in PRIVATE_TABLES}
    global_counts_before = {t: _count(conn, t) for t in GLOBAL_TABLES}

    # --- 4. staging 表（无 FK / 无 PK，仅承载拷贝） ---
    op.create_table(
        "watchlist_stage",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "index_watchlist_stage",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tag_stage",
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "watchlist_tag_stage",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
    )

    # 旧数据全部归属 legacy owner（旧主键保证无重复，stage 直拷）
    op.execute(
        sa.text(
            "INSERT INTO watchlist_stage "
            "SELECT :uid, instrument_id, sort_order, created_at FROM watchlist"
        ).bindparams(uid=legacy_id)
    )
    op.execute(
        sa.text(
            "INSERT INTO index_watchlist_stage "
            "SELECT :uid, instrument_id, sort_order, created_at FROM index_watchlist"
        ).bindparams(uid=legacy_id)
    )
    op.execute(
        sa.text(
            "INSERT INTO tag_stage "
            "SELECT tag_id, :uid, name, created_at, updated_at FROM tag"
        ).bindparams(uid=legacy_id)
    )
    op.execute(
        sa.text(
            "INSERT INTO watchlist_tag_stage "
            "SELECT :uid, instrument_id, tag_id FROM watchlist_tag"
        ).bindparams(uid=legacy_id)
    )

    # staging 行数 == 旧表行数
    _require(
        _count(conn, "watchlist_stage") == old_counts["watchlist"],
        f"watchlist staging 行数不一致: {_count(conn, 'watchlist_stage')} != {old_counts['watchlist']}",
    )
    _require(
        _count(conn, "index_watchlist_stage") == old_counts["index_watchlist"],
        "index_watchlist staging 行数不一致",
    )
    _require(_count(conn, "tag_stage") == old_counts["tag"], "tag staging 行数不一致")
    _require(
        _count(conn, "watchlist_tag_stage") == old_counts["watchlist_tag"],
        "watchlist_tag staging 行数不一致",
    )

    # --- 5. 删旧表（无任何对象依赖 staging） ---
    op.drop_table("watchlist_tag")
    op.drop_table("tag")
    op.drop_table("index_watchlist")
    op.drop_table("watchlist")

    # --- 6. 以最终表名建新表（CREATE TABLE 时携带全部 FK） ---
    op.create_table(
        "watchlist",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"], name="fk_watchlist_user"),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.instrument_id"], name="fk_watchlist_instrument"
        ),
        sa.PrimaryKeyConstraint("user_id", "instrument_id"),
    )
    op.create_table(
        "index_watchlist",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.user_id"], name="fk_index_watchlist_user"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_index_watchlist_instrument",
        ),
        sa.PrimaryKeyConstraint("user_id", "instrument_id"),
    )
    op.create_table(
        "tag",
        sa.Column(
            "tag_id",
            sa.BigInteger(),
            server_default=sa.text("nextval('seq_tag_id')"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.user_id"], name="fk_tag_user"),
        sa.PrimaryKeyConstraint("tag_id"),
    )
    op.create_table(
        "watchlist_tag",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "instrument_id"],
            ["watchlist.user_id", "watchlist.instrument_id"],
            name="fk_watchlist_tag_watchlist",
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.tag_id"], name="fk_watchlist_tag_tag"),
        sa.PrimaryKeyConstraint("user_id", "instrument_id", "tag_id"),
    )

    # --- 7. staging -> 新表（FK 在插入时即校验完整性） ---
    op.execute(
        "INSERT INTO watchlist (user_id, instrument_id, sort_order, created_at) "
        "SELECT user_id, instrument_id, sort_order, created_at FROM watchlist_stage"
    )
    op.execute(
        "INSERT INTO index_watchlist (user_id, instrument_id, sort_order, created_at) "
        "SELECT user_id, instrument_id, sort_order, created_at FROM index_watchlist_stage"
    )
    op.execute(
        "INSERT INTO tag (tag_id, user_id, name, created_at, updated_at) "
        "SELECT tag_id, user_id, name, created_at, updated_at FROM tag_stage"
    )
    op.execute(
        "INSERT INTO watchlist_tag (user_id, instrument_id, tag_id) "
        "SELECT user_id, instrument_id, tag_id FROM watchlist_tag_stage"
    )

    # --- 8. 数据校验（db-migration spec：迁移数据校验） ---
    _require(
        _count(conn, "watchlist") == old_counts["watchlist"], "watchlist 新表行数不一致"
    )
    _require(
        _count(conn, "index_watchlist") == old_counts["index_watchlist"],
        "index_watchlist 新表行数不一致",
    )
    _require(_count(conn, "tag") == old_counts["tag"], "tag 新表行数不一致")
    _require(
        _count(conn, "watchlist_tag") == old_counts["watchlist_tag"],
        "watchlist_tag 新表行数不一致",
    )

    # 新行 user_id 均存在于 app_user
    orphan_users = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM watchlist w "
            "LEFT JOIN app_user u ON u.user_id = w.user_id WHERE u.user_id IS NULL"
        )
    ).scalar_one()
    _require(orphan_users == 0, f"watchlist 存在 {orphan_users} 行悬空 user_id")

    # 关联与自选条目同用户（复合 FK 保证，显式复核防回归）
    dangling_links = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM watchlist_tag wt "
            "LEFT JOIN watchlist w "
            "ON w.user_id = wt.user_id AND w.instrument_id = wt.instrument_id "
            "WHERE w.instrument_id IS NULL"
        )
    ).scalar_one()
    _require(dangling_links == 0, f"watchlist_tag 存在 {dangling_links} 行悬空关联")

    # 全局表行数不变
    for table in GLOBAL_TABLES:
        after = _count(conn, table)
        _require(
            after == global_counts_before[table],
            f"全局表 {table} 行数在迁移中发生变化: "
            f"{global_counts_before[table]} -> {after}",
        )

    # --- 9. 清理 staging ---
    for stage in ("watchlist_tag_stage", "tag_stage", "index_watchlist_stage", "watchlist_stage"):
        op.drop_table(stage)


def downgrade() -> None:
    """降级回单用户结构：仅保留 legacy owner（首个 admin）的数据。

    多用户数据无法无损折叠回单用户模型，其余用户的数据被丢弃——
    生产环境破坏性回滚应走数据库文件备份恢复（db-migration spec）。

    与 upgrade 对称的 staging 方案：四张私有表均保留 legacy owner 的数据
    （不仅是 watchlist——tag / index_watchlist / watchlist_tag 同样回灌，
    否则降级后 legacy owner 的标签与指数配置全部丢失）。
    """
    conn = op.get_bind()

    legacy_id = conn.execute(
        sa.text("SELECT user_id FROM app_user WHERE username = :username").bindparams(
            username=LEGACY_USERNAME
        )
    ).scalar_one_or_none()

    # --- 1. staging 表（0001 单用户结构，无 FK / 无 PK） ---
    op.create_table(
        "watchlist_down_stage",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "index_watchlist_down_stage",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tag_down_stage",
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "watchlist_tag_down_stage",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
    )

    if legacy_id is not None:
        op.execute(
            sa.text(
                "INSERT INTO watchlist_down_stage "
                "SELECT instrument_id, sort_order, created_at FROM watchlist "
                "WHERE user_id = :uid"
            ).bindparams(uid=legacy_id)
        )
        op.execute(
            sa.text(
                "INSERT INTO index_watchlist_down_stage "
                "SELECT instrument_id, sort_order, created_at FROM index_watchlist "
                "WHERE user_id = :uid"
            ).bindparams(uid=legacy_id)
        )
        op.execute(
            sa.text(
                "INSERT INTO tag_down_stage "
                "SELECT tag_id, name, created_at, updated_at FROM tag "
                "WHERE user_id = :uid"
            ).bindparams(uid=legacy_id)
        )
        # 复合 FK 保证关联与同用户自选条目对应；JOIN 再过滤一次为防御性复核
        op.execute(
            sa.text(
                "INSERT INTO watchlist_tag_down_stage "
                "SELECT wt.instrument_id, wt.tag_id FROM watchlist_tag wt "
                "JOIN watchlist w ON w.user_id = wt.user_id "
                "AND w.instrument_id = wt.instrument_id "
                "WHERE wt.user_id = :uid"
            ).bindparams(uid=legacy_id)
        )

    # --- 2. 删旧表（staging 无任何对象依赖） ---
    op.drop_table("watchlist_tag")
    op.drop_table("tag")
    op.drop_table("index_watchlist")
    op.drop_table("watchlist")
    op.drop_table("user_session")
    op.drop_table("app_user")
    op.execute("DROP SEQUENCE IF EXISTS seq_user_id")

    # --- 3. 重建 0001 单用户结构（与 0001_duckdb_baseline 完全一致，含 FK） ---
    op.create_table(
        "watchlist",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_watchlist_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
    )
    op.create_table(
        "index_watchlist",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.instrument_id"],
            name="fk_index_watchlist_instrument",
        ),
        sa.PrimaryKeyConstraint("instrument_id"),
    )
    # seq_tag_id 在 upgrade 中保留未删（新 tag 表沿用），此处无需重建
    op.create_table(
        "tag",
        sa.Column(
            "tag_id",
            sa.BigInteger(),
            server_default=sa.text("nextval('seq_tag_id')"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tag_id"),
    )
    op.create_table(
        "watchlist_tag",
        sa.Column("instrument_id", sa.String(length=64), nullable=False),
        sa.Column("tag_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["watchlist.instrument_id"], name="fk_watchlist_tag_instrument"
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.tag_id"], name="fk_watchlist_tag_tag"),
        sa.PrimaryKeyConstraint("instrument_id", "tag_id"),
    )

    # --- 4. staging 回灌 + 行数校验 ---
    op.execute(
        "INSERT INTO watchlist (instrument_id, sort_order, created_at) "
        "SELECT instrument_id, sort_order, created_at FROM watchlist_down_stage"
    )
    op.execute(
        "INSERT INTO index_watchlist (instrument_id, sort_order, created_at) "
        "SELECT instrument_id, sort_order, created_at FROM index_watchlist_down_stage"
    )
    op.execute(
        "INSERT INTO tag (tag_id, name, created_at, updated_at) "
        "SELECT tag_id, name, created_at, updated_at FROM tag_down_stage"
    )
    op.execute(
        "INSERT INTO watchlist_tag (instrument_id, tag_id) "
        "SELECT instrument_id, tag_id FROM watchlist_tag_down_stage"
    )
    for table, stage in (
        ("watchlist", "watchlist_down_stage"),
        ("index_watchlist", "index_watchlist_down_stage"),
        ("tag", "tag_down_stage"),
        ("watchlist_tag", "watchlist_tag_down_stage"),
    ):
        _require(
            _count(conn, table) == _count(conn, stage),
            f"{table} 降级回灌行数不一致: {_count(conn, table)} != {_count(conn, stage)}",
        )

    # --- 5. 清理 staging ---
    for stage in (
        "watchlist_tag_down_stage",
        "tag_down_stage",
        "index_watchlist_down_stage",
        "watchlist_down_stage",
    ):
        op.drop_table(stage)
