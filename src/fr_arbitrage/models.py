"""Pydantic data models for the FR-Arbitrage Bot.

Corresponds to README §5 — Data Model Design.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 5.1  TargetSymbol — In-memory scan result
# ---------------------------------------------------------------------------

class TargetSymbol(BaseModel):
    """A market pair that passes the MarketScanner filters.

    Kept in-memory only; not persisted to the database.
    """

    symbol: str              # e.g. "DOGE/USDT"
    funding_rate: float      # e.g. 0.0004 (= 0.04%)
    spot_price: float
    perp_price: float
    spread_pct: float        # (perp - spot) / spot
    volume_24h: float        # 24h volume in USDT


# ---------------------------------------------------------------------------
# 5.2  ActivePosition — Persisted to SQLite (`positions` table)
# ---------------------------------------------------------------------------

class ActivePosition(BaseModel):
    """Tracks an open delta-neutral position (Spot + Perp Short).

    Stored in the `positions` SQLite table for crash recovery.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str
    entry_timestamp: int = Field(
        default_factory=lambda: int(datetime.now(timezone.utc).timestamp())
    )
    spot_qty: float          # Spot quantity held
    perp_qty: float          # Perp short quantity (positive = short size)
    entry_spread: float      # Spread % at entry
    total_fees: float = 0.0  # Cumulative fees in USDT
    status: str = "OPEN"     # "OPEN" | "CLOSING" | "CLOSED"
