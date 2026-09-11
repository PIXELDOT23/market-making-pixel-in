"""
risk_manager.py
----------------
Enforces hard risk and margin limits. Deliberately separated from strategy
logic so execution bugs cannot bypass risk controls.
"""

import time
from collections import deque
from datetime import datetime

import config
import logger as log


class RiskManager:
    def __init__(self):
        self.net_position = 0            # +ve = net long, -ve = net short (in QUOTE_QTY units / lots)
        self.realized_pnl = 0.0          # in INR
        self.order_timestamps = deque()  # for per-minute throttling
        self.halted = False
        self.halt_reason = ""
        self.last_margin_avail = 0.0
        self.last_margin_required = 0.0

    # ---- throttle: don't fire more than N orders/min ----
    def can_place_order(self) -> bool:
        if self.halted:
            return False
        now = time.time()
        while self.order_timestamps and now - self.order_timestamps[0] > 60:
            self.order_timestamps.popleft()
        if len(self.order_timestamps) >= config.MAX_ORDERS_PER_MINUTE:
            log.warn(f"Order throttle hit: {len(self.order_timestamps)} orders in past 60s (max {config.MAX_ORDERS_PER_MINUTE}/min).")
            return False
        return True

    def record_order_sent(self):
        self.order_timestamps.append(time.time())

    # ---- position limits: which sides are still allowed to quote ----
    def can_quote_buy(self) -> bool:
        return not self.halted and self.net_position < config.MAX_POSITION_QTY

    def can_quote_sell(self) -> bool:
        return not self.halted and self.net_position > -config.MAX_POSITION_QTY

    # ---- margin limits validation ----
    def can_afford_margin(self, required_margin: float, available_margin: float) -> bool:
        self.last_margin_required = required_margin
        self.last_margin_avail = available_margin
        if self.halted:
            return False
        min_required = required_margin + getattr(config, "MIN_FREE_MARGIN_BUFFER_RS", 0.0)
        if available_margin < min_required:
            log.warn(
                f"Margin check failed: required ₹{required_margin:,.2f} + buffer ₹{config.MIN_FREE_MARGIN_BUFFER_RS:,.2f} "
                f"= ₹{min_required:,.2f}, but available margin is only ₹{available_margin:,.2f}."
            )
            return False
        return True

    # ---- update state on fills ----
    def on_fill(self, side: int, qty: int, price: float, ref_price: float):
        """
        side: 1 = buy, -1 = sell.
        qty: number of contracts/shares.
        ref_price: current market price used to mark-to-market.
        """
        signed_qty = qty if side == 1 else -qty
        self.net_position += signed_qty

        # Realized cashflow from trade (scaled by LOT_SIZE for derivatives)
        multiplier = getattr(config, "LOT_SIZE", 1)
        self.realized_pnl -= signed_qty * price * multiplier
        self._check_daily_loss(ref_price)

    def _check_daily_loss(self, mark_price: float):
        multiplier = getattr(config, "LOT_SIZE", 1)
        mtm = self.realized_pnl + (self.net_position * mark_price * multiplier)
        if mtm <= -abs(config.MAX_DAILY_LOSS_RS):
            self.halt(f"Daily loss limit breached: MTM = ₹{mtm:,.2f} <= -₹{config.MAX_DAILY_LOSS_RS:,.2f}")

    def halt(self, reason: str):
        self.halted = True
        self.halt_reason = reason
        log.halt(f"{reason} — squaring off and stopping.")

    # ---- trading window ----
    @staticmethod
    def within_trading_window() -> bool:
        now = datetime.now().strftime("%H:%M")
        return config.TRADING_START <= now <= config.TRADING_END

    def status(self) -> str:
        return (
            f"pos={self.net_position} realized_pnl=₹{self.realized_pnl:,.2f} "
            f"halted={self.halted}"
        )