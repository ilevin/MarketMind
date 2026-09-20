"""SQLAlchemy 模型汇总；建表时由此导入注册。"""

from app.models.instrument import Instrument
from app.models.watchlist import Watchlist, IndexWatchlist
from app.models.quote import QuoteSnapshot
from app.models.fundamental import FundamentalSnapshot
from app.models.setting import AppSetting
from app.models.trading_calendar import TradingCalendarDay
from app.models.tag import Tag
from app.models.job_status import JobStatus
from app.models.watchlist_tag import WatchlistTag
from app.models.user import AppUser
from app.models.user_session import UserSession
from app.models.history_market import CnStockBasic, CnStockCompany, CnStockNameChange
from app.models.history_sync import (
    DatasetKind,
    DatasetName,
    DatasetStatus,
    HistoryDayStatus,
    HistorySyncRun,
    HistorySyncRunDataset,
    HistorySyncState,
    RunDatasetStatus,
    RunStatus,
    TriggerType,
)
import app.models.history_fact  # noqa: F401  注册四张 Core 事实表到 Base.metadata

__all__ = [
    "Instrument",
    "Watchlist",
    "IndexWatchlist",
    "QuoteSnapshot",
    "FundamentalSnapshot",
    "AppSetting",
    "TradingCalendarDay",
    "Tag",
    "JobStatus",
    "WatchlistTag",
    "AppUser",
    "UserSession",
    # --- a-share-historical-data ---
    "CnStockBasic",
    "CnStockCompany",
    "CnStockNameChange",
    "HistorySyncState",
    "HistoryDayStatus",
    "HistorySyncRun",
    "HistorySyncRunDataset",
    "DatasetName",
    "DatasetKind",
    "DatasetStatus",
    "TriggerType",
    "RunStatus",
    "RunDatasetStatus",
]
