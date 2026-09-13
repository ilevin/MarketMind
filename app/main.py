"""应用入口：FastAPI、Jinja2 页面、静态资源、健康检查、后台任务生命周期。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, load_config
from app.db import check_database, create_db_engine, make_session_factory
from app.version import APP_VERSION

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def setup_logging(config: AppConfig) -> None:
    logging.basicConfig(
        level=getattr(logging, config.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _all_watchlist_ids(session_factory) -> list[str]:
    """启动预热：普通自选 + 指数配置的全部 instrument_id。"""
    from app.repositories.watchlist import IndexWatchlistRepository, WatchlistRepository

    with session_factory() as session:
        ids = [row.instrument_id for row, _ in WatchlistRepository(session).list_ordered()]
        ids += [row.instrument_id for row, _ in IndexWatchlistRepository(session).list_ordered()]
        return ids


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config()
    setup_logging(config)
    logger = logging.getLogger(__name__)

    engine = create_db_engine(config.database.url)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("应用启动")
        # 数据库结构由 Alembic 迁移负责（容器 CMD 先执行 alembic upgrade head，失败即退出）；
        # 此处不再 create_all，避免掩盖迁移遗漏导致 schema 漂移（v0.03 design D5）。
        logger.info("数据库结构由 Alembic 管理: %s", config.database.url)

        from app.providers.trading_calendar.provider import TushareTradingCalendarProvider
        from app.repositories.quote import QuoteSnapshotRepository
        from app.repositories.trading_calendar import TradingCalendarRepository
        from app.services.market_session_service import MarketSessionService
        from app.services.quote_cache import QuoteCache
        from app.services.refresh_service import RefreshService

        with session_factory() as session:
            calendar_repo = TradingCalendarRepository(session)
        calendar = TushareTradingCalendarProvider(config, calendar_repo)
        session_service = MarketSessionService(calendar)

        cache = QuoteCache(stale_seconds=config.quote.stale_seconds)
        with session_factory() as session:
            cache.warmup(QuoteSnapshotRepository(session).latest_many(
                _all_watchlist_ids(session_factory)
            ))

        refresh_service = RefreshService(
            config, session_factory, app.state.quote_providers, session_service, cache
        )
        app.state.session_service = session_service
        app.state.quote_cache = cache
        app.state.refresh_service = refresh_service

        from app.jobs.fundamental_refresh import FundamentalRefreshJob
        from app.observability.provider_metrics import TimedFundamentalProvider
        from app.providers.fundamental.tushare import TushareFundamentalProvider
        from app.services.job_status_service import JobStatusService

        job_status_service = JobStatusService(session_factory)
        app.state.job_status_service = job_status_service

        fundamental_job = FundamentalRefreshJob(
            config,
            session_factory,
            TimedFundamentalProvider(
                TushareFundamentalProvider(config),
                app.state.provider_metrics,
                config.providers.timeout.tushare,
            ),
            session_service,
            job_status_service,
        )
        app.state.fundamental_refresh = fundamental_job

        from app.jobs.quote_refresh import QuoteRefreshJob

        job = QuoteRefreshJob(config, refresh_service, job_status_service)
        await job.start()
        await fundamental_job.start()
        try:
            yield
        finally:
            await job.stop()
            await fundamental_job.stop()
            logger.info("应用已停止")

    app = FastAPI(title="股票与 ETF 行情看板", lifespan=lifespan)
    app.state.config = config
    app.state.session_factory = session_factory

    from app.observability.provider_metrics import ProviderMetricsRegistry
    from app.providers.instrument_names import AkshareInstrumentNameProvider
    from app.providers.quote import QuoteProviderRegistry

    provider_metrics = ProviderMetricsRegistry()
    app.state.provider_metrics = provider_metrics
    app.state.name_provider = AkshareInstrumentNameProvider()
    app.state.quote_providers = QuoteProviderRegistry(config, provider_metrics)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # 版本号全局注入：全部模板可直接使用 {{ app_version }}（v0.03 design D13）
    templates.env.globals["app_version"] = APP_VERSION
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # API 路由
    from app.api import admin, index_watchlist, quotes, status, tags, watchlist

    app.include_router(quotes.router)
    app.include_router(watchlist.router)
    app.include_router(index_watchlist.router)
    app.include_router(admin.router)
    app.include_router(status.router)
    app.include_router(tags.router)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request, exc):
        """兜底：任何未处理异常不允许变成裸 500 堆栈泄露，也不让进程退出。"""
        logger.exception("未处理异常: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "服务内部错误，请稍后重试"})

    @app.get("/health")
    def health():
        """健康检查：只查应用与数据库，不调用 AKShare / Tushare；返回当前版本。"""
        with session_factory() as session:
            db_ok = check_database(session)
        status = "ok" if db_ok else "error"
        return JSONResponse(
            status_code=200 if db_ok else 503,
            content={
                "status": status,
                "database": "ok" if db_ok else "error",
                "version": APP_VERSION,
            },
        )

    @app.get("/")
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html")

    @app.get("/watchlist")
    def watchlist_page(request: Request):
        return templates.TemplateResponse(request, "watchlist.html")

    @app.get("/tags")
    def tags_page(request: Request):
        return templates.TemplateResponse(request, "tags.html")

    return app


app = create_app()
