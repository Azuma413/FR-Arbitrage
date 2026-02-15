"""MockExchange — CSV-backed exchange simulator for backtesting.

Implements the subset of the ccxt Exchange interface used by
MarketScanner and OrderManager, driven by historical OHLCV + FR data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class SimulatedTrade:
    """A single simulated fill recorded by MockExchange."""

    id: str
    timestamp: int          # Unix ms
    symbol: str
    side: str               # "buy" | "sell"
    qty: float
    price: float
    fee_cost: float
    fee_currency: str = "USDT"


# ---------------------------------------------------------------------------
# MockExchange
# ---------------------------------------------------------------------------

class MockExchange:
    """A deterministic, CSV-driven mock of the ccxt async Exchange.

    Parameters
    ----------
    symbol : str
        Trading pair in ccxt format, e.g. ``"DOGE/USDT"``.
    data_dir : str | Path
        Directory containing the CSV files produced by ``fetch_data.py``.
    slippage_pct : float
        Simulated slippage applied to market orders (default 0.05%).
    fee_rate : float
        Fee rate per fill (default 0.1% = taker fee).
    """

    id: str = "mock"

    def __init__(
        self,
        symbol: str,
        data_dir: str | Path = "data",
        slippage_pct: float = 0.0005,
        fee_rate: float = 0.001,
    ) -> None:
        self._symbol = symbol
        self._data_dir = Path(data_dir)
        self._slippage_pct = slippage_pct
        self._fee_rate = fee_rate

        # Internal state
        self._step: int = 0  # current index into OHLCV data
        self._spot_df: pd.DataFrame = pd.DataFrame()
        self._perp_df: pd.DataFrame = pd.DataFrame()
        self._fr_df: pd.DataFrame = pd.DataFrame()
        self.trades: list[SimulatedTrade] = []
        self.markets: dict[str, dict[str, Any]] = {}

        self._load_data()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_data(self) -> None:
        """Read CSV files into DataFrames."""
        safe_name = self._symbol.replace("/", "")  # e.g. "DOGEUSDT"

        spot_path = self._data_dir / f"{safe_name}_spot_1m.csv"
        perp_path = self._data_dir / f"{safe_name}_perp_1m.csv"
        fr_path = self._data_dir / f"{safe_name}_funding.csv"

        self._spot_df = pd.read_csv(spot_path, parse_dates=["timestamp"])
        self._perp_df = pd.read_csv(perp_path, parse_dates=["timestamp"])
        self._fr_df = pd.read_csv(fr_path, parse_dates=["timestamp"])

        # Sort chronologically
        self._spot_df.sort_values("timestamp", inplace=True)
        self._perp_df.sort_values("timestamp", inplace=True)
        self._fr_df.sort_values("timestamp", inplace=True)

        self._spot_df.reset_index(drop=True, inplace=True)
        self._perp_df.reset_index(drop=True, inplace=True)
        self._fr_df.reset_index(drop=True, inplace=True)

        logger.info(
            "MockExchange loaded {} spot rows, {} perp rows, {} FR records for {}",
            len(self._spot_df),
            len(self._perp_df),
            len(self._fr_df),
            self._symbol,
        )

    # ------------------------------------------------------------------
    # ccxt-compatible interface
    # ------------------------------------------------------------------

    async def load_markets(self) -> dict[str, Any]:
        """Return minimal market info (no-op for mock)."""
        spot_sym = self._symbol            # "DOGE/USDT"
        perp_sym = f"{self._symbol}:USDT"  # "DOGE/USDT:USDT"
        base_market: dict[str, Any] = {
            "limits": {"amount": {"min": 1.0}},
            "precision": {"amount": 0},
        }
        self.markets = {
            spot_sym: {**base_market, "type": "spot"},
            perp_sym: {**base_market, "type": "swap"},
        }
        return self.markets

    async def fetch_tickers(self) -> dict[str, dict[str, Any]]:
        """Return Spot + Perp ticker at current timestep."""
        spot_row = self._current_spot()
        perp_row = self._current_perp()

        spot_sym = self._symbol
        perp_sym = f"{self._symbol}:USDT"

        return {
            spot_sym: {
                "symbol": spot_sym,
                "last": float(spot_row["close"]),
                "quoteVolume": float(spot_row.get("volume", 0))
                * float(spot_row["close"]),
            },
            perp_sym: {
                "symbol": perp_sym,
                "last": float(perp_row["close"]),
                "quoteVolume": float(perp_row.get("volume", 0))
                * float(perp_row["close"]),
            },
        }

    async def fetch_funding_rates(self) -> dict[str, dict[str, Any]]:
        """Return current funding rate keyed by perp symbol."""
        fr = self._current_funding_rate()
        perp_sym = f"{self._symbol}:USDT"
        return {perp_sym: {"symbol": perp_sym, "fundingRate": fr}}

    async def fetch_funding_rate(self, symbol: str) -> dict[str, Any]:
        """Return funding rate for a single symbol."""
        fr = self._current_funding_rate()
        return {"symbol": symbol, "fundingRate": fr}

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Simulate a market order fill with slippage and fees."""
        base_price = self._price_for_symbol(symbol)

        # Apply slippage
        if side == "buy":
            fill_price = base_price * (1 + self._slippage_pct)
        else:
            fill_price = base_price * (1 - self._slippage_pct)

        fee_cost = amount * fill_price * self._fee_rate
        trade_id = str(uuid.uuid4())[:8]
        ts = self._current_timestamp_ms()

        trade = SimulatedTrade(
            id=trade_id,
            timestamp=ts,
            symbol=symbol,
            side=side,
            qty=amount,
            price=fill_price,
            fee_cost=fee_cost,
        )
        self.trades.append(trade)

        logger.debug(
            "MOCK FILL: {} {} {} qty={:.4f} @ {:.6f} fee={:.4f}",
            trade_id,
            side,
            symbol,
            amount,
            fill_price,
            fee_cost,
        )

        return {
            "id": trade_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "filled": amount,
            "price": fill_price,
            "cost": amount * fill_price,
            "fee": {"cost": fee_cost, "currency": "USDT"},
            "timestamp": ts,
            "status": "closed",
        }

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        """Return amount as-is (no precision enforcement in mock)."""
        return str(amount)

    async def close(self) -> None:
        """No-op for mock."""
        pass

    # ------------------------------------------------------------------
    # Time-stepping
    # ------------------------------------------------------------------

    def advance_time(self, minutes: int = 1) -> bool:
        """Advance internal clock by *minutes* steps.

        Returns ``False`` when the end of data is reached.
        """
        self._step += minutes
        max_len = min(len(self._spot_df), len(self._perp_df))
        if self._step >= max_len:
            logger.info("MockExchange reached end of data at step {}", self._step)
            return False
        return True

    @property
    def current_step(self) -> int:
        return self._step

    @property
    def total_steps(self) -> int:
        return min(len(self._spot_df), len(self._perp_df))

    def current_datetime(self) -> datetime:
        """Return the datetime of the current timestep."""
        row = self._current_spot()
        ts = row["timestamp"]
        if isinstance(ts, pd.Timestamp):
            return ts.to_pydatetime().replace(tzinfo=timezone.utc)
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _current_spot(self) -> pd.Series:
        idx = min(self._step, len(self._spot_df) - 1)
        return self._spot_df.iloc[idx]

    def _current_perp(self) -> pd.Series:
        idx = min(self._step, len(self._perp_df) - 1)
        return self._perp_df.iloc[idx]

    def _current_funding_rate(self) -> float:
        """Get the most recent FR at or before current timestamp."""
        if self._fr_df.empty:
            return 0.0
        current_ts = self._current_spot()["timestamp"]
        mask = self._fr_df["timestamp"] <= current_ts
        if mask.any():
            return float(self._fr_df.loc[mask, "funding_rate"].iloc[-1])
        return float(self._fr_df["funding_rate"].iloc[0])

    def _price_for_symbol(self, symbol: str) -> float:
        """Return close price for spot or perp."""
        if ":USDT" in symbol:
            return float(self._current_perp()["close"])
        return float(self._current_spot()["close"])

    def _current_timestamp_ms(self) -> int:
        ts = self._current_spot()["timestamp"]
        if isinstance(ts, pd.Timestamp):
            return int(ts.timestamp() * 1000)
        return int(ts)
