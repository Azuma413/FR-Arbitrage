"""OrderManager — Concurrent Taker execution with leg-risk protection.

Implements README §4.2 (Execution Logic):
  - Spot Buy + Perp Short via asyncio.gather (Market orders)
  - Automatic rollback on one-sided fill
  - Position exit (close both legs)
"""

from __future__ import annotations

import asyncio
from typing import Any

import ccxt.async_support as ccxt
from loguru import logger

from fr_arbitrage.config import Settings
from fr_arbitrage.models import ActivePosition, TargetSymbol


class OrderManager:
    """Executes delta-neutral entry / exit orders with leg-risk safeguards."""

    def __init__(
        self,
        settings: Settings,
        *,
        spot_exchange: Any | None = None,
        perp_exchange: Any | None = None,
    ) -> None:
        self._settings = settings
        self._spot_exchange = spot_exchange
        self._perp_exchange = perp_exchange
        self._owns_exchange = spot_exchange is None  # True = we created them

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise exchange connections (skipped if injected via DI)."""
        if self._spot_exchange is not None and self._perp_exchange is not None:
            logger.info("OrderManager using injected exchanges")
            return

        exchange_cls = getattr(ccxt, self._settings.exchange_name)

        common_params: dict[str, Any] = {
            "apiKey": self._settings.api_key,
            "secret": self._settings.api_secret,
            "enableRateLimit": True,
        }

        # Spot connection
        self._spot_exchange = exchange_cls(
            {**common_params, "options": {"defaultType": "spot"}}
        )
        await self._spot_exchange.load_markets()

        # Perp (swap) connection
        self._perp_exchange = exchange_cls(
            {**common_params, "options": {"defaultType": "swap"}}
        )
        await self._perp_exchange.load_markets()

        logger.info("OrderManager ready (spot + perp connections established)")

    async def close(self) -> None:
        """Shutdown exchange connections (skipped if injected)."""
        if self._owns_exchange:
            for ex in (self._spot_exchange, self._perp_exchange):
                if ex is not None:
                    await ex.close()
        logger.info("OrderManager connections closed")

    # ------------------------------------------------------------------
    # Entry: Spot Buy + Perp Short (concurrent taker)
    # ------------------------------------------------------------------

    async def execute_entry(
        self, target: TargetSymbol, amount_usdt: float
    ) -> ActivePosition | None:
        """Open a delta-neutral position (Spot Buy + Perp Short).

        Parameters
        ----------
        target:
            The scanned target symbol to trade.
        amount_usdt:
            USDT amount to invest in this position.

        Returns
        -------
        ActivePosition | None
            The newly opened position, or ``None`` if execution failed.
        """
        assert self._spot_exchange is not None
        assert self._perp_exchange is not None

        spot_symbol = target.symbol.replace(":USDT", "")  # e.g. "DOGE/USDT"
        perp_symbol = target.symbol                        # e.g. "DOGE/USDT:USDT"

        # 1. Determine quantity from market info
        qty = self._calculate_quantity(
            amount_usdt, target.spot_price, spot_symbol
        )
        if qty is None or qty <= 0:
            logger.error("Could not calculate valid quantity for {}", spot_symbol)
            return None

        logger.info(
            "Executing entry: {} | qty={} | ~{:.2f} USDT",
            spot_symbol,
            qty,
            amount_usdt,
        )

        # 2. Fire both legs concurrently
        spot_task = asyncio.create_task(
            self._place_order(self._spot_exchange, spot_symbol, "buy", qty),
            name=f"spot_buy_{spot_symbol}",
        )
        perp_task = asyncio.create_task(
            self._place_order(self._perp_exchange, perp_symbol, "sell", qty),
            name=f"perp_sell_{perp_symbol}",
        )

        results = await asyncio.gather(spot_task, perp_task, return_exceptions=True)
        spot_result, perp_result = results

        # 3. Handle leg-risk scenarios
        spot_ok = not isinstance(spot_result, BaseException) and spot_result is not None
        perp_ok = not isinstance(perp_result, BaseException) and perp_result is not None

        if spot_ok and perp_ok:
            # Both legs filled — success
            spot_filled = float(spot_result.get("filled", qty))
            perp_filled = float(perp_result.get("filled", qty))
            total_fees = self._sum_fees(spot_result) + self._sum_fees(perp_result)

            position = ActivePosition(
                symbol=spot_symbol,
                spot_qty=spot_filled,
                perp_qty=perp_filled,
                entry_spread=target.spread_pct,
                total_fees=total_fees,
            )
            logger.success(
                "Entry complete: {} spot={} perp={} fees={:.4f}",
                spot_symbol,
                spot_filled,
                perp_filled,
                total_fees,
            )
            return position

        # --- One-sided fill: ROLLBACK ---
        if spot_ok and not perp_ok:
            logger.error(
                "LEG RISK — Perp order failed for {}. Rolling back spot buy.",
                spot_symbol,
            )
            if isinstance(perp_result, BaseException):
                logger.error("Perp error: {}", perp_result)
            await self._rollback(self._spot_exchange, spot_symbol, "sell", qty)

        elif perp_ok and not spot_ok:
            logger.error(
                "LEG RISK — Spot order failed for {}. Rolling back perp short.",
                perp_symbol,
            )
            if isinstance(spot_result, BaseException):
                logger.error("Spot error: {}", spot_result)
            await self._rollback(self._perp_exchange, perp_symbol, "buy", qty)

        else:
            logger.error("Both legs failed for {} — no rollback needed", spot_symbol)
            for r in results:
                if isinstance(r, BaseException):
                    logger.error("  error: {}", r)

        return None

    # ------------------------------------------------------------------
    # Exit: Close both legs
    # ------------------------------------------------------------------

    async def execute_exit(self, position: ActivePosition) -> bool:
        """Close the delta-neutral position (Spot Sell + Perp Cover).

        Parameters
        ----------
        position:
            The active position to close.

        Returns
        -------
        bool
            ``True`` if both legs closed successfully.
        """
        assert self._spot_exchange is not None
        assert self._perp_exchange is not None

        spot_symbol = position.symbol
        perp_symbol = f"{position.symbol}:USDT"

        logger.info("Executing exit: {}", spot_symbol)

        spot_task = asyncio.create_task(
            self._place_order(
                self._spot_exchange, spot_symbol, "sell", position.spot_qty
            )
        )
        perp_task = asyncio.create_task(
            self._place_order(
                self._perp_exchange,
                perp_symbol,
                "buy",
                position.perp_qty,
                params={"reduceOnly": True},
            )
        )

        results = await asyncio.gather(spot_task, perp_task, return_exceptions=True)
        spot_result, perp_result = results

        spot_ok = not isinstance(spot_result, BaseException) and spot_result is not None
        perp_ok = not isinstance(perp_result, BaseException) and perp_result is not None

        if spot_ok and perp_ok:
            logger.success("Exit complete: {}", spot_symbol)
            position.status = "CLOSED"
            return True

        logger.error(
            "Exit partially failed for {} — manual intervention may be required",
            spot_symbol,
        )
        for r in results:
            if isinstance(r, BaseException):
                logger.error("  error: {}", r)
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _calculate_quantity(
        self,
        amount_usdt: float,
        price: float,
        spot_symbol: str,
    ) -> float | None:
        """Calculate order quantity respecting min_amount and step_size.

        Uses the spot exchange market info to align the quantity.
        """
        assert self._spot_exchange is not None

        market = self._spot_exchange.markets.get(spot_symbol)
        if market is None:
            logger.error("Market info not found for {}", spot_symbol)
            return None

        raw_qty = amount_usdt / price

        # Apply step-size precision via ccxt's amount_to_precision
        try:
            qty = float(
                self._spot_exchange.amount_to_precision(spot_symbol, raw_qty)
            )
        except Exception:
            logger.warning(
                "Precision adjustment failed for {}; using raw qty", spot_symbol
            )
            qty = raw_qty

        # Check minimum trade amount
        min_amount = market.get("limits", {}).get("amount", {}).get("min")
        if min_amount is not None and qty < float(min_amount):
            logger.warning(
                "{}: qty {} below minimum {}",
                spot_symbol,
                qty,
                min_amount,
            )
            return None

        return qty

    async def _place_order(
        self,
        exchange: ccxt.Exchange,
        symbol: str,
        side: str,
        qty: float,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place a market order via the exchange.

        Wraps ``exchange.create_market_order`` with retry on transient errors.
        """
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                order = await exchange.create_market_order(
                    symbol, side, qty, params=params or {}
                )
                logger.debug(
                    "Order filled: {} {} {} qty={} id={}",
                    exchange.id,
                    side,
                    symbol,
                    qty,
                    order.get("id"),
                )
                return order  # type: ignore[return-value]
            except (ccxt.RateLimitExceeded, ccxt.NetworkError) as exc:
                wait = 2**attempt
                logger.warning(
                    "Transient error on {} {} {} (attempt {}/{}): {} — retrying in {}s",
                    side,
                    symbol,
                    exchange.id,
                    attempt,
                    max_retries,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
        raise RuntimeError(
            f"Failed to execute {side} {symbol} after {max_retries} retries"
        )

    async def _rollback(
        self,
        exchange: ccxt.Exchange,
        symbol: str,
        side: str,
        qty: float,
    ) -> None:
        """Emergency rollback: execute opposite trade to flatten exposure."""
        logger.warning("ROLLBACK: {} {} {} qty={}", side, symbol, exchange.id, qty)
        try:
            await exchange.create_market_order(symbol, side, qty)
            logger.info("Rollback successful")
        except Exception as exc:
            logger.critical(
                "ROLLBACK FAILED for {} {} — MANUAL INTERVENTION REQUIRED: {}",
                side,
                symbol,
                exc,
            )

    @staticmethod
    def _sum_fees(order: dict[str, Any]) -> float:
        """Extract total fee cost from a ccxt order response."""
        fee = order.get("fee")
        if fee and fee.get("cost") is not None:
            return float(fee["cost"])
        # Some exchanges return fees as a list
        fees = order.get("fees", [])
        return sum(float(f.get("cost", 0)) for f in fees if f)
