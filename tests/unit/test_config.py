"""配置读取单测：Token 读取、缺失提示、日志不输出 Token、history 兼容换算（D13）。"""

from __future__ import annotations

import logging

import pytest
import yaml

from app.config import AppConfig, load_config


def test_load_config_reads_tushare_token(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "database": {"url": "duckdb:///./data/test.duckdb"},
                "tushare": {"token": "real-token-abc123"},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(cfg_file)
    assert config.tushare.token == "real-token-abc123"
    assert config.has_tushare_token is True
    # 默认刷新周期 60 秒、stale 阈值 180 秒
    assert config.quote.refresh_seconds == 60
    assert config.quote.stale_seconds == 180


def test_missing_token_still_starts_and_warns(tmp_path, caplog):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump({"database": {"url": "duckdb:///./x.duckdb"}}), encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        config = load_config(cfg_file)
    assert config.has_tushare_token is False
    assert any("Token" in r.message for r in caplog.records)


def test_placeholder_token_treated_as_missing(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump({"tushare": {"token": "YOUR_TUSHARE_TOKEN"}}), encoding="utf-8"
    )
    assert load_config(cfg_file).has_tushare_token is False


def test_missing_file_uses_defaults(tmp_path):
    config = load_config(tmp_path / "not_exist.yaml")
    assert isinstance(config, AppConfig)
    assert config.quote.refresh_seconds == 60


def test_log_does_not_leak_token(tmp_path, caplog):
    cfg_file = tmp_path / "config.yaml"
    secret = "super-secret-token-xyz"
    cfg_file.write_text(yaml.safe_dump({"tushare": {"token": secret}}), encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="app.config"):
        load_config(cfg_file)
    assert secret not in caplog.text


class TestHistoryMaxRetriesCompatibility:
    """D13：max_retries 与 max_attempts 的兼容换算与三分支测试。"""

    def test_default_max_retries_and_max_attempts(self) -> None:
        """缺省配置：max_retries=3，max_attempts=10（各自默认值，不触发换算）。"""
        config = AppConfig()
        assert config.history.max_retries == 3
        assert config.history.max_attempts == 10

    def test_only_max_retries_configured_uses_directly(self, tmp_path) -> None:
        """仅显式配置 max_retries → 直接使用，不触发 WARNING。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump({"history": {"max_retries": 5}}), encoding="utf-8"
        )
        config = load_config(cfg_file)
        assert config.history.max_retries == 5
        # max_attempts 仍保持默认 10（未参与换算时不变）
        assert config.history.max_attempts == 10

    def test_only_max_attempts_configured_triggers_compat_warning(
        self, tmp_path, caplog
    ) -> None:
        """仅显式配置 max_attempts 且未配置 max_retries → 换算 + WARNING。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump({"history": {"max_attempts": 6}}), encoding="utf-8"
        )
        with caplog.at_level(logging.WARNING, logger="app.config"):
            config = load_config(cfg_file)
        # 换算：max_retries = max_attempts - 1 = 5
        assert config.history.max_retries == 5
        assert config.history.max_attempts == 6
        # WARNING 日志存在且包含弃用提示
        assert any(
            "已弃用" in r.message and "max_attempts" in r.message
            for r in caplog.records
        )
        assert any(
            "max_retries=5" in r.message or "max_retries = 5" in r.message
            for r in caplog.records
        )

    def test_both_configured_max_retries_takes_precedence(
        self, tmp_path, caplog
    ) -> None:
        """两者都配置 → max_retries 为准，不触发 WARNING（仅 INFO 提示）。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {"history": {"max_attempts": 10, "max_retries": 3}}
            ),
            encoding="utf-8",
        )
        with caplog.at_level(logging.INFO, logger="app.config"):
            config = load_config(cfg_file)
        # max_retries 为准
        assert config.history.max_retries == 3
        assert config.history.max_attempts == 10
        # 不应有 WARNING 级别的兼容提示
        warning_msgs = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not any("已弃用" in m for m in warning_msgs)

    def test_max_attempts_1_floors_max_retries_to_0(self, tmp_path) -> None:
        """max_attempts=1 时换算为 max_retries=0（不产生负值）。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump({"history": {"max_attempts": 1}}), encoding="utf-8"
        )
        config = load_config(cfg_file)
        assert config.history.max_retries == 0


class TestEtfConfigDefaults:
    """ETF 配置项默认值与显式配置覆盖测试（etf-data-module）。"""

    def test_etf_config_defaults(self) -> None:
        """缺省配置：ETF 启用，默认请求间隔 0.5 秒，universe 刷新 24 小时。"""
        config = AppConfig()
        assert config.history.etf_enabled is True
        assert config.history.etf_request_min_interval_seconds == 0.5
        assert config.history.etf_universe_refresh_hours == 24
        assert config.providers.history.etf_daily == "eastmoney"
        assert config.providers.history.etf_adj_factor == "tushare"
        assert config.history.availability.etf_daily == "16:30"
        assert config.history.availability.etf_adj_factor == "09:30"

    def test_etf_enabled_false(self, tmp_path) -> None:
        """显式配置 etf_enabled=false 正确读取。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump({"history": {"etf_enabled": False}}), encoding="utf-8"
        )
        config = load_config(cfg_file)
        assert config.history.etf_enabled is False

    def test_etf_config_explicit_override(self, tmp_path) -> None:
        """显式配置覆盖 ETF 配置项。"""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.safe_dump(
                {
                    "history": {
                        "etf_enabled": True,
                        "etf_request_min_interval_seconds": 1.0,
                        "etf_universe_refresh_hours": 12,
                        "availability": {
                            "etf_daily": "17:00",
                            "etf_adj_factor": "10:00",
                        },
                    },
                    "providers": {
                        "history": {
                            "etf_daily": "tushare",
                            "etf_adj_factor": "eastmoney",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        config = load_config(cfg_file)
        assert config.history.etf_enabled is True
        assert config.history.etf_request_min_interval_seconds == 1.0
        assert config.history.etf_universe_refresh_hours == 12
        assert config.providers.history.etf_daily == "tushare"
        assert config.providers.history.etf_adj_factor == "eastmoney"
        assert config.history.availability.etf_daily == "17:00"
        assert config.history.availability.etf_adj_factor == "10:00"
