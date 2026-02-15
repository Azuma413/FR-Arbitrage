"""Application settings loaded from .env file via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global configuration for the FR-Arbitrage Bot.

    Values are loaded from a `.env` file in the project root.
    Any field can be overridden by setting the corresponding environment variable.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Exchange -----------------------------------------------------------
    exchange_name: str = "bybit"  # "bybit" or "binance"

    # --- API Credentials ----------------------------------------------------
    api_key: str = ""
    api_secret: str = ""

    # --- Trading Parameters -------------------------------------------------
    investment_amount_usdt: float = 1000.0
    max_open_positions: int = 3

    # --- MarketScanner Thresholds -------------------------------------------
    min_funding_rate: float = 0.0003      # 0.03% per 8h
    min_volume_24h: float = 10_000_000.0  # 10M USDT
    min_spread_pct: float = 0.002         # 0.2%

    # --- Exit Thresholds ----------------------------------------------------
    exit_funding_rate: float = 0.00005    # 0.005%
    exit_spread_pct: float = -0.01        # -1.0%

    # --- Safety -------------------------------------------------------------
    emergency_stop: bool = False

    # --- Scan Interval (seconds) --------------------------------------------
    scan_interval_sec: int = 60
