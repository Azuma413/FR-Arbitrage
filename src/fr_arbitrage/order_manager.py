"""Execution Engine — IOC limit order placement with Dry-Run support.

Corresponds to README §3.3:
  - Spot Buy (IOC) → verify fill → Perp Short (IOC)
  - Netting rollback on partial fills
  - Dry-Run mode: simulate fills using real market data

When ``DRY_RUN=True``, ``_place_order()`` returns a synthetic fill
based on current best bid/ask + configured slippage. No real orders
are sent. All downstream logic (position tracking, DB writes,
guardian evaluation) operates identically.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, Optional

import structlog
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from fr_arbitrage.config import Settings
from fr_arbitrage.database import upsert_position
from fr_arbitrage.models import AssetMeta, MarketState, Position, TargetSymbol

logger = structlog.get_logger()


class OrderManager:
    """Executes delta-neutral entry / exit orders with leg-risk safeguards.

    In Dry-Run mode, simulates fills without sending real orders.
    """

    def __init__(
        self,
        settings: Settings,
        states: Dict[str, MarketState],
        asset_meta: Dict[str, AssetMeta],
    ) -> None:
        self._settings = settings
        self._states = states
        self._asset_meta = asset_meta
        self._exchange: Optional[Exchange] = None
        self._info: Optional[Info] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize Hyperliquid Exchange client."""
        base_url = (
            constants.MAINNET_API_URL
            if self._settings.environment.upper() == "MAINNET"
            else constants.TESTNET_API_URL
        )

        self._info = Info(base_url, skip_ws=True)

        if not self._settings.dry_run:
            # Real exchange connection
            from eth_account import Account

            wallet = Account.from_key(self._settings.private_key)
            self._exchange = Exchange(
                wallet,
                base_url,
                account_address=self._settings.account_address or None,
            )
            logger.info(
                "order_manager_started",
                mode="LIVE",
                environment=self._settings.environment,
            )
        else:
            logger.info(
                "order_manager_started",
                mode="DRY_RUN",
                environment=self._settings.environment,
            )

    async def close(self) -> None:
        """Cleanup (no persistent connections to close for Hyperliquid SDK)."""
        logger.info("order_manager_closed")

    # ------------------------------------------------------------------
    # Entry: Spot Buy + Perp Short
    # ------------------------------------------------------------------

    async def execute_entry(
        self, target: TargetSymbol, amount_usdc: float
    ) -> Optional[Position]:
        """Open a delta-neutral position (Spot Buy + Perp Short).

        Parameters
        ----------
        target:
            The scanned target coin to trade.
        amount_usdc:
            USDC amount to invest in this position.

        Returns
        -------
        Position | None
            The newly opened position, or ``None`` if execution failed.
        """
        coin = target.coin
        meta = self._asset_meta.get(coin)
        if meta is None:
            logger.error("no_asset_meta", coin=coin)
            return None

        state = self._states.get(coin)
        if state is None or state.spot_best_ask <= 0:
            logger.error("no_market_state", coin=coin)
            return None

        # Calculate quantity
        raw_qty = amount_usdc / state.spot_best_ask
        qty = round(raw_qty, meta.sz_decimals)
        if qty <= 0:
            logger.error("qty_too_small", coin=coin, raw_qty=raw_qty)
            return None

        logger.info(
            "executing_entry",
            coin=coin,
            qty=qty,
            amount_usdc=amount_usdc,
            spot_ask=state.spot_best_ask,
            perp_bid=state.best_bid,
        )

        # Step 1: Spot Buy (IOC)
        spot_result = await self._place_spot_order(
            coin, is_buy=True, sz=qty, meta=meta
        )
        if spot_result is None:
            logger.error("spot_entry_failed", coin=coin)
            return None

        spot_filled = spot_result["filled_sz"]
        spot_price = spot_result["avg_price"]

        if spot_filled <= 0:
            logger.warning("spot_zero_fill", coin=coin)
            return None

        # Step 2: Perp Short (IOC) for the filled spot quantity
        perp_result = await self._place_perp_order(
            coin, is_buy=False, sz=spot_filled, meta=meta
        )

        if perp_result is None or perp_result["filled_sz"] <= 0:
            # ROLLBACK: sell spot to flatten
            logger.error("perp_entry_failed_rollback", coin=coin)
            rollback_res = await self._place_spot_order(
                coin, is_buy=False, sz=spot_filled, meta=meta
            )
            
            # Check if rollback succeeded
            rollback_filled = 0.0
            if rollback_res:
                rollback_filled = rollback_res.get("filled_sz", 0.0)
            
            remaining_spot = spot_filled - rollback_filled
            if remaining_spot > 0:
                 logger.critical(
                     "entry_rollback_failed", 
                     coin=coin, 
                     remaining_spot=remaining_spot
                 )
                 # Persist the IMBALANCED position so Guardian can fix it
                 position = Position(
                    symbol=coin,
                    spot_sz=remaining_spot,
                    perp_sz=0.0,
                    entry_price=spot_price,
                    accumulated_funding=0.0,
                    state="OPEN", # Guardian will see delta imbalance and fix
                )
                 await upsert_position(position)
                 return position # Return the imbalanced position
            
            return None

        perp_filled = perp_result["filled_sz"]
        perp_price = perp_result["avg_price"]

        # Step 3: Netting — if perp partially filled, reduce spot to match
        if perp_filled < spot_filled:
            excess = round(spot_filled - perp_filled, meta.sz_decimals)
            if excess > 0:
                logger.warning(
                    "netting_excess_spot",
                    coin=coin,
                    excess=excess,
                )
                await self._place_spot_order(
                    coin, is_buy=False, sz=excess, meta=meta
                )
            spot_filled = perp_filled

        # Step 4: Persist position
        entry_price = (spot_price + perp_price) / 2
        position = Position(
            symbol=coin,
            spot_sz=spot_filled,
            perp_sz=perp_filled,
            entry_price=entry_price,
            accumulated_funding=0.0,
            state="OPEN",
        )
        await upsert_position(position)

        logger.info(
            "entry_complete",
            coin=coin,
            spot_sz=spot_filled,
            perp_sz=perp_filled,
            entry_price=entry_price,
            dry_run=self._settings.dry_run,
        )
        return position

    # ------------------------------------------------------------------
    # Exit: Close both legs
    # ------------------------------------------------------------------

    async def execute_exit(self, position: Position, slippage_override: Optional[float] = None) -> bool:
        """Close the delta-neutral position (Spot Sell + Perp Cover).

        Returns True if both legs closed successfully.
        """
        coin = position.symbol
        meta = self._asset_meta.get(coin)
        if meta is None:
            logger.error("no_asset_meta_for_exit", coin=coin)
            return False

        logger.info(
            "executing_exit",
            coin=coin,
            spot_sz=position.spot_sz,
            perp_sz=position.perp_sz,
        )

        # Close both legs concurrently
        spot_task = asyncio.create_task(
            self._place_spot_order(
                coin, is_buy=False, sz=position.spot_sz, meta=meta
            )
        )
        perp_task = asyncio.create_task(
            self._place_perp_order(
                coin, is_buy=True, sz=position.perp_sz, meta=meta,
                reduce_only=True,
            )
        )

        results = await asyncio.gather(spot_task, perp_task, return_exceptions=True)
        spot_result, perp_result = results

        # Calculate remaining sizes
        spot_filled = spot_result.get("filled_sz", 0) if (spot_result and not isinstance(spot_result, BaseException)) else 0
        perp_filled = perp_result.get("filled_sz", 0) if (perp_result and not isinstance(perp_result, BaseException)) else 0
        
        position.spot_sz = max(0.0, position.spot_sz - spot_filled)
        position.perp_sz = max(0.0, position.perp_sz - perp_filled)

        # Allow small dust to be considered closed (e.g. < $1 value)
        # For now, strict check: if both zero, closed.
        if position.spot_sz <= 0 and position.perp_sz <= 0:
            position.state = "CLOSED"
            await upsert_position(position)
            logger.info("exit_complete", coin=coin)
            return True

        # Partial fill or failure
        # Reset state to OPEN so Guardian can retry later
        # (Guardian will see closing_attempts incremented in its own logic)
        position.state = "OPEN"
        await upsert_position(position)
        
        logger.warning(
            "exit_partially_filled",
            coin=coin,
            spot_filled=spot_filled,
            perp_filled=perp_filled,
            remaining_spot=position.spot_sz,
            remaining_perp=position.perp_sz,
        )
        return False

    # ------------------------------------------------------------------
    # Internal order placement
    # ------------------------------------------------------------------

    async def _place_spot_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        meta: AssetMeta,
    ) -> Optional[Dict[str, Any]]:
        """Place an IOC limit order on the spot market.

        In Dry-Run mode, returns a simulated fill.
        """
        state = self._states.get(coin)
        if state is None:
            return None

        # Determine limit price with slippage
        if is_buy:
            limit_px = state.spot_best_ask * (1 + self._settings.slippage_tolerance)
        else:
            limit_px = state.spot_best_bid * (1 - self._settings.slippage_tolerance)

        limit_px = round(limit_px, meta.px_decimals)
        sz = round(sz, meta.sz_decimals)

        if self._settings.dry_run:
            return self._simulate_fill(coin, "spot", is_buy, sz, limit_px)

        return await self._send_order(
            coin=coin,
            is_buy=is_buy,
            sz=sz,
            limit_px=limit_px,
            meta=meta,
            is_spot=True,
        )

    async def _place_perp_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        meta: AssetMeta,
        reduce_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Place an IOC limit order on the perp market.

        In Dry-Run mode, returns a simulated fill.
        """
        state = self._states.get(coin)
        if state is None:
            return None

        # Determine limit price with slippage
        if is_buy:
            limit_px = state.best_ask * (1 + self._settings.slippage_tolerance)
        else:
            limit_px = state.best_bid * (1 - self._settings.slippage_tolerance)

        limit_px = round(limit_px, meta.px_decimals)
        sz = round(sz, meta.sz_decimals)

        if self._settings.dry_run:
            return self._simulate_fill(coin, "perp", is_buy, sz, limit_px)

        return await self._send_order(
            coin=coin,
            is_buy=is_buy,
            sz=sz,
            limit_px=limit_px,
            meta=meta,
            is_spot=False,
            reduce_only=reduce_only,
        )

    async def _send_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        meta: AssetMeta,
        is_spot: bool = False,
        reduce_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Send a real IOC order via the Hyperliquid SDK."""
        if self._exchange is None:
            logger.error("exchange_not_initialized")
            return None

        order_type = {"limit": {"tif": "Ioc"}}
        max_retries = self._settings.max_retry_attempts

        for attempt in range(1, max_retries + 1):
            try:
                if is_spot:
                    spot_coin = self._spot_coin_name(coin)
                    if spot_coin is None:
                        logger.error("no_spot_coin_name", coin=coin)
                        return None
                    result = self._exchange.order(
                        spot_coin, is_buy, sz, limit_px, order_type,
                        reduce_only=reduce_only,
                    )
                else:
                    result = self._exchange.order(
                        coin, is_buy, sz, limit_px, order_type,
                        reduce_only=reduce_only,
                    )

                # Parse SDK response
                status = result.get("status", "")
                response = result.get("response", {})

                if status == "ok":
                    # Extract fill info from statuses
                    statuses = response.get("data", {}).get("statuses", [])
                    filled_sz = 0.0
                    avg_price = limit_px

                    for s in statuses:
                        if isinstance(s, dict):
                            if "filled" in s:
                                filled_info = s["filled"]
                                filled_sz += float(filled_info.get("totalSz", 0))
                                avg_price = float(filled_info.get("avgPx", limit_px))
                            elif "resting" in s:
                                # IOC shouldn't rest, but handle gracefully
                                logger.warning("ioc_order_resting", coin=coin)

                    logger.info(
                        "order_filled",
                        coin=coin,
                        side="buy" if is_buy else "sell",
                        market="spot" if is_spot else "perp",
                        filled_sz=filled_sz,
                        avg_price=avg_price,
                    )
                    return {"filled_sz": filled_sz, "avg_price": avg_price}
                else:
                    logger.warning(
                        "order_rejected",
                        coin=coin,
                        status=status,
                        response=response,
                        attempt=attempt,
                    )

            except Exception as exc:
                wait = 2 ** attempt
                logger.warning(
                    "order_error",
                    coin=coin,
                    error=str(exc),
                    attempt=attempt,
                    retry_in=wait,
                )
                await asyncio.sleep(wait)

        logger.error("order_max_retries_exceeded", coin=coin)
        return None

    # ------------------------------------------------------------------
    # Dry-Run simulation
    # ------------------------------------------------------------------

    def _simulate_fill(
        self,
        coin: str,
        market: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
    ) -> Dict[str, Any]:
        """Generate a synthetic fill for Dry-Run mode."""
        state = self._states.get(coin)
        
        # Use market price if available, else limit
        market_px = limit_px
        if state:
            if market == "spot":
                # For buy, use Ask; for sell, use Bid
                market_px = state.spot_best_ask if is_buy else state.spot_best_bid
            else:
                market_px = state.best_ask if is_buy else state.best_bid
        
        if market_px <= 0:
            market_px = limit_px

        # Simulating realistic slippage (0.1%) + fee (0.025%)
        # Buy: price increases. Sell: price decreases.
        impact = 0.001
        
        if is_buy:
             fill_price = market_px * (1 + impact)
        else:
             fill_price = market_px * (1 - impact)

        notional = sz * fill_price
        fee = notional * 0.00025

        logger.info(
            "dry_run_simulated_fill",
            coin=coin,
            market=market,
            side="buy" if is_buy else "sell",
            sz=sz,
            market_px=round(market_px, 6),
            fill_price=round(fill_price, 6),
            limit_px=round(limit_px, 6),
            notional=round(notional, 2),
            fee=round(fee, 4),
            order_id=str(uuid.uuid4())[:8],
        )

        return {"filled_sz": sz, "avg_price": fill_price}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _spot_coin_name(self, base_coin: str) -> Optional[str]:
        """Get the spot coin name for order submission."""
        meta = self._asset_meta.get(base_coin)
        if meta is None or meta.spot_asset_id is None:
            return None
        spot_index = meta.spot_asset_id - 10000
        if base_coin == "PURR":
            return "PURR/USDC"
        return f"@{spot_index}"
