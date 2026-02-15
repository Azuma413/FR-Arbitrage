"""Application settings — Pydantic-based configuration for Hyperliquid Yield Harvester.

Corresponds to README §6.
"""

from __future__ import annotations

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global configuration for the Hyperliquid Yield Harvester.

    Values are loaded from a `.env` file in the project root.
    Any field can be overridden by setting the corresponding environment variable.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Network & Auth -----------------------------------------------------
    environment: str = "MAINNET"  # "MAINNET" or "TESTNET"
    private_key: str = ""  # 0x... Ethereum private key
    account_address: str = ""  # wallet address derived from key

    # --- Strategy Parameters ------------------------------------------------
    target_coins: List[str] = ["HYPE", "PURR", "SOL", "ETH"]
    blacklist_coins: List[str] = []

    # Entry Filters
    min_funding_rate_hourly: float = 0.00002  # 0.002% per hour
    max_entry_spread: float = 0.001  # 0.1% spread limit
    min_daily_volume: float = 1_000_000.0  # $1M volume

    # Execution Parameters
    slippage_tolerance: float = 0.002  # 0.2%
    max_retry_attempts: int = 3

    # Position Sizing
    max_position_usdc: float = 1000.0  # Max USDC per coin
    max_open_positions: int = 5  # Max concurrent positions
    leverage_buffer: float = 0.55  # Perp collateral ratio (55% to perp)

    # --- Dry-Run Mode -------------------------------------------------------
    dry_run: bool = False  # Paper trading: Mainnet data, simulated fills

    # --- Position Guardian --------------------------------------------------
    exit_negative_fr_count: int = 3  # Exit after N consecutive negative FR
    margin_usage_threshold: float = 0.80  # Trigger rebalance at 80%

    # --- System -------------------------------------------------------------
    db_url: str = "sqlite+aiosqlite:///./yield_harvester.db"
    log_level: str = "INFO"

    # --- Scan / Guardian intervals (seconds) --------------------------------
    scan_interval_sec: int = 60
    guardian_interval_sec: int = 30

    # --- Safety -------------------------------------------------------------
    emergency_stop: bool = False
