from __future__ import annotations

import os
from pathlib import Path
from enum import Enum
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "assets.yaml"


class AgentMode(str, Enum):
    PAPER_CHALLENGE = "PAPER_CHALLENGE"
    ALERT_ONLY = "ALERT_ONLY"
    REPLAY = "REPLAY"
    PAUSED = "PAUSED"


class AssetConfig(BaseModel):
    symbol: str
    kraken_pair: str
    coinbase_pair: str
    active: bool = True


class AppSettings(BaseSettings):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_owner_ids: str = ""
    telegram_chat_ids: str = ""
    agent_mode: AgentMode = AgentMode.PAPER_CHALLENGE
    beginner_explanations: bool = True
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'challenge.db'}"
    app_env: str = "development"

    signal_expiry_minutes: int = 30
    strategy_version: str = "1.0"
    live_trading_enabled: bool = False

    starting_balance: float = 1000.0
    win_level: float = 1120.0
    loss_level: float = 950.0
    risk_per_trade_pct_min: float = 0.015
    risk_per_trade_pct_max: float = 0.022
    risk_per_trade_pct_default: float = 0.018
    max_total_open_risk_pct: float = 0.04
    max_open_positions: int = 2
    commission_pct: float = 0.0026
    spread_pct: float = 0.001
    divergence_threshold_pct: float = 0.015
    max_provider_price_divergence_pct: float = 0.05

    min_valid_candles: int = 250
    target_fetch_candles: int = 300
    max_daily_candle_age_hours: int = 30

    timezone: str = "Asia/Jerusalem"
    active_hours_start: int = 8
    active_hours_end: int = 23
    check_interval_minutes: int = 15

    ignore_below_pct: float = 0.01
    analyze_pct: float = 0.03
    important_pct: float = 0.05
    emergency_pct: float = 0.08
    vertical_spike_pct: float = 0.08

    take_profit_risk_multiple: float = 2.0

    assets: list[AssetConfig] = Field(default_factory=list)

    model_config = {"env_prefix": "", "case_sensitive": False}


def load_config() -> AppSettings:
    overrides: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            raw = yaml.safe_load(f)
        if raw and "settings" in raw:
            overrides.update(raw["settings"])
        if raw and "thresholds" in raw:
            overrides.update(raw["thresholds"])
        if raw and "assets" in raw:
            overrides["assets"] = [AssetConfig(**a) for a in raw["assets"]]
    settings = AppSettings(**overrides)
    return settings


settings = load_config()
