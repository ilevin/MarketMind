"""标签业务服务（v0.03）：名称校验、查重、引用计数与删除保护（技术方案 §4/§7/§9）。

删除保护为双层：本服务先做引用计数检查并抛出带中文提示的 TagInUseError（409），
数据库 RESTRICT 外键作为绕过业务层时的兜底（design D4）。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.db import write_coordinator
from app.models.tag import Tag
from app.repositories.tag import TagRepository

logger = logging.getLogger(__name__)

MAX_TAG_NAME_LENGTH = 50


class TagServiceError(Exception):
    """标签业务错误基类。"""


class TagNameEmptyError(TagServiceError):
    """名称去空格后为空。"""


class TagNameTooLongError(TagServiceError):
    """名称超长。"""


class DuplicateTagNameError(TagServiceError):
    """名称重复。"""


class TagNotFoundError(TagServiceError):
    """标签不存在。"""


class TagInUseError(TagServiceError):
    """标签仍被自选条目引用，禁止删除。"""


class TagService:
    """标签业务服务（multi-user-auth：命名空间为同一用户内）。"""

    def __init__(self, session: Session, user_id: int):
        # 身份由认证依赖解析注入（CurrentUser），不接受客户端传入归属 user_id
        self.session = session
        self.user_id = user_id
        self.repo = TagRepository(session, user_id)

    def _validate_name(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise TagNameEmptyError("标签名称不能为空")
        if len(name) > MAX_TAG_NAME_LENGTH:
            raise TagNameTooLongError(f"标签名称不能超过 {MAX_TAG_NAME_LENGTH} 个字符")
        return name

    def list_with_usage(self) -> list[tuple[Tag, int]]:
        return self.repo.list_with_usage()

    def create(self, name: str) -> Tag:
        name = self._validate_name(name)
        if self.repo.get_by_name(name) is not None:
            raise DuplicateTagNameError(f"标签名称已存在: {name}")
        with write_coordinator.write():
            tag = self.repo.create(name)
            self.session.commit()
        logger.info("已创建标签: %s (id=%s)", name, tag.tag_id)
        return tag

    def rename(self, tag_id: int, name: str) -> Tag:
        tag = self._require(tag_id)
        name = self._validate_name(name)
        existing = self.repo.get_by_name(name)
        if existing is not None and existing.tag_id != tag_id:
            raise DuplicateTagNameError(f"标签名称已存在: {name}")
        with write_coordinator.write():
            tag = self.repo.rename(tag, name)
            self.session.commit()
        logger.info("已修改标签: id=%s -> %s", tag_id, name)
        return tag

    def delete(self, tag_id: int) -> None:
        tag = self._require(tag_id)
        usage = self.repo.count_usage(tag_id)
        if usage > 0:
            raise TagInUseError(
                f"标签“{tag.name}”当前被 {usage} 个证券使用，不能删除。"
                "请先解除这些证券与标签的关联。"
            )
        with write_coordinator.write():
            self.repo.delete(tag)
            self.session.commit()
        logger.info("已删除标签: %s (id=%s)", tag.name, tag_id)

    def _require(self, tag_id: int) -> Tag:
        tag = self.repo.get(tag_id)
        if tag is None:
            raise TagNotFoundError(f"标签不存在: {tag_id}")
        return tag
