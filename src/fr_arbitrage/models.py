"""Data models for the Hyperliquid Yield Harvester.

Includes:
- SQLAlchemy ORM model for position persistence (README §5.1)
- Pydantic models for in-memory state
- Dataclasses for market state and asset metadata (README §4.1)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from pydantic import BaseModel
from sqlalchemy import Column, DateTime, Float, String, func
from sqlalchemy.orm import DeclarativeBase


# ---------------------------------------------------------------------------
# SQLAlchemy Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""
    pass


# ---------------------------------------------------------------------------
# Position — DB-persisted (README §5.1)
# ---------------------------------------------------------------------------

class Position(Base):
    """Tracks an open delta-neutral position (Spot + Perp Short).

    Stored in the ``positions`` SQLite table for crash recovery.
    """

    __tablename__ = "positions"

    symbol: str = Column(String, primary_key=True)  # e.g. "HYPE"
    spot_sz: float = Column(Float, default=0.0)  # Spot quantity held
    perp_sz: float = Column(Float, default=0.0)  # Perp short quantity
    entry_price: float = Column(Float, default=0.0)  # Weighted avg entry
    accumulated_funding: float = Column(Float, default=0.0)  # Total FR earned
    state: str = Column(String, default="OPEN")  # OPEN / REBALANCING / CLOSING_PENDING
    updated_at: datetime = Column(  # type: ignore[assignment]
        DateTime(timezone=True),
        default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<Position {self.symbol} spot={self.spot_sz} perp={self.perp_sz} "
            f"state={self.state}>"
        )


# ---------------------------------------------------------------------------
# AssetMeta — metadata per coin (README §4.1)
# ---------------------------------------------------------------------------

@dataclass
class AssetMeta:
    """Hyperliquid asset metadata for precise rounding.

    Loaded once from ``info.meta()`` and ``info.spot_meta()`` at startup.
    """

    coin: str
    perp_asset_id: Optional[int] = None  # Index in meta.universe
    spot_asset_id: Optional[int] = None  # 10000 + index in spotMeta.universe
    spot_name: Optional[str] = None  # Canonical name for WS (e.g. "@107", "PURR/USDC")
    sz_decimals: int = 0  # Size precision
    px_decimals: int = 2  # Price precision (derived from tick size)


# ---------------------------------------------------------------------------
# MarketState — in-memory, updated by WebSocket (README §3.1)
# ---------------------------------------------------------------------------

@dataclass
class MarketState:
    """Real-time market data for a single coin, kept in memory."""

    coin: str
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid_price: float = 0.0
    funding_rate: float = 0.0  # Current predicted hourly FR
    open_interest: float = 0.0  # Open interest in USD
    spot_best_bid: float = 0.0
    spot_best_ask: float = 0.0
    spot_mid_price: float = 0.0
    last_updated: float = 0.0  # Unix timestamp
    funding_rate_history: List[Tuple[float, float]] = field(default_factory=list)  # [(timestamp, FR)]

    @property
    def ma_funding_rate(self) -> float:
        """Moving average of the funding rate over the tracked history."""
        if not self.funding_rate_history:
            return self.funding_rate
        # Calculate simple average of the historical rates
        total = sum(fr for _, fr in self.funding_rate_history)
        return total / len(self.funding_rate_history)

    @property
    def perp_spot_spread(self) -> float:
        """(Spot Ask - Perp Bid) / Spot Ask — cost to enter."""
        if self.spot_best_ask <= 0:
            return float("inf")
        return (self.spot_best_ask - self.best_bid) / self.spot_best_ask


# ---------------------------------------------------------------------------
# TargetSymbol — scan result passed to execution engine
# ---------------------------------------------------------------------------

class TargetSymbol(BaseModel):
    """A coin that passes the OpportunityScanner filters."""

    coin: str  # e.g. "HYPE"
    funding_rate: float
    spot_ask: float
    perp_bid: float
    spread: float  # perp_spot_spread
    open_interest: float
