"""Position Guardian — Monitors existing positions for exit and rebalance.

Corresponds to README §3.4:
  - Exit: FR negative for N consecutive checks, or spread backwardation
  - Auto-deleverage: reduce position when margin usage > threshold
  - Runs as a periodic asyncio task
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

import structlog
from hyperliquid.info import Info
from hyperliquid.utils import constants

from fr_arbitrage.config import Settings
from fr_arbitrage.database import get_open_positions, upsert_position
from fr_arbitrage.models import AssetMeta, MarketState, Position
from fr_arbitrage.order_manager import OrderManager

logger = structlog.get_logger()


class PositionGuardian:
    """Monitors and maintains open positions — exit logic + auto-deleverage."""

    def __init__(
        self,
        settings: Settings,
        states: Dict[str, MarketState],
        asset_meta: Dict[str, AssetMeta],
        order_manager: OrderManager,
    ) -> None:
        self._settings = settings
        self._states = states
        self._asset_meta = asset_meta
        self._order_mgr = order_manager
        self._info: Optional[Info] = None

        # Track consecutive negative FR counts per coin
        self._negative_fr_counts: Dict[str, int] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize Info client for account state queries."""
        base_url = (
            constants.MAINNET_API_URL
            if self._settings.environment.upper() == "MAINNET"
            else constants.TESTNET_API_URL
        )
        self._info = Info(base_url, skip_ws=True)
        self._running = True
        logger.info("position_guardian_started")

    async def stop(self) -> None:
        self._running = False
        logger.info("position_guardian_stopped")

    # ------------------------------------------------------------------
    # Main loop (run as asyncio task)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Periodically check all open positions for exit/rebalance conditions."""
        while self._running:
            try:
                await self._check_all_positions()
            except Exception as exc:
                logger.error("guardian_check_error", error=str(exc))

            await asyncio.sleep(self._settings.guardian_interval_sec)

    async def _check_all_positions(self) -> None:
        """Evaluate all open positions."""
        positions = await get_open_positions()
        if not positions:
            return

        for position in positions:
            if position.state not in ("OPEN", "REBALANCING"):
                continue

            coin = position.symbol
            state = self._states.get(coin)
            if state is None:
                continue

            # --- Exit condition 1: Negative FR for N consecutive checks ---
            if state.funding_rate < 0:
                count = self._negative_fr_counts.get(coin, 0) + 1
                self._negative_fr_counts[coin] = count
                logger.warning(
                    "negative_fr_detected",
                    coin=coin,
                    funding_rate=f"{state.funding_rate:.6%}",
                    consecutive_count=count,
                )
                if count >= self._settings.exit_negative_fr_count:
                    logger.warning(
                        "exit_trigger_negative_fr",
                        coin=coin,
                        consecutive=count,
                    )
                    await self._close_position(position)
                    continue
            else:
                # Reset counter when FR is positive
                self._negative_fr_counts[coin] = 0

            # --- Exit condition 2: Spread backwardation (profit-take) ---
            if state.perp_spot_spread < 0 and abs(state.perp_spot_spread) > 0.005:
                logger.info(
                    "exit_trigger_backwardation",
                    coin=coin,
                    spread=f"{state.perp_spot_spread:.4%}",
                )
                await self._close_position(position)
                continue

            # --- Auto-deleverage: Check margin usage ---
            await self._check_margin_and_rebalance(position)

            # --- Funding Income Tracking ---
            if state.funding_rate > 0:
                # Estimate funding income per check interval
                hours_per_check = self._settings.guardian_interval_sec / 3600
                notional = position.perp_sz * state.mid_price
                funding_income = state.funding_rate * notional * hours_per_check
                position.accumulated_funding += funding_income
                await upsert_position(position)

    async def check_position_now(self, coin: str) -> None:
        """Manually trigger a check for a specific coin (used after entry failure)."""
        position = await self._order_mgr._states.get(coin) # Just logging for now
        logger.info("guardian_manual_trigger", coin=coin)
        # Real logic: fetch position from DB and check it
        try:
             # Re-use logic by calling singular check if implemented, or just wait for next loop.
             # For now, we rely on next loop, but we can log that we are aware.
             pass
        except Exception as exc:
             logger.error("manual_check_error", error=str(exc))

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    # Track stuck positions: coin -> attempts
    _closing_attempts: Dict[str, int] = {}

    async def _close_position(self, position: Position) -> None:
        """Close both legs of a position."""
        coin = position.symbol
        
        # Track attempts
        attempts = self._closing_attempts.get(coin, 0) + 1
        self._closing_attempts[coin] = attempts
        
        if attempts > 10:
            logger.critical(
                "STUCK_POSITION_ALERT",
                coin=coin,
                attempts=attempts,
                hint="Manual intervention required or increase slippage/retry limits.",
            )
            # Optionally: stop retrying to avoid spamming API?
            # return 

        position.state = "CLOSING_PENDING"
        await upsert_position(position)

        # Allow higher slippage if stuck? Could pass attempts to execute_exit
        success = await self._order_mgr.execute_exit(position)
        
        if success:
            self._closing_attempts.pop(coin, None)
        else:
            logger.error(
                "position_close_failed",
                coin=position.symbol,
                attempt=attempts
            )

    # ------------------------------------------------------------------
    # Auto-deleverage / Rebalance
    # ------------------------------------------------------------------

    async def _check_margin_and_rebalance(self, position: Position) -> None:
        """Check margin usage and reduce position if necessary."""
        if self._info is None or self._settings.dry_run:
            # In dry-run mode, skip margin queries (no real account state)
            return

        try:
            user_state = self._info.user_state(self._settings.account_address)
            margin_summary = user_state.get("marginSummary", {})
            account_value = float(margin_summary.get("accountValue", 0))
            total_margin_used = float(margin_summary.get("totalMarginUsed", 0))

            if account_value <= 0:
                return

            margin_usage = total_margin_used / account_value

            if margin_usage > self._settings.margin_usage_threshold:
                logger.warning(
                    "margin_usage_high",
                    coin=position.symbol,
                    margin_usage=f"{margin_usage:.1%}",
                    threshold=f"{self._settings.margin_usage_threshold:.1%}",
                )

                # Calculate reduction size
                state = self._states.get(position.symbol)
                if state is None or state.mid_price <= 0:
                    return

                target_margin = self._settings.margin_usage_threshold * 0.8
                excess_margin = total_margin_used - (target_margin * account_value)
                reduce_sz = excess_margin / state.mid_price

                meta = self._asset_meta.get(position.symbol)
                if meta is None:
                    return

                reduce_sz = round(reduce_sz, meta.sz_decimals)
                reduce_sz = min(reduce_sz, position.perp_sz * 0.5)  # Max 50% reduction

                if reduce_sz > 0:
                    position.state = "REBALANCING"
                    await upsert_position(position)

                    logger.info(
                        "auto_deleverage",
                        coin=position.symbol,
                        reduce_sz=reduce_sz,
                    )

                    # Reduce: sell spot + cover perp
                    await self._order_mgr._place_spot_order(
                        position.symbol, is_buy=False, sz=reduce_sz, meta=meta
                    )
                    await self._order_mgr._place_perp_order(
                        position.symbol, is_buy=True, sz=reduce_sz, meta=meta,
                        reduce_only=True,
                    )

                    position.spot_sz -= reduce_sz
                    position.perp_sz -= reduce_sz
                    position.state = "OPEN"
                    await upsert_position(position)

        except Exception as exc:
            logger.warning("margin_check_error", error=str(exc))
