"""Opportunity Scanner — Filters market data for profitable entry targets.

Corresponds to README §3.2:
  - Reads from MarketState dict (no direct API calls)
  - Filters by funding rate, open interest, and spread
  - Returns list[TargetSymbol] for coins not already held
"""

from __future__ import annotations

from typing import Dict, Set

import structlog

from fr_arbitrage.config import Settings
from fr_arbitrage.models import MarketState, TargetSymbol

logger = structlog.get_logger()


class OpportunityScanner:
    """Scans MarketState for coins meeting entry criteria."""

    def __init__(
        self,
        settings: Settings,
        states: Dict[str, MarketState],
    ) -> None:
        self._settings = settings
        self._states = states

    def scan(self, held_symbols: Set[str]) -> list[TargetSymbol]:
        """Evaluate all tracked coins and return those passing filters.

        Parameters
        ----------
        held_symbols:
            Set of coin names already held (skip these).

        Returns
        -------
        list[TargetSymbol]
            Coins passing all criteria, sorted by funding_rate descending.
        """
        targets: list[TargetSymbol] = []

        for coin, state in self._states.items():
            # Skip already held
            if coin in held_symbols:
                continue

            # Skip blacklisted
            if coin in self._settings.blacklist_coins:
                continue

            # Skip if no price data yet
            if state.best_bid <= 0 or state.spot_best_ask <= 0:
                continue

            # --- Filter 1: Funding Rate (must be positive) ---
            if state.funding_rate < self._settings.min_funding_rate_hourly:
                continue

            # --- Filter 2: Open Interest > threshold ---
            if state.open_interest < self._settings.min_daily_volume:
                continue

            # --- Filter 3: Spread check ---
            # (Spot Ask - Perp Bid) / Spot Ask should be less than
            # estimated 24h funding income
            spread = state.perp_spot_spread
            estimated_24h_funding = state.funding_rate * 24  # hourly → daily
            if spread >= estimated_24h_funding:
                logger.debug(
                    "spread_too_wide",
                    coin=coin,
                    spread=f"{spread:.4%}",
                    funding_24h=f"{estimated_24h_funding:.4%}",
                )
                continue

            # Also check max spread limit
            if spread > self._settings.max_entry_spread:
                continue

            targets.append(
                TargetSymbol(
                    coin=coin,
                    funding_rate=state.funding_rate,
                    spot_ask=state.spot_best_ask,
                    perp_bid=state.best_bid,
                    spread=spread,
                    open_interest=state.open_interest,
                )
            )

        # Sort by funding rate descending (best opportunities first)
        targets.sort(key=lambda t: t.funding_rate, reverse=True)

        if targets:
            logger.info(
                "scan_complete",
                total_coins=len(self._states),
                targets_found=len(targets),
                top_coin=targets[0].coin if targets else None,
                top_fr=f"{targets[0].funding_rate:.6%}" if targets else None,
            )
        else:
            logger.debug("scan_complete", targets_found=0)

        return targets
