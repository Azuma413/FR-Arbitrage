"""Virtual Wallet for Dry-Run mode.

Simulates account balance, margin usage, and PnL tracking without real execution.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Dict, Optional, TYPE_CHECKING
import time

if TYPE_CHECKING:
    from fr_arbitrage.models import MarketState

logger = structlog.get_logger()

@dataclass
class VirtualPosition:
    """Tracks a single virtual position for margin checks."""
    symbol: str
    size: float
    entry_price: float
    current_price: float = 0.0

class VirtualWallet:
    """Simulates a Hyperliquid account wallet."""

    def __init__(self, initial_balance: float = 10000.0) -> None:
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self._positions: Dict[str, VirtualPosition] = {}
        
        # Hyperliquid specific margin parameters (approximate)
        self.margin_maintenance = 0.05 # 5% maintenance margin
        self.last_funding_time = time.time()
        
        logger.info("virtual_wallet_initialized", balance=initial_balance)

    def get_account_value(self, market_states: Dict[str, MarketState]) -> float:
        """Total equity = Balance + Unrealized PnL."""
        upnl = 0.0
        for key, pos in self._positions.items():
            # Get current price from market state
            # Symbol key format: "COIN-spot" or "COIN-perp"
            coin = pos.symbol.split("-")[0]
            state = market_states.get(coin)
            if not state:
                continue

            # Use mid_price for valuation
            current_price = state.mid_price
            if current_price <= 0:
                current_price = pos.entry_price

            # PnL = Size * (Current Price - Entry Price)
            # Works for both Long (Size>0) and Short (Size<0)
            pnl = pos.size * (current_price - pos.entry_price)
            upnl += pnl

        return self.balance + upnl

    def get_total_margin_used(self, market_states: Dict[str, MarketState]) -> float:
        """Approximate margin used by open positions."""
        # In cross margin, this is sum of position values * maintenance margin
        # roughly.
        used = 0.0
        for key, pos in self._positions.items():
             coin = pos.symbol.split("-")[0]
             state = market_states.get(coin)
             price = state.mid_price if state else pos.entry_price
             if price <= 0: price = pos.entry_price
             
             used += (abs(pos.size) * price) * self.margin_maintenance
        return used

    def get_withdrawable(self, market_states: Dict[str, MarketState]) -> float:
        """Free collateral."""
        equity = self.get_account_value(market_states)
        margin = self.get_total_margin_used(market_states)
        return max(0.0, equity - margin)

    def update_on_fill(
        self,
        coin: str,
        market: str, # "spot" or "perp"
        side: str,   # "buy" or "sell"
        sz: float,
        px: float,
        fee: float,
        market_states: Dict[str, MarketState] = None
    ) -> None:
        """Update wallet state based on a filled order."""
        
        # 1. Deduct Fee
        self.balance -= fee
        
        # 2. Update Position tracking
        # Key for position tracking: "HYPE-spot" or "HYPE-perp"
        key = f"{coin}-{market}"
        
        if key not in self._positions:
            self._positions[key] = VirtualPosition(symbol=key, size=0.0, entry_price=0.0)
            
        pos = self._positions[key]
        
        # Signed size tracking for margin
        signed_sz_change = sz if side == "buy" else -sz
        
        # Update size
        new_size = pos.size + signed_sz_change
        
        # Realized PnL calculation on reduction
        if (pos.size > 0 and signed_sz_change < 0) or (pos.size < 0 and signed_sz_change > 0):
            # Closing trade
            # Pnl = (Exit Price - Entry Price) * qty * (1 if Long else -1)
            # Long (size > 0): Sell (change < 0) -> (Px - Entry) * Qty * 1
            # Short (size < 0): Buy (change > 0) -> (Px - Entry) * Qty * -1
            
            qty_closing = min(abs(pos.size), abs(signed_sz_change))
            direction = 1 if pos.size > 0 else -1
            
            pnl = (px - pos.entry_price) * qty_closing * direction
            self.balance += pnl
            
            if new_size == 0:
                pos.entry_price = 0.0
        
        elif new_size != 0:
            # Opening trade (increase size or flip)
            # Update avg entry only if increasing in same direction or flip?
            # Simplified: just weighted average for now if increasing. 
            # If flip, it's complex, but our bot doesn't flip.
            
            if (pos.size >= 0 and signed_sz_change > 0) or (pos.size <= 0 and signed_sz_change < 0):
                total_cost = (abs(pos.size) * pos.entry_price) + (sz * px)
                pos.entry_price = total_cost / (abs(pos.size) + sz)
            elif (pos.size == 0):
                 pos.entry_price = px
            
            
        pos.size = new_size
        
        margin_used = 0.0
        if market_states:
             margin_used = self.get_total_margin_used(market_states)

        logger.info(
            "virtual_wallet_updated",
            balance=round(self.balance, 4),
            margin_used=round(margin_used, 4),
            fee=fee,
            coin=coin,
            market=market
        )

    def apply_funding(self, market_states: Dict[str, 'MarketState']) -> None:
        """Check for hourly funding and apply payments if crossed."""
        now = time.time()
        current_hour = int(now / 3600)
        last_hour = int(self.last_funding_time / 3600)
        
        if current_hour > last_hour:
            total_funding_pnl = 0.0
            
            for key, pos in self._positions.items():
                # Only perp positions pay/receive funding
                if not key.endswith("-perp"):
                    continue
                    
                # Extract coin symbol "HYPE-perp" -> "HYPE"
                coin = pos.symbol.split("-")[0]
                state = market_states.get(coin)
                
                if not state:
                    continue
                    
                # Funding logic: 
                # Funding Payment = -1 * Size * Price * Rate
                # If Rate > 0: Longs pay (Size>0 -> Payment<0), Shorts receive (Size<0 -> Payment>0)
                
                # Use mark price (mid_price) for calculation
                price = state.mid_price
                if price <= 0:
                    price = pos.entry_price # Fallback
                
                funding_pnl = -1 * pos.size * price * state.funding_rate
                
                total_funding_pnl += funding_pnl
                
                logger.info(
                    "funding_applied_position",
                    coin=coin,
                    size=pos.size,
                    rate=state.funding_rate,
                    payment=funding_pnl
                )
            
            if total_funding_pnl != 0:
                self.balance += total_funding_pnl
                logger.info(
                    "hourly_funding_complete", 
                    total_pnl=total_funding_pnl, 
                    new_balance=self.balance
                )
                
            self.last_funding_time = now

