"""backtest.py — Backtest runner for the FR-Arbitrage strategy.

Usage:
    uv run python src/backtest.py --symbol DOGE/USDT [--data-dir data]

Injects a MockExchange into MarketScanner / OrderManager and simulates
the trading loop minute-by-minute over historical data.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from loguru import logger

from fr_arbitrage.config import Settings
from fr_arbitrage.market_scanner import MarketScanner
from fr_arbitrage.mocks import MockExchange
from fr_arbitrage.models import ActivePosition
from fr_arbitrage.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Backtest bookkeeping
# ---------------------------------------------------------------------------

FUNDING_HOURS_UTC = {0, 8, 16}  # Funding settlement times


@dataclass
class BacktestResult:
    """Accumulated metrics from a backtest run."""

    total_funding_income: float = 0.0
    total_fees: float = 0.0
    total_entry_cost: float = 0.0
    total_exit_revenue: float = 0.0
    num_trades: int = 0
    num_wins: int = 0
    pnl_history: list[float] = field(default_factory=list)

    @property
    def net_pnl(self) -> float:
        return (
            self.total_funding_income
            + self.total_exit_revenue
            - self.total_entry_cost
            - self.total_fees
        )

    @property
    def max_drawdown(self) -> float:
        if not self.pnl_history:
            return 0.0
        peak = self.pnl_history[0]
        max_dd = 0.0
        for pnl in self.pnl_history:
            if pnl > peak:
                peak = pnl
            dd = peak - pnl
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def win_rate(self) -> float:
        return self.num_wins / self.num_trades if self.num_trades > 0 else 0.0


# ---------------------------------------------------------------------------
# Core backtest loop
# ---------------------------------------------------------------------------

async def run_backtest(symbol: str, data_dir: str) -> BacktestResult:
    """Execute the backtest and return results."""
    settings = Settings()

    # --- Create mock exchange and inject into components --------------------
    mock = MockExchange(symbol=symbol, data_dir=data_dir)
    await mock.load_markets()

    scanner = MarketScanner(settings, exchange=mock)
    order_mgr = OrderManager(
        settings, spot_exchange=mock, perp_exchange=mock
    )

    await scanner.start()
    await order_mgr.start()

    result = BacktestResult()
    positions: list[ActivePosition] = []
    prev_hour: int | None = None

    logger.info("=== Backtest starting: {} ({} timesteps) ===", symbol, mock.total_steps)

    # --- Main loop ----------------------------------------------------------
    while True:
        current_dt = mock.current_datetime()
        current_hour = current_dt.hour

        # 1. Funding income: check if we crossed a funding settlement hour
        if (
            prev_hour is not None
            and current_hour in FUNDING_HOURS_UTC
            and prev_hour != current_hour
        ):
            fr_data = await mock.fetch_funding_rate(f"{symbol}:USDT")
            current_fr = fr_data.get("fundingRate", 0.0)
            for pos in positions:
                if pos.status == "OPEN":
                    # FR income = rate × position_notional (perp short earns positive FR)
                    perp_price = (await mock.fetch_tickers()).get(
                        f"{symbol}:USDT", {}
                    ).get("last", 0.0)
                    notional = pos.perp_qty * perp_price
                    funding_income = current_fr * notional
                    result.total_funding_income += funding_income
                    logger.debug(
                        "  Funding: {} FR={:.6%} notional={:.2f} income={:.4f}",
                        pos.symbol,
                        current_fr,
                        notional,
                        funding_income,
                    )

        prev_hour = current_hour

        # 2. Scan for entry targets
        targets = await scanner.scan()

        open_count = sum(1 for p in positions if p.status == "OPEN")
        already_held = {p.symbol for p in positions if p.status == "OPEN"}

        for target in targets:
            if open_count >= settings.max_open_positions:
                break
            spot_sym = target.symbol.replace(":USDT", "")
            if spot_sym in already_held:
                continue

            pos = await order_mgr.execute_entry(
                target, settings.investment_amount_usdt
            )
            if pos is not None:
                # Track entry cost
                entry_trades = [
                    t
                    for t in mock.trades
                    if t.side == "buy" and t.symbol == spot_sym
                ]
                if entry_trades:
                    last_buy = entry_trades[-1]
                    result.total_entry_cost += last_buy.qty * last_buy.price
                result.total_fees += pos.total_fees
                positions.append(pos)
                open_count += 1
                result.num_trades += 1

        # 3. Check exit conditions
        for pos in positions:
            if pos.status != "OPEN":
                continue
            perp_sym = f"{pos.symbol}:USDT"
            fr_data = await mock.fetch_funding_rate(perp_sym)
            current_fr = fr_data.get("fundingRate", 0.0)

            if current_fr <= settings.exit_funding_rate:
                logger.info(
                    "Exit trigger: {} FR={:.6%} < threshold",
                    pos.symbol,
                    current_fr,
                )
                pos.status = "CLOSING"
                success = await order_mgr.execute_exit(pos)
                if success:
                    # Track exit revenue
                    exit_trades = [
                        t
                        for t in mock.trades
                        if t.side == "sell" and t.symbol == pos.symbol
                    ]
                    if exit_trades:
                        last_sell = exit_trades[-1]
                        result.total_exit_revenue += last_sell.qty * last_sell.price
                    # Simple win check: did funding income cover fees?
                    if result.total_funding_income > result.total_fees:
                        result.num_wins += 1

        # 4. Record PnL snapshot
        result.pnl_history.append(result.net_pnl)

        # 5. Advance time
        if not mock.advance_time(1):
            break

        # Progress logging every 10,000 steps
        if mock.current_step % 10000 == 0:
            logger.info(
                "  Step {}/{} — PnL: {:.4f} USDT",
                mock.current_step,
                mock.total_steps,
                result.net_pnl,
            )

    # --- Close remaining positions ------------------------------------------
    for pos in positions:
        if pos.status == "OPEN":
            logger.info("Closing remaining position: {}", pos.symbol)
            pos.status = "CLOSING"
            await order_mgr.execute_exit(pos)
            exit_trades = [
                t for t in mock.trades if t.side == "sell" and t.symbol == pos.symbol
            ]
            if exit_trades:
                last_sell = exit_trades[-1]
                result.total_exit_revenue += last_sell.qty * last_sell.price

    await scanner.close()
    await order_mgr.close()

    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(symbol: str, result: BacktestResult) -> None:
    """Print a formatted backtest report to stdout."""
    print("\n" + "=" * 60)
    print(f"  BACKTEST REPORT: {symbol}")
    print("=" * 60)
    print(f"  Total Trades:        {result.num_trades}")
    print(f"  Win Rate:            {result.win_rate:.1%}")
    print(f"  Funding Income:      {result.total_funding_income:>12.4f} USDT")
    print(f"  Total Fees:          {result.total_fees:>12.4f} USDT")
    print(f"  Entry Cost:          {result.total_entry_cost:>12.4f} USDT")
    print(f"  Exit Revenue:        {result.total_exit_revenue:>12.4f} USDT")
    print("  " + "-" * 56)
    print(f"  Net PnL:             {result.net_pnl:>12.4f} USDT")
    print(f"  Max Drawdown:        {result.max_drawdown:>12.4f} USDT")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<level>{message}</level>"
        ),
    )

    parser = argparse.ArgumentParser(description="FR-Arbitrage Backtest Runner")
    parser.add_argument(
        "--symbol",
        type=str,
        default="DOGE/USDT",
        help="Trading pair (e.g. DOGE/USDT)",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data",
        help="Directory containing CSV data files",
    )
    args = parser.parse_args()

    result = asyncio.run(run_backtest(args.symbol, args.data_dir))
    print_report(args.symbol, result)


if __name__ == "__main__":
    main()
