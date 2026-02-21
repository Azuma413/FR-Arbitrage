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
import wandb

from fr_arbitrage.config import Settings
from fr_arbitrage.database import get_open_positions, upsert_position
from fr_arbitrage.models import AssetMeta, MarketState, Position
from fr_arbitrage.order_manager import OrderManager
from fr_arbitrage.virtual_wallet import VirtualWallet

logger = structlog.get_logger()


class PositionGuardian:
    """Monitors and maintains open positions — exit logic + auto-deleverage."""

    def __init__(
        self,
        settings: Settings,
        states: Dict[str, MarketState],
        asset_meta: Dict[str, AssetMeta],
        order_manager: OrderManager,
        virtual_wallet: Optional[VirtualWallet] = None,
    ) -> None:
        self._settings = settings
        self._states = states
        self._asset_meta = asset_meta
        self._order_mgr = order_manager
        self._virtual_wallet = virtual_wallet
        self._info: Optional[Info] = None

        self._stop_event = asyncio.Event()
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
        self._stop_event.clear()
        logger.info("position_guardian_started")

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        logger.info("position_guardian_stopped")

    # ------------------------------------------------------------------
    # Main loop (run as asyncio task)
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Periodically check all open positions for exit/rebalance conditions."""
        while not self._stop_event.is_set():
            try:
                await self._check_all_positions()
            except Exception as exc:
                logger.error("guardian_check_error", error=str(exc))

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._settings.guardian_interval_sec)
                break
            except asyncio.TimeoutError:
                pass

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

            # --- Exit condition 1: Negative FR Moving Average ---
            if state.ma_funding_rate < 0:
                logger.warning(
                    "negative_fr_ma_detected",
                    coin=coin,
                    current_fr=f"{state.funding_rate:.6%}",
                    ma_fr=f"{state.ma_funding_rate:.6%}",
                    history_points=len(state.funding_rate_history),
                )
                logger.warning(
                    "exit_trigger_negative_fr_ma",
                    coin=coin,
                    ma_fr=f"{state.ma_funding_rate:.6%}",
                )

                if self._settings.wandb_enabled:
                    wandb.log({
                        "guardian/trigger_exit_negative_fr_ma": 1, 
                        "guardian/symbol": coin,
                        "guardian/ma_fr": state.ma_funding_rate
                    })
                await self._close_position(position)
                continue

            # --- Exit condition 2: Spread backwardation (profit-take) ---
            if state.perp_spot_spread > 0 and state.perp_spot_spread > 0.005:
                logger.info(
                    "exit_trigger_backwardation",
                    coin=coin,
                    spread=f"{state.perp_spot_spread:.4%}",

                )
                if self._settings.wandb_enabled:
                    wandb.log({
                        "guardian/trigger_exit_backwardation": 1,
                        "guardian/symbol": coin,
                        "guardian/spread": state.perp_spot_spread
                    })
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
        
        account_value = 0.0
        total_margin_used = 0.0

        if self._settings.dry_run:
            if self._virtual_wallet:
                account_value = self._virtual_wallet.get_account_value(self._states)
                total_margin_used = self._virtual_wallet.get_total_margin_used(self._states)
            else:
                return
        elif self._info:
             try:
                user_state = self._info.user_state(self._settings.account_address)
                margin_summary = user_state.get("marginSummary", {})
                account_value = float(margin_summary.get("accountValue", 0))
                total_margin_used = float(margin_summary.get("totalMarginUsed", 0))
             except Exception as exc:
                logger.warning("margin_check_error_live", error=str(exc))
                return
        else:
            return

        try:
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
