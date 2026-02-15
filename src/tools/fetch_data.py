"""fetch_data.py — Download historical OHLCV + Funding Rate data from Bybit.

Usage:
    uv run python src/tools/fetch_data.py --symbol DOGE/USDT --days 30

Outputs CSV files to ``data/`` directory:
    - {SYMBOL}_spot_1m.csv
    - {SYMBOL}_perp_1m.csv
    - {SYMBOL}_funding.csv
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt.async_support as ccxt
import pandas as pd
from loguru import logger


DATA_DIR = Path("data")


# ---------------------------------------------------------------------------
# OHLCV downloader (paginated)
# ---------------------------------------------------------------------------

async def fetch_ohlcv_all(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    since_ms: int,
    until_ms: int,
) -> pd.DataFrame:
    """Fetch OHLCV data with pagination (ccxt returns max ~200 candles per call)."""
    all_candles: list[list] = []
    cursor = since_ms
    limit = 200

    while cursor < until_ms:
        candles = await exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=cursor, limit=limit
        )
        if not candles:
            break
        all_candles.extend(candles)
        # Move cursor past last candle
        cursor = candles[-1][0] + 1
        logger.debug(
            "  fetched {} candles for {} (up to {})",
            len(candles),
            symbol,
            datetime.fromtimestamp(cursor / 1000, tz=timezone.utc).isoformat(),
        )
        # Respect rate limit
        await asyncio.sleep(0.2)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.drop_duplicates(subset=["timestamp"], inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Funding Rate history downloader
# ---------------------------------------------------------------------------

async def fetch_funding_history(
    exchange: ccxt.Exchange,
    symbol: str,
    since_ms: int,
    until_ms: int,
) -> pd.DataFrame:
    """Fetch funding rate history with pagination."""
    all_records: list[dict] = []
    cursor = since_ms
    limit = 200

    while cursor < until_ms:
        try:
            records = await exchange.fetch_funding_rate_history(
                symbol, since=cursor, limit=limit
            )
        except Exception as exc:
            logger.warning("FR history fetch error: {} — stopping pagination", exc)
            break

        if not records:
            break

        for rec in records:
            all_records.append(
                {
                    "timestamp": rec.get("timestamp"),
                    "funding_rate": rec.get("fundingRate"),
                }
            )
        cursor = records[-1]["timestamp"] + 1
        logger.debug(
            "  fetched {} FR records (up to {})",
            len(records),
            datetime.fromtimestamp(cursor / 1000, tz=timezone.utc).isoformat(),
        )
        await asyncio.sleep(0.2)

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.drop_duplicates(subset=["timestamp"], inplace=True)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def download(symbol: str, days: int) -> None:
    """Download spot OHLCV, perp OHLCV, and FR history for *symbol*."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = symbol.replace("/", "")  # e.g. "DOGEUSDT"

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_ms = int(since.timestamp() * 1000)
    until_ms = int(now.timestamp() * 1000)

    logger.info("Downloading data for {} — past {} days", symbol, days)

    # --- Spot OHLCV ---------------------------------------------------------
    logger.info("Fetching Spot OHLCV 1m ...")
    spot_exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    try:
        spot_df = await fetch_ohlcv_all(spot_exchange, symbol, "1m", since_ms, until_ms)
        spot_path = DATA_DIR / f"{safe_name}_spot_1m.csv"
        spot_df.to_csv(spot_path, index=False)
        logger.info("  Saved {} rows → {}", len(spot_df), spot_path)
    finally:
        await spot_exchange.close()

    # --- Perp OHLCV ---------------------------------------------------------
    perp_symbol = f"{symbol}:USDT"
    logger.info("Fetching Perp OHLCV 1m ({}) ...", perp_symbol)
    perp_exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    try:
        perp_df = await fetch_ohlcv_all(perp_exchange, perp_symbol, "1m", since_ms, until_ms)
        perp_path = DATA_DIR / f"{safe_name}_perp_1m.csv"
        perp_df.to_csv(perp_path, index=False)
        logger.info("  Saved {} rows → {}", len(perp_df), perp_path)
    finally:
        await perp_exchange.close()

    # --- Funding Rate History -----------------------------------------------
    logger.info("Fetching Funding Rate history ({}) ...", perp_symbol)
    fr_exchange = ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    try:
        fr_df = await fetch_funding_history(fr_exchange, perp_symbol, since_ms, until_ms)
        fr_path = DATA_DIR / f"{safe_name}_funding.csv"
        fr_df.to_csv(fr_path, index=False)
        logger.info("  Saved {} rows → {}", len(fr_df), fr_path)
    finally:
        await fr_exchange.close()

    logger.info("=== Download complete for {} ===", symbol)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download historical data for backtesting")
    parser.add_argument("--symbol", type=str, default="DOGE/USDT", help="Trading pair (e.g. DOGE/USDT)")
    parser.add_argument("--days", type=int, default=30, help="Number of days to download (default: 30)")
    args = parser.parse_args()

    asyncio.run(download(args.symbol, args.days))


if __name__ == "__main__":
    main()
