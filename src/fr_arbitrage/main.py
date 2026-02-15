"""Main entry point — async event loop for the FR-Arbitrage Bot.

Ties together MarketScanner and OrderManager in a perpetual scan → trade loop.
Implements the global Kill Switch (EMERGENCY_STOP) from README §6.
"""

from __future__ import annotations

import asyncio
import sys

from loguru import logger

from fr_arbitrage.config import Settings
from fr_arbitrage.market_scanner import MarketScanner
from fr_arbitrage.models import ActivePosition
from fr_arbitrage.order_manager import OrderManager


# ---------------------------------------------------------------------------
# In-memory position store (replace with SQLite in future phases)
# ---------------------------------------------------------------------------
_open_positions: list[ActivePosition] = []


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    """Core async loop: scan → entry → monitor → exit."""
    settings = Settings()

    # --- Logging setup -----------------------------------------------------
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
    )
    logger.add("logs/fr_arbitrage_{time:YYYY-MM-DD}.log", rotation="1 day", retention="7 days")

    logger.info("=== FR-Arbitrage Bot starting ===")
    logger.info("Exchange: {}", settings.exchange_name)
    logger.info("Investment per position: {} USDT", settings.investment_amount_usdt)
    logger.info("Max open positions: {}", settings.max_open_positions)

    # --- Kill-switch check -------------------------------------------------
    if settings.emergency_stop:
        logger.critical("EMERGENCY_STOP is ON — aborting startup")
        return

    # --- Initialise components ---------------------------------------------
    scanner = MarketScanner(settings)
    order_mgr = OrderManager(settings)

    try:
        await scanner.start()
        await order_mgr.start()

        while True:
            # Re-read settings for hot-reload of EMERGENCY_STOP
            live_settings = Settings()
            if live_settings.emergency_stop:
                logger.critical("EMERGENCY_STOP detected — closing all positions")
                await _emergency_close_all(order_mgr)
                break

            # 1. Scan for targets
            targets = await scanner.scan()

            # 2. Entry logic
            open_count = sum(1 for p in _open_positions if p.status == "OPEN")
            already_held = {p.symbol for p in _open_positions if p.status == "OPEN"}

            for target in targets:
                if open_count >= settings.max_open_positions:
                    logger.info("Max positions ({}) reached — skipping new entries", settings.max_open_positions)
                    break

                spot_symbol = target.symbol.replace(":USDT", "")
                if spot_symbol in already_held:
                    logger.debug("Already holding {} — skip", spot_symbol)
                    continue

                position = await order_mgr.execute_entry(
                    target, settings.investment_amount_usdt
                )
                if position is not None:
                    _open_positions.append(position)
                    open_count += 1

            # 3. Check exit conditions for open positions
            await _check_exit_conditions(scanner, order_mgr, settings)

            # 4. Wait for next scan cycle
            logger.info(
                "Sleeping {}s until next scan (open positions: {})",
                settings.scan_interval_sec,
                open_count,
            )
            await asyncio.sleep(settings.scan_interval_sec)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received — shutting down")
    except Exception as exc:
        logger.exception("Unhandled exception in main loop: {}", exc)
    finally:
        await scanner.close()
        await order_mgr.close()
        logger.info("=== FR-Arbitrage Bot stopped ===")


# ---------------------------------------------------------------------------
# Exit condition checks
# ---------------------------------------------------------------------------

async def _check_exit_conditions(
    scanner: MarketScanner,
    order_mgr: OrderManager,
    settings: Settings,
) -> None:
    """Evaluate open positions against exit criteria (README §4.3)."""
    for position in _open_positions:
        if position.status != "OPEN":
            continue

        # Fetch latest funding rate for this symbol
        perp_symbol = f"{position.symbol}:USDT"
        try:
            assert scanner._exchange is not None
            fr_data = await scanner._exchange.fetch_funding_rate(perp_symbol)
            current_fr = fr_data.get("fundingRate", 0.0)
        except Exception as exc:
            logger.warning("Failed to fetch FR for {}: {}", perp_symbol, exc)
            continue

        # Exit: FR dropped below threshold or went negative
        if current_fr <= settings.exit_funding_rate:
            logger.warning(
                "FR for {} dropped to {:.6%} (threshold {:.6%}) — triggering exit",
                position.symbol,
                current_fr,
                settings.exit_funding_rate,
            )
            position.status = "CLOSING"
            await order_mgr.execute_exit(position)


async def _emergency_close_all(order_mgr: OrderManager) -> None:
    """Close every open position immediately (Kill Switch)."""
    for position in _open_positions:
        if position.status == "OPEN":
            logger.warning("Emergency closing: {}", position.symbol)
            position.status = "CLOSING"
            await order_mgr.execute_exit(position)


# ---------------------------------------------------------------------------
# Sync entry point (called by console script)
# ---------------------------------------------------------------------------

def main() -> None:
    """Synchronous wrapper for ``asyncio.run``."""
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
