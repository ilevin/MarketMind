"""行情与指数查询 API：只读缓存，浏览器不直接触发数据源请求。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import (
    IndexQuoteItem,
    IndicesResponse,
    QuoteItem,
    QuotesResponse,
    TagBrief,
)
from app.services.market_session_service import MarketStatus
from app.services.quote_cache import QuoteCache

router = APIRouter(prefix="/api", tags=["quotes"])


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def _assemble(
    request: Request,
    asset_type_filter: set[str],
    tag_id: int | None = None,
    untagged: bool = False,
) -> tuple[list, dict]:
    """从缓存 + DuckDB 快照回退组装行情条目，并返回市场状态。

    tag_id / untagged 仅对股票/ETF 生效：标签是纯展示层过滤，
    不触发任何 Provider 请求（v0.03 技术方案 §12.2）。
    """
    app = request.app
    session_service = app.state.session_service
    cache: QuoteCache = app.state.quote_cache

    from app.repositories.fundamental import FundamentalRepository
    from app.repositories.watchlist import IndexWatchlistRepository, WatchlistRepository

    with app.state.session_factory() as session:
        tags_map: dict[str, list] = {}
        if asset_type_filter == {"INDEX"}:
            pairs = [
                (inst, row.sort_order)
                for row, inst in IndexWatchlistRepository(session).list_ordered()
            ]
            fundamentals: dict = {}
        else:
            triples = [
                (row, inst, tags)
                for row, inst, tags in WatchlistRepository(session).list_ordered_with_tags()
                if inst.asset_type in asset_type_filter
            ]
            if tag_id is not None:
                # 命中"包含该标签"的条目（多对多，v0.03b）
                triples = [t for t in triples if any(x.tag_id == tag_id for x in t[2])]
            elif untagged:
                triples = [t for t in triples if not t[2]]
            tags_map = {row.instrument_id: tags for row, _inst, tags in triples}
            pairs = [(inst, row.sort_order) for row, inst, _tags in triples]
            fundamentals = FundamentalRepository(session).latest_many(
                [inst.instrument_id for inst, _ in pairs]
            )

        statuses = session_service.all_status()
        market_status = {m: s.value for m, s in statuses.items()}

        # 内存未命中的标的从 DuckDB 最近快照回退（重启/失败场景）
        missing = [
            iid for iid, _ in ((i.instrument_id, o) for i, o in pairs)
            if cache.get(iid) is None
        ]
        if missing:
            from app.repositories.quote import QuoteSnapshotRepository

            cache.warmup(QuoteSnapshotRepository(session).latest_many(missing))

        items = []
        for inst, _order in pairs:
            cached = cache.get(inst.instrument_id)
            status = statuses.get(inst.market, MarketStatus.CLOSED)
            quote = cached.quote if cached else None
            fund = fundamentals.get(inst.instrument_id)
            # 数据源不提供数据时间时（如 akshare tx 通道），回退显示抓取时间
            source_ts = None
            if quote is not None:
                source_ts = quote.source_timestamp or (cached.fetched_at if cached else None)

            if inst.asset_type == "INDEX":
                items.append(
                    IndexQuoteItem(
                        instrument_id=inst.instrument_id,
                        symbol=inst.symbol,
                        name=inst.name,
                        market=inst.market,
                        asset_type=inst.asset_type,
                        price=quote.price if quote else None,
                        change_percent=quote.change_percent if quote else None,
                        quote_source=quote.source if quote else None,
                        source_timestamp=_iso(source_ts),
                        is_stale=cache.is_stale(inst.instrument_id, status),
                    )
                )
            else:
                tags = tags_map.get(inst.instrument_id, [])
                items.append(
                    QuoteItem(
                        instrument_id=inst.instrument_id,
                        symbol=inst.symbol,
                        name=inst.name,
                        market=inst.market,
                        asset_type=inst.asset_type,
                        price=quote.price if quote else None,
                        change_percent=quote.change_percent if quote else None,
                        volume_ratio=quote.volume_ratio if quote else None,
                        pe_ttm=fund.pe_ttm if fund else None,
                        pb=fund.pb if fund else None,
                        dividend_yield_ttm=fund.dividend_yield_ttm if fund else None,
                        quote_source=quote.source if quote else None,
                        fundamental_source=fund.source if fund else None,
                        source_timestamp=_iso(source_ts),
                        is_stale=cache.is_stale(inst.instrument_id, status),
                        delayed=quote.delayed if quote else False,
                        tags=[TagBrief(id=t.tag_id, name=t.name) for t in tags],
                    )
                )
        return items, market_status


@router.get("/quotes", response_model=QuotesResponse)
def get_quotes(
    request: Request,
    tag_id: int | None = Query(default=None, description="仅返回关联该标签的条目"),
    untagged: bool = Query(default=False, description="仅返回无标签条目"),
):
    if tag_id is not None and untagged:
        raise HTTPException(
            status_code=422, detail="tag_id 与 untagged 不能同时使用"
        )
    items, market_status = _assemble(
        request, {"STOCK", "ETF"}, tag_id=tag_id, untagged=untagged
    )
    return QuotesResponse(market_status=market_status, items=items)


@router.get("/indices", response_model=IndicesResponse)
def get_indices(request: Request):
    items, market_status = _assemble(request, {"INDEX"})
    return IndicesResponse(items=items, market_status=market_status)
