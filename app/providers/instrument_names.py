"""证券名称识别 Provider（腾讯批量接口，识别失败返回 None）。"""

from __future__ import annotations

import logging

from app.providers.quote.tencent import TencentQuoteClient, to_tencent_code

logger = logging.getLogger(__name__)


class AkshareInstrumentNameProvider:
    """通过腾讯行情接口识别证券名称；接口异常时返回 None（调用方返回 404）。"""

    def __init__(self, client: TencentQuoteClient | None = None):
        self.client = client or TencentQuoteClient()
        self._cache: dict[tuple[str, str, str], str | None] = {}

    def get_name(self, market: str, asset_type: str, symbol: str) -> str | None:
        key = (market.upper(), asset_type.upper(), symbol)
        if key in self._cache:
            return self._cache[key]

        code = to_tencent_code(*key)
        if code is None:
            return None
        try:
            fields = self.client.fetch_one(code)
        except Exception:
            logger.exception("名称识别请求失败: %s", code)
            return None

        name = fields[1].strip() if fields else None
        self._cache[key] = name
        if name is None:
            logger.info("名称识别失败: %s/%s/%s", *key)
        return name
