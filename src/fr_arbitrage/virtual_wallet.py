"""Virtual Wallet for Dry-Run mode.

Simulates account balance, margin usage, and PnL tracking without real execution.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Dict, Optional

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
        
        logger.info("virtual_wallet_initialized", balance=initial_balance)

    @property
    def account_value(self) -> float:
        """Total equity = Balance + Unrealized PnL."""
        upnl = 0.0
        for pos in self._positions.values():
            # Simply: size * (current_price - entry_price) for Long
            # But we are doing arbitrage: Spot Long + Perp Short
            # This class sees individual fills.
            # However, `OrderManager` manages the strategy.
            # Here we just track "cash" changes from fills (fees) 
            # and potentially unrealized PnL if we track mark prices?
            
            # For simplicity in this first version:
            # We will rely on "balance" being cash.
            # But true account value needs Mark Price updates.
            # If we don't stream mark prices here, we can't calculate uPnL accurately.
            
            # IMPROVEMENT: For now, we'll assume stable prices for uPnL or 
            # just track realized PnL from closed trades if we can.
            # Hyperliquid "accountValue" includes uPnL.
            pass
        
        # Since we don't easily get real-time mark prices pushed here without
        # coupling tightly to MarketDataStreamer, let's approximate:
        # account_value ~= balance (assuming delta neutral stability)
        # adjusted by realized fees and funding (if we implement funding simulation).
        return self.balance

    @property
    def total_margin_used(self) -> float:
        """Approximate margin used by open positions."""
        # In cross margin, this is sum of position values * maintenance margin
        # roughly.
        used = 0.0
        for pos in self._positions.values():
             used += (pos.size * pos.entry_price) * self.margin_maintenance
        return used

    @property
    def withdrawable(self) -> float:
        """Free collateral."""
        return max(0.0, self.account_value - self.total_margin_used)

    def update_on_fill(
        self,
        coin: str,
        market: str, # "spot" or "perp"
        side: str,   # "buy" or "sell"
        sz: float,
        px: float,
        fee: float
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
        
        logger.info(
            "virtual_wallet_updated",
            balance=round(self.balance, 4),
            margin_used=round(self.total_margin_used, 4),
            fee=fee,
            coin=coin,
            market=market
        )
