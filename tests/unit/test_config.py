"""配置读取单测：Token 读取、缺失提示、日志不输出 Token。"""

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
