"""Application settings — Pydantic-based configuration for Hyperliquid Yield Harvester.

Corresponds to README §6.
"""

from __future__ import annotations

from typing import List, Optional
from pydantic import model_validator
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

    # --- WandB Logging ------------------------------------------------------
    wandb_enabled: bool = False
    wandb_project: str = "fr-arbitrage"
    wandb_entity: str = ""  # Optional: Username or Team name
    wandb_api_key: str = ""  # Optional: from env var WANDB_API_KEY

    # Auto-enable WandB if API key is present and enabled isn't explicitly False
    # (Pydantic validator would be ideal, but simple post-init logic in main is also fine.
    #  Let's use a validator if possible, or just default enabled=True if key is there? 
    #  Simpler: If key is present, default enabled to True in post-init or use validator)
    
    
    @model_validator(mode="after")
    def _enable_wandb_if_key_present(self) -> Settings:

        if self.wandb_api_key and not self.wandb_enabled:
             # Only auto-enable if it wasn't explicitly disabled? 
             # Actually, if user provided key, they probably want it.
             # But 'wandb_enabled' defaults to False.
             # So if key is present, we flip it to True.
             self.wandb_enabled = True
        return self

    # --- Scan / Guardian intervals (seconds) --------------------------------

    scan_interval_sec: int = 60
    guardian_interval_sec: int = 30

    # --- Safety -------------------------------------------------------------
    emergency_stop: bool = False
