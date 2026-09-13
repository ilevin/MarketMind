"""统一配置管理：启动时读取 config.yaml -> Pydantic 模型，注入各 Provider/Service。

业务代码禁止直接读取 YAML（见 design.md D7）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 全部业务时间统一使用的时区（CN 与 HK 均为 UTC+8 且无夏令时，见 design.md D5.1）
BUSINESS_TZ_NAME = "Asia/Shanghai"

DEFAULT_CONFIG_PATH = Path("config.yaml")


class DatabaseConfig(BaseModel):
    url: str = "duckdb:///./data/marketmind.duckdb"


class QuoteConfig(BaseModel):
    refresh_seconds: int = Field(default=60, ge=1)
    stale_seconds: int = Field(default=180, ge=1)


class TushareConfig(BaseModel):
    token: str = ""


class QuoteProviderConfig(BaseModel):
    # 默认值与部署环境实测结果一致：A股股票走 akshare(腾讯通道)，
    # 其余资产走 tencent 批量接口（东财/新浪接口在当前环境不可用，见 providers/quote/）
    cn_stock: str = "akshare"
    cn_etf: str = "tencent"
    hk_stock: str = "tencent"
    hk_etf: str = "tencent"
    cn_index: str = "tencent"
    hk_index: str = "tencent"


class FundamentalProviderConfig(BaseModel):
    cn_stock: str = "tushare"


class TimeoutConfig(BaseModel):
    """第三方 Provider 超时（秒，技术方案 §27；数值可按线上耗时调整）。"""

    tencent: float = 8
    akshare: float = 45
    tushare: float = 15


class ProvidersConfig(BaseModel):
    quote: QuoteProviderConfig = Field(default_factory=QuoteProviderConfig)
    fundamental: FundamentalProviderConfig = Field(default_factory=FundamentalProviderConfig)
    timeout: TimeoutConfig = Field(default_factory=TimeoutConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"


class AppConfig(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    quote: QuoteConfig = Field(default_factory=QuoteConfig)
    tushare: TushareConfig = Field(default_factory=TushareConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def has_tushare_token(self) -> bool:
        """Token 是否已配置且非占位符。日志禁止输出 Token 明文。"""
        token = self.tushare.token.strip()
        return bool(token) and token != "YOUR_TUSHARE_TOKEN"


def load_config(path: Path | str | None = None) -> AppConfig:
    """读取 config.yaml。文件缺失或字段缺失时使用默认值并记录警告（允许无 Token 启动）。"""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH

    raw: dict = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        logger.info("已加载配置文件: %s", config_path)
    else:
        logger.warning("配置文件 %s 不存在，使用默认配置", config_path)

    config = AppConfig.model_validate(raw or {})

    if not config.has_tushare_token:
        # 明确的配置错误提示；不输出 Token 本身
        logger.warning("Tushare Token 未配置（config.yaml -> tushare.token），估值功能将不可用")

    return config
