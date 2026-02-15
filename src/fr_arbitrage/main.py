"""Main entry point — async event loop for the Hyperliquid Yield Harvester.

Launches 4 concurrent services via asyncio.gather:
  1. Market Data Streamer (WebSocket)
  2. Opportunity Scanner (periodic scan → entry)
  3. Position Guardian (periodic check → exit/rebalance)
  4. Health monitor / kill-switch
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Set

import structlog

from fr_arbitrage.config import Settings
from fr_arbitrage.database import close_db, get_open_positions, init_db
from fr_arbitrage.market_data import MarketDataStreamer
from fr_arbitrage.market_scanner import OpportunityScanner
from fr_arbitrage.order_manager import OrderManager
from fr_arbitrage.position_guardian import PositionGuardian

logger = structlog.get_logger()

# Flag for graceful shutdown
_shutdown_event = asyncio.Event()


# ---------------------------------------------------------------------------
# Service tasks
# ---------------------------------------------------------------------------

async def _scanner_loop(
    scanner: OpportunityScanner,
    order_mgr: OrderManager,
    settings: Settings,
) -> None:
    """Periodically scan for opportunities and execute entries."""
    # Wait for initial market data to populate
    await asyncio.sleep(10)

    while not _shutdown_event.is_set():
        try:
            # Get currently held symbols from DB
            positions = await get_open_positions()
            held: Set[str] = {p.symbol for p in positions}

            targets = scanner.scan(held)

            open_count = len(held)
            for target in targets:
                if open_count >= settings.max_open_positions:
                    logger.info(
                        "max_positions_reached",
                        max=settings.max_open_positions,
                    )
                    break

                position = await order_mgr.execute_entry(
                    target, settings.max_position_usdc
                )
                if position is not None:
                    # Check if position is imbalanced (entry failed rollback)
                    if position.perp_sz <= 0 and position.spot_sz > 0:
                         logger.warning("imbalanced_entry_detected_triggering_guardian", coin=position.symbol)
                         pass

                    open_count += 1

        except Exception as exc:
            logger.error("scanner_loop_error", error=str(exc))

        try:
            await asyncio.wait_for(
                _shutdown_event.wait(),
                timeout=settings.scan_interval_sec,
            )
            break  # shutdown was signaled
        except asyncio.TimeoutError:
            pass  # normal: timeout = time to scan again


async def _kill_switch_monitor(settings: Settings) -> None:
    """Periodically re-read settings to detect EMERGENCY_STOP."""
    while not _shutdown_event.is_set():
        try:
            live = Settings()
            if live.emergency_stop:
                logger.critical("emergency_stop_detected")
                _shutdown_event.set()
                return
        except Exception:
            pass

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# Structured logging setup
# ---------------------------------------------------------------------------

def _setup_logging(log_level: str) -> None:
    """Configure structlog with JSON + console rendering."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_bot() -> None:
    """Core async entry: initialize all services and run concurrently."""
    settings = Settings()
    _setup_logging(settings.log_level)

    log = structlog.get_logger()

    log.info(
        "bot_starting",
        environment=settings.environment,
        dry_run=settings.dry_run,
        target_coins=settings.target_coins,
        max_position_usdc=settings.max_position_usdc,
    )

    if settings.emergency_stop:
        log.critical("emergency_stop_on_startup")
        return

    # --- Initialize database ------------------------------------------------
    await init_db(settings.db_url)

    # --- Initialize Market Data Streamer ------------------------------------
    streamer = MarketDataStreamer(settings)
    await streamer.start()

    # --- Initialize components (sharing streamer's state) -------------------
    scanner = OpportunityScanner(settings, streamer.states)
    order_mgr = OrderManager(settings, streamer.states, streamer.asset_meta)
    guardian = PositionGuardian(
        settings, streamer.states, streamer.asset_meta, order_mgr
    )

    await order_mgr.start()
    await guardian.start()

    # --- Setup signal handlers for graceful shutdown ------------------------
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    log.info("all_services_initialized")

    # --- Run all services concurrently --------------------------------------
    try:
        await asyncio.gather(
            streamer.run_periodic_refresh(),      # Service 1: Market data
            _scanner_loop(scanner, order_mgr, settings),  # Service 2: Scanner
            guardian.run(),                        # Service 3: Guardian
            _kill_switch_monitor(settings),        # Service 4: Health
        )
    except asyncio.CancelledError:
        log.info("tasks_cancelled")
    except KeyboardInterrupt:
        log.info("keyboard_interrupt")
    finally:
        _shutdown_event.set()
        await guardian.stop()
        await streamer.stop()
        await order_mgr.close()
        await close_db()
        log.info("bot_stopped")


def main() -> None:
    """Synchronous wrapper for ``asyncio.run``."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
