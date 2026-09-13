"""TagService 单元测试（v0.03 技术方案 §36.1 + design D15）。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.instrument import Instrument
from app.models.tag import Tag as TagModel
from app.models.watchlist import Watchlist
from app.models.watchlist_tag import WatchlistTag
from app.services.tag_service import (
    DuplicateTagNameError,
    TagInUseError,
    TagNameEmptyError,
    TagNameTooLongError,
    TagNotFoundError,
    TagService,
)


def _add_instrument(session, iid="CN:STOCK:600519", asset_type="STOCK"):
    inst = Instrument(
        instrument_id=iid,
        symbol=iid.split(":")[-1],
        name="测试证券",
        market=iid.split(":")[0],
        asset_type=asset_type,
        currency="CNY",
    )
    session.add(inst)
    session.flush()
    return inst


def _add_watchlist(session, iid="CN:STOCK:600519", sort_order=10):
    row = Watchlist(instrument_id=iid, sort_order=sort_order)
    session.add(row)
    session.flush()
    return row


def _link_tag(session, watchlist_row, tag_id):
    """建立条目-标签关联（多对多）。"""
    link = WatchlistTag(instrument_id=watchlist_row.instrument_id, tag_id=tag_id)
    session.add(link)
    session.flush()
    return link


# ---- 创建与校验 ----


def test_create_strips_and_saves(session):
    tag = TagService(session).create("  科技  ")
    assert tag.name == "科技"


def test_create_empty_name_rejected(session):
    with pytest.raises(TagNameEmptyError):
        TagService(session).create("   ")


def test_create_too_long_name_rejected(session):
    with pytest.raises(TagNameTooLongError):
        TagService(session).create("标" * 51)


def test_create_duplicate_name_rejected(session):
    svc = TagService(session)
    svc.create("科技")
    with pytest.raises(DuplicateTagNameError):
        svc.create("科技")


# ---- 修改 ----


def test_rename_success_keeps_association(session):
    svc = TagService(session)
    tag = svc.create("科技")
    _add_instrument(session)
    _link_tag(session, _add_watchlist(session), tag.tag_id)
    session.commit()

    renamed = svc.rename(tag.tag_id, "科技股")
    assert renamed.name == "科技股"

    # 关联按 id 维护：改名不影响既有自选关联
    link = session.query(WatchlistTag).one()
    assert link.tag_id == tag.tag_id
    assert svc.repo.get(link.tag_id).name == "科技股"


def test_rename_to_duplicate_name_rejected(session):
    svc = TagService(session)
    svc.create("科技")
    tag = svc.create("消费")
    with pytest.raises(DuplicateTagNameError):
        svc.rename(tag.tag_id, "科技")


def test_rename_missing_tag(session):
    with pytest.raises(TagNotFoundError):
        TagService(session).rename(999, "新名字")


# ---- 删除保护 ----


def test_delete_unused_tag(session):
    svc = TagService(session)
    tag = svc.create("观察")
    svc.delete(tag.tag_id)
    assert svc.repo.get(tag.tag_id) is None


def test_delete_in_use_tag_rejected_with_count(session):
    svc = TagService(session)
    tag = svc.create("高股息")
    _add_instrument(session)
    _link_tag(session, _add_watchlist(session), tag.tag_id)
    session.commit()

    with pytest.raises(TagInUseError) as ei:
        svc.delete(tag.tag_id)
    assert "1 个证券" in str(ei.value)
    assert svc.repo.get(tag.tag_id) is not None  # 标签仍在


def test_usage_count_multiple_tags_and_entries(session):
    """多对多：一个条目多标签、一个标签多条目时 usage_count 正确。"""
    svc = TagService(session)
    tag_hi = svc.create("高股息")
    tag_tech = svc.create("科技")
    _add_instrument(session, "CN:STOCK:600519")
    _add_instrument(session, "CN:STOCK:600900")
    row_a = _add_watchlist(session, "CN:STOCK:600519", 10)
    row_b = _add_watchlist(session, "CN:STOCK:600900", 20)
    _link_tag(session, row_a, tag_hi.tag_id)
    _link_tag(session, row_a, tag_tech.tag_id)
    _link_tag(session, row_b, tag_hi.tag_id)
    session.commit()

    assert svc.repo.count_usage(tag_hi.tag_id) == 2
    assert svc.repo.count_usage(tag_tech.tag_id) == 1


def test_usage_count_decreases_after_watchlist_removed(session):
    """DuckDB 无级联删除，删除自选时由 Service 先删关联、提交后再删条目（design D3）。

    须分两次提交：DuckDB 1.5.5 的 FK 检查看不到同事务内已删的子表行，
    同事务"先删关联再删条目"会被误拦（Phase 0 结论，同 WatchlistService.remove）。
    """
    svc = TagService(session)
    tag = svc.create("高股息")
    _add_instrument(session, "CN:STOCK:600519")
    _add_instrument(session, "CN:STOCK:600900")
    _link_tag(session, _add_watchlist(session, "CN:STOCK:600519", 10), tag.tag_id)
    _link_tag(session, _add_watchlist(session, "CN:STOCK:600900", 20), tag.tag_id)
    session.commit()
    assert svc.repo.count_usage(tag.tag_id) == 2

    # 模拟 WatchlistService.remove 的两段提交顺序：先删关联并提交，再删条目
    session.query(WatchlistTag).filter_by(instrument_id="CN:STOCK:600519").delete()
    session.commit()
    session.query(Watchlist).filter_by(instrument_id="CN:STOCK:600519").delete()
    session.commit()
    assert svc.repo.count_usage(tag.tag_id) == 1

    # 删除全部引用后可删除
    session.query(WatchlistTag).filter_by(instrument_id="CN:STOCK:600900").delete()
    session.commit()
    session.query(Watchlist).filter_by(instrument_id="CN:STOCK:600900").delete()
    session.commit()
    assert svc.repo.count_usage(tag.tag_id) == 0
    svc.delete(tag.tag_id)
    assert svc.repo.get(tag.tag_id) is None


def test_db_layer_blocks_delete_of_referenced_tag(session):
    """数据库层兜底：绕过业务校验直接删除被引用标签，DuckDB FK RESTRICT 拦截。"""
    svc = TagService(session)
    tag = svc.create("高股息")
    _add_instrument(session)
    _link_tag(session, _add_watchlist(session), tag.tag_id)
    session.commit()

    obj = session.get(TagModel, tag.tag_id)
    session.delete(obj)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
