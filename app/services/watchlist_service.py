"""自选 / 指数配置业务服务（增删查排序 + 名称自动识别）。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from sqlalchemy import delete as sa_delete

from app.db import write_coordinator
from app.models.instrument import Instrument
from app.models.tag import Tag
from app.models.watchlist_tag import WatchlistTag
from app.providers.base import InstrumentNameProvider
from app.repositories.instrument import InstrumentRepository
from app.repositories.tag import TagRepository
from app.repositories.watchlist import (
    BaseWatchlistRepository,
    IndexWatchlistRepository,
    WatchlistRepository,
)
from app.services.instrument_id import (
    MARKET_CURRENCY,
    InvalidInstrumentError,
    build_instrument_id,
)

logger = logging.getLogger(__name__)

_HK_INDEX_CODE_HINT = (
    "。港股指数代码为字母缩写，常见如 HSI（恒生指数）、"
    "HSCEI（国企指数）、HSTECH（恒生科技指数），请核对后重试"
)


def _unknown_symbol_detail(market: str, asset_type: str, symbol: str) -> str:
    """识别失败的报错文案；港股指数附常见代码指引。"""
    detail = f"无法识别证券: {market}/{asset_type}/{symbol}"
    if market.upper() == "HK" and asset_type == "INDEX":
        detail += _HK_INDEX_CODE_HINT
    return detail


class ServiceError(Exception):
    """业务错误基类。"""


class DuplicateItemError(ServiceError):
    """重复添加。"""


class InstrumentNotFoundError(ServiceError):
    """证券无法识别。"""


class InvalidTypeError(ServiceError):
    """资产类型不允许进入该列表。"""


class WatchlistEntryNotFoundError(ServiceError):
    """自选条目不存在（v0.03 标签关联接口）。"""


class TagNotAllowedError(ServiceError):
    """指数不支持标签（v0.03 技术方案 §10）。"""


class BaseWatchlistService:
    """股票/ETF 自选与指数配置共用的服务逻辑。"""

    allowed_asset_types: frozenset[str]

    def __init__(self, session: Session, name_provider: InstrumentNameProvider):
        self.session = session
        self.name_provider = name_provider
        self.instrument_repo = InstrumentRepository(session)
        self.repo = self._make_repo(session)

    def _make_repo(self, session: Session) -> BaseWatchlistRepository:
        raise NotImplementedError

    def list(self) -> list[tuple[Instrument, int]]:
        return [(inst, row.sort_order) for row, inst in self.repo.list_ordered()]

    def add(self, *, symbol: str, market: str, asset_type: str) -> str:
        """返回 instrument_id；失败抛出业务异常。"""
        asset_type = asset_type.upper()
        if asset_type not in self.allowed_asset_types:
            raise InvalidTypeError(
                f"asset_type 仅允许 {'/'.join(sorted(self.allowed_asset_types))}，收到 {asset_type}"
            )
        # 港股指数代码为字母缩写（如 HSTECH），统一大写；A股/港股股票与 ETF 代码均为数字
        symbol = symbol.strip().upper()
        instrument_id = build_instrument_id(market, asset_type, symbol)
        if self.repo.exists(instrument_id):
            raise DuplicateItemError(f"已在列表中: {instrument_id}")

        name = self.name_provider.get_name(market.upper(), asset_type, symbol)
        if not name:
            raise InstrumentNotFoundError(_unknown_symbol_detail(market, asset_type, symbol))

        with write_coordinator.write():
            self.instrument_repo.upsert(
                instrument_id=instrument_id,
                symbol=symbol,
                name=name,
                market=market.upper(),
                asset_type=asset_type,
                currency=MARKET_CURRENCY[market.upper()],
            )
            self.repo.add(instrument_id, sort_order=self.repo.next_sort_order())
            self.session.commit()
        logger.info("已添加自选: %s (%s)", instrument_id, name)
        return instrument_id

    def remove(self, instrument_id: str) -> bool:
        with write_coordinator.write():
            # DuckDB 不支持外键级联删除：先删标签关联再删条目。
            # 注意须分两次提交：DuckDB 1.5.5 的 FK 检查看不到同事务内已删的
            # 子表行，同事务"先删关联再删条目"会被误拦（Phase 0 结论）；
            # 写锁保证两段之间无其他写者，单写者模型下无中间态并发风险。
            self.session.execute(
                sa_delete(WatchlistTag).where(WatchlistTag.instrument_id == instrument_id)
            )
            self.session.commit()
            removed = self.repo.remove(instrument_id)
            self.session.commit()
        if removed:
            logger.info("已删除自选: %s", instrument_id)
        return removed

    def reorder(self, orders: dict[str, int]) -> None:
        with write_coordinator.write():
            self.repo.reorder(orders)
            self.session.commit()
        logger.info("已调整自选排序: %s", list(orders.items()))


class WatchlistService(BaseWatchlistService):
    allowed_asset_types = frozenset({"STOCK", "ETF"})

    def _make_repo(self, session: Session) -> WatchlistRepository:
        return WatchlistRepository(session)

    def list_with_tags(self) -> list[tuple[Instrument, int, list[Tag]]]:
        """列表（含标签数组）；指数服务无此能力（指数不支持标签）。"""
        return [
            (inst, row.sort_order, tags)
            for row, inst, tags in self.repo.list_ordered_with_tags()
        ]

    def set_tags(
        self, instrument_id: str, tag_ids: list[int]
    ) -> tuple[Instrument, int, list[Tag]]:
        """以全量集合替换自选条目的全部标签关联（幂等；空数组即解除全部）。

        校验顺序（design D2 修订）：先判 instrument 的 asset_type（INDEX→400，
        指数条目只存在于 index_watchlist、不在 watchlist 表，先判类型使该
        场景经 API 可达），再查自选行（404）、各 tag_id 存在性（404）。
        """
        inst = self.instrument_repo.get(instrument_id)
        if inst is None or inst.asset_type == "INDEX":
            if inst is not None:
                raise TagNotAllowedError("指数不支持标签，仅股票 / ETF 可设置标签")
            raise WatchlistEntryNotFoundError(f"自选中不存在: {instrument_id}")
        row = self.repo.get(instrument_id)
        if row is None:
            raise WatchlistEntryNotFoundError(f"自选中不存在: {instrument_id}")

        tag_repo = TagRepository(self.session)
        tags: list[Tag] = []
        for tag_id in dict.fromkeys(tag_ids):  # 去重且保序
            tag = tag_repo.get(tag_id)
            if tag is None:
                from app.services.tag_service import TagNotFoundError

                raise TagNotFoundError(f"标签不存在: {tag_id}")
            tags.append(tag)

        # 全量替换：清空旧关联后重建（一个条目可关联多个标签，v0.03b 多对多）
        with write_coordinator.write():
            self.session.execute(
                sa_delete(WatchlistTag).where(WatchlistTag.instrument_id == instrument_id)
            )
            for tag in tags:
                self.session.add(WatchlistTag(instrument_id=instrument_id, tag_id=tag.tag_id))
            self.session.commit()
        logger.info(
            "已更新自选标签: %s -> %s",
            instrument_id,
            "、".join(t.name for t in tags) if tags else "无标签",
        )
        return inst, row.sort_order, tags


class IndexWatchlistService(BaseWatchlistService):
    allowed_asset_types = frozenset({"INDEX"})

    def _make_repo(self, session: Session) -> IndexWatchlistRepository:
        return IndexWatchlistRepository(session)
