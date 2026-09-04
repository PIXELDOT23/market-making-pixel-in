"""
risk_manager.py
----------------
Enforces hard limits. This is deliberately kept separate from strategy logic
so a bug in the quoting code can't silently bypass risk checks.
"""

import time
from collections import deque
from datetime import datetime

import config
import logger as log


class RiskManager:
    def __init__(self):
        self.net_position = 0            # +ve = net long, -ve = net short
        self.realized_pnl = 0.0
        self.order_timestamps = deque()  # for per-minute throttling
        self.halted = False
        self.halt_reason = ""

    # ---- throttle: don't fire more than N orders/min ----
    def can_place_order(self) -> bool:
        if self.halted:
            return False
        now = time.time()
        while self.order_timestamps and now - self.order_timestamps[0] > 60:
            self.order_timestamps.popleft()
        if len(self.order_timestamps) >= config.MAX_ORDERS_PER_MINUTE:
            return False
        return True

    def record_order_sent(self):
        self.order_timestamps.append(time.time())

    # ---- position limits: which sides are still allowed to quote ----
    def can_quote_buy(self) -> bool:
        return not self.halted and self.net_position < config.MAX_POSITION_QTY

    def can_quote_sell(self) -> bool:
        return not self.halted and self.net_position > -config.MAX_POSITION_QTY

    # ---- update state on fills ----
    def on_fill(self, side: int, qty: int, price: float, ref_price: float):
        """side: 1 = buy, -1 = sell. ref_price used to mark-to-market roughly."""
        signed_qty = qty if side == 1 else -qty
        self.net_position += signed_qty
        # rough realized PnL approximation on the trade itself vs reference
        self.realized_pnl -= signed_qty * price
        self._check_daily_loss(ref_price)

    def _check_daily_loss(self, mark_price: float):
        # mark-to-market open position at current price for a fuller PnL picture
        mtm = self.realized_pnl + self.net_position * mark_price
        if mtm <= -abs(config.MAX_DAILY_LOSS_RS):
            self.halt("Daily loss limit breached")

    def halt(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        log.halt(reason + " — squaring off and stopping.")

    # ---- trading window ----
    @staticmethod
    def within_trading_window() -> bool:
        now = datetime.now().strftime("%H:%M")
        return config.TRADING_START <= now <= config.TRADING_END

    def status(self) -> str:
        return (
            f"pos={self.net_position} realized_pnl={self.realized_pnl:.2f} "
            f"halted={self.halted}"
        )