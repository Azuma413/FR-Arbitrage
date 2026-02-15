"""MarketScanner — Scans exchange for high-FR arbitrage opportunities.

Implements README §4.1 (Entry Criteria):
  - Quote currency: USDT
  - Funding Rate  ≥ threshold (default 0.03% / 8h)
  - 24h Volume    ≥ threshold (default 10M USDT)
  - Spread (Perp − Spot) / Spot ≥ threshold (default 0.2%) and positive
"""

from __future__ import annotations

import asyncio
from typing import Any

import ccxt.async_support as ccxt
import pandas as pd
from loguru import logger

from fr_arbitrage.config import Settings
from fr_arbitrage.models import TargetSymbol


class MarketScanner:
    """Fetches market data and filters for profitable FR-arbitrage targets."""

    def __init__(
        self, settings: Settings, *, exchange: Any | None = None
    ) -> None:
        self._settings = settings
        self._exchange = exchange
        self._owns_exchange = exchange is None  # True = we created it

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the exchange connection (skipped if injected via DI)."""
        if self._exchange is not None:
            logger.info("MarketScanner using injected exchange")
            return

        exchange_cls = getattr(ccxt, self._settings.exchange_name)
        self._exchange = exchange_cls(
            {
                "apiKey": self._settings.api_key,
                "secret": self._settings.api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},  # perpetual futures
            }
        )
        await self._exchange.load_markets()
        logger.info(
            "MarketScanner connected to {} — {} markets loaded",
            self._settings.exchange_name,
            len(self._exchange.markets),
        )

    async def close(self) -> None:
        """Close the exchange connection gracefully (skipped if injected)."""
        if self._exchange is not None and self._owns_exchange:
            await self._exchange.close()
            logger.info("MarketScanner exchange connection closed")

    # ------------------------------------------------------------------
    # Core scan logic
    # ------------------------------------------------------------------

    async def scan(self) -> list[TargetSymbol]:
        """Scan all USDT-perp markets and return filtered targets sorted by FR descending.

        Returns
        -------
        list[TargetSymbol]
            Markets passing all filter criteria, sorted by ``funding_rate`` descending.
        """
        assert self._exchange is not None, "Call start() before scan()"

        # 1. Collect funding rates for all linear USDT perpetuals
        funding_rates = await self._fetch_funding_rates()
        if not funding_rates:
            logger.warning("No funding rate data retrieved")
            return []

        # 2. Fetch tickers for spot + perp prices and volume
        tickers = await self._exchange.fetch_tickers()

        # 3. Build rows
        rows: list[dict[str, Any]] = []
        for symbol, fr in funding_rates.items():
            spot_symbol = self._to_spot_symbol(symbol)
            perp_ticker = tickers.get(symbol)
            spot_ticker = tickers.get(spot_symbol)
            if perp_ticker is None or spot_ticker is None:
                continue

            spot_price = spot_ticker.get("last")
            perp_price = perp_ticker.get("last")
            volume_24h = perp_ticker.get("quoteVolume")  # USDT volume

            if not all([spot_price, perp_price, volume_24h]):
                continue

            spread_pct = (perp_price - spot_price) / spot_price

            rows.append(
                {
                    "symbol": symbol,
                    "funding_rate": fr,
                    "spot_price": spot_price,
                    "perp_price": perp_price,
                    "spread_pct": spread_pct,
                    "volume_24h": volume_24h,
                }
            )

        if not rows:
            logger.info("No pairs with valid price data")
            return []

        # 4. Filter using config thresholds
        df = pd.DataFrame(rows)
        filtered = df[
            (df["funding_rate"] >= self._settings.min_funding_rate)
            & (df["volume_24h"] >= self._settings.min_volume_24h)
            & (df["spread_pct"] >= self._settings.min_spread_pct)
        ]
        filtered = filtered.sort_values("funding_rate", ascending=False)

        targets = [TargetSymbol(**row) for row in filtered.to_dict(orient="records")]

        logger.info(
            "Scan complete: {}/{} pairs passed filters",
            len(targets),
            len(rows),
        )
        for t in targets[:5]:
            logger.debug(
                "  {} FR={:.4%} Spread={:.4%} Vol={:,.0f}",
                t.symbol,
                t.funding_rate,
                t.spread_pct,
                t.volume_24h,
            )

        return targets

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _fetch_funding_rates(self) -> dict[str, float]:
        """Retrieve the latest predicted funding rate for every USDT-perp market.

        Returns a ``{symbol: rate}`` mapping.
        """
        assert self._exchange is not None

        # ccxt unified: fetch_funding_rates() returns {symbol: {info, rate, ...}}
        raw: dict[str, Any] = await self._exchange.fetch_funding_rates()
        rates: dict[str, float] = {}
        for symbol, data in raw.items():
            if not symbol.endswith("/USDT:USDT"):
                continue
            rate = data.get("fundingRate")
            if rate is not None:
                rates[symbol] = float(rate)
        return rates

    @staticmethod
    def _to_spot_symbol(perp_symbol: str) -> str:
        """Convert a perpetual symbol like ``BTC/USDT:USDT`` to spot ``BTC/USDT``."""
        return perp_symbol.replace(":USDT", "")
