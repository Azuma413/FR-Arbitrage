"""Market Data Streamer — WebSocket-based real-time data feed.

Corresponds to README §3.1:
  - Subscribes to L2 Book for each target coin → updates MarketState
  - Periodically fetches funding rates and open interest via Info API
  - Exposes a shared ``states`` dict for other services to read
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

import structlog
from hyperliquid.info import Info
from hyperliquid.utils import constants

from fr_arbitrage.config import Settings
from fr_arbitrage.models import AssetMeta, MarketState

logger = structlog.get_logger()


class MarketDataStreamer:
    """Maintains real-time MarketState for all target coins via WebSocket + REST."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._info: Optional[Info] = None

        # Shared state — read by scanner and guardian
        self.states: Dict[str, MarketState] = {}
        self.asset_meta: Dict[str, AssetMeta] = {}

        self._stop_event = asyncio.Event()
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize Info client, load metadata, start subscriptions."""
        base_url = (
            constants.MAINNET_API_URL
            if self._settings.environment.upper() == "MAINNET"
            else constants.TESTNET_API_URL
        )

        # Info client with WebSocket enabled
        self._info = Info(base_url, skip_ws=False)

        # Load asset metadata
        await self._load_metadata()

        # Initialize MarketState for each target coin
        for coin in self._settings.target_coins:
            if coin in self._settings.blacklist_coins:
                continue
            self.states[coin] = MarketState(coin=coin)

        # Subscribe to L2 Book for each coin (perp)
        for coin in self.states:
            self._info.subscribe(
                {"type": "l2Book", "coin": coin},
                self._on_l2_book,
            )
            logger.info("ws_subscribed", coin=coin, channel="l2Book")

        # Subscribe to L2 Book for spot
        for coin in self.states:
            meta = self.asset_meta.get(coin)
            if meta and meta.spot_asset_id is not None:
                spot_coin = self._spot_coin_name(coin)
                if spot_coin:
                    self._info.subscribe(
                        {"type": "l2Book", "coin": spot_coin},
                        self._on_spot_l2_book,
                    )
                    logger.info("ws_subscribed", coin=spot_coin, channel="l2Book_spot")

        self._running = True
        self._stop_event.clear()
        logger.info("market_data_streamer_started", coins=list(self.states.keys()))

    async def stop(self) -> None:
        """Stop the streamer."""
        self._running = False
        self._stop_event.set()
        
        if self._info is not None:
             if hasattr(self._info, "disconnect_websocket"):
                 self._info.disconnect_websocket()
        
        # The SDK's websocket will be closed when Info is garbage collected
        self._info = None
        logger.info("market_data_streamer_stopped")

    # ------------------------------------------------------------------
    # Periodic funding / OI refresh (run as asyncio task)
    # ------------------------------------------------------------------

    async def run_periodic_refresh(self) -> None:
        """Periodically fetch funding rates and OI from REST API.

        Should be launched as an ``asyncio.create_task``.
        """
        while not self._stop_event.is_set():
            try:
                await self._refresh_funding_and_oi()
            except Exception as exc:
                logger.error("periodic_refresh_error", error=str(exc))
            
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                break
            except asyncio.TimeoutError:
                pass

    async def _refresh_funding_and_oi(self) -> None:
        """Fetch latest funding rates and open interest via Info API."""
        if self._info is None:
            return

        # Fetch all mids — gives us current prices too
        try:
            all_mids: Dict[str, str] = self._info.all_mids()
            for coin, state in self.states.items():
                mid = all_mids.get(coin)
                if mid is not None:
                    state.mid_price = float(mid)
                    state.last_updated = time.time()
        except Exception as exc:
            logger.warning("all_mids_fetch_error", error=str(exc))

        # Fetch meta for open interest and funding data
        try:
            meta_and_ctx = self._info.meta_and_asset_ctxs()
            if isinstance(meta_and_ctx, list) and len(meta_and_ctx) >= 2:
                universe = meta_and_ctx[0].get("universe", [])
                ctxs = meta_and_ctx[1]
                for asset_info, ctx in zip(universe, ctxs):
                    coin_name = asset_info.get("name", "")
                    if coin_name in self.states:
                        state = self.states[coin_name]
                        state.funding_rate = float(ctx.get("funding", 0))
                        state.open_interest = float(ctx.get("openInterest", 0))
                        mid = float(ctx.get("midPx", 0))
                        if mid > 0:
                            state.open_interest *= mid  # Convert to USD
        except Exception as exc:
            logger.warning("meta_ctx_fetch_error", error=str(exc))

    # ------------------------------------------------------------------
    # WebSocket callbacks
    # ------------------------------------------------------------------

    def _on_l2_book(self, data: Any) -> None:
        """Handle perp L2 Book updates."""
        try:
            book_data = data.get("data", data)
            if isinstance(book_data, dict):
                coin = book_data.get("coin", "")
                levels = book_data.get("levels", [])
                if coin in self.states and len(levels) >= 2:
                    bids = levels[0]  # List of [price, size, ...]
                    asks = levels[1]
                    state = self.states[coin]
                    if bids:
                        state.best_bid = float(bids[0].get("px", 0))
                    if asks:
                        state.best_ask = float(asks[0].get("px", 0))
                    if state.best_bid > 0 and state.best_ask > 0:
                        state.mid_price = (state.best_bid + state.best_ask) / 2
                    state.last_updated = time.time()
        except Exception as exc:
            logger.warning("l2_book_parse_error", error=str(exc))

    def _on_spot_l2_book(self, data: Any) -> None:
        """Handle spot L2 Book updates."""
        try:
            book_data = data.get("data", data)
            if isinstance(book_data, dict):
                spot_coin = book_data.get("coin", "")
                levels = book_data.get("levels", [])

                # Resolve back to base coin name
                base_coin = self._resolve_base_coin(spot_coin)
                if base_coin and base_coin in self.states and len(levels) >= 2:
                    bids = levels[0]
                    asks = levels[1]
                    state = self.states[base_coin]
                    if bids:
                        state.spot_best_bid = float(bids[0].get("px", 0))
                    if asks:
                        state.spot_best_ask = float(asks[0].get("px", 0))
                    if state.spot_best_bid > 0 and state.spot_best_ask > 0:
                        state.spot_mid_price = (
                            state.spot_best_bid + state.spot_best_ask
                        ) / 2
                    state.last_updated = time.time()
        except Exception as exc:
            logger.warning("spot_l2_book_parse_error", error=str(exc))

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def _load_metadata(self) -> None:
        """Load asset metadata (sz_decimals, px_decimals) from Info API."""
        if self._info is None:
            return

        # Perp metadata
        try:
            meta = self._info.meta()
            universe = meta.get("universe", [])
            for idx, asset in enumerate(universe):
                coin = asset.get("name", "")
                self.asset_meta[coin] = AssetMeta(
                    coin=coin,
                    perp_asset_id=idx,
                    sz_decimals=asset.get("szDecimals", 0),
                )
                logger.debug(
                    "perp_meta_loaded",
                    coin=coin,
                    asset_id=idx,
                    sz_decimals=asset.get("szDecimals"),
                )
        except Exception as exc:
            logger.error("perp_meta_load_error", error=str(exc))

        # Spot metadata
        try:
            spot_meta = self._info.spot_meta()
            spot_universe = spot_meta.get("universe", [])
            tokens = spot_meta.get("tokens", [])
            
            # Map token index to token name
            token_map = {i: t["name"] for i, t in enumerate(tokens)}

            # 1. Collect all spot assets, grouped by potential base name
            candidates: Dict[str, List[tuple]] = {}

            for idx, spot in enumerate(spot_universe):
                spot_name = spot.get("name", "")
                spot_tokens = spot.get("tokens", [])  # [base_token_idx, quote_token_idx]
                
                base_coin = None
                quote_coin = None
                
                if len(spot_tokens) >= 2:
                    base_coin = token_map.get(spot_tokens[0])
                    quote_coin = token_map.get(spot_tokens[1])
                
                # Fallback parsing
                if not base_coin and "/" in spot_name:
                    parts = spot_name.split("/")
                    base_coin = parts[0]
                    quote_coin = parts[1] if len(parts) > 1 else None

                if base_coin:
                    if base_coin not in candidates:
                        candidates[base_coin] = []
                    # Store (spot_universe_index, spot_name, quote_coin)
                    candidates[base_coin].append((idx, spot_name, quote_coin))

            # 2. Match Perp Assets to best Spot Candidate
            for coin, meta in self.asset_meta.items():
                matches = candidates.get(coin)
                
                if not matches:
                    # Try U-prefix
                    u_coin = "U" + coin
                    matches = candidates.get(u_coin)

                if matches:
                    def quote_priority(item):
                        q = item[2] # quote_coin
                        if q == "USDC": return 0
                        if q in ("USDT", "USDT0", "USDE"): return 1
                        return 2

                    matches.sort(key=quote_priority)
                    
                    best_match = matches[0]
                    spot_idx = best_match[0]
                    spot_name = best_match[1]
                    
                    meta.spot_asset_id = 10000 + spot_idx
                    meta.spot_name = spot_name
                    
                    logger.info(
                        "spot_meta_associated",
                        perp_coin=coin,
                        spot_base=coin if matches == candidates.get(coin) else "U"+coin,
                        spot_name=spot_name,
                        quote=best_match[2]
                    )

        except Exception as exc:
            logger.error("spot_meta_load_error", error=str(exc))

    def _spot_coin_name(self, base_coin: str) -> Optional[str]:
        """Get the spot coin name for WebSocket subscription."""
        meta = self.asset_meta.get(base_coin)
        if meta and meta.spot_name:
            return meta.spot_name
        return None

    def _resolve_base_coin(self, spot_coin: str) -> Optional[str]:
        """Resolve a spot coin name back to the base coin."""
        # Check direct mapping from loaded metadata
        for coin, meta in self.asset_meta.items():
            if meta.spot_name == spot_coin:
                return coin
        return None
