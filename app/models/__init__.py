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
]
