"""
bot.py
------
Simple two-sided market maker for a single equity symbol on Fyers.

Strategy (kept intentionally simple and readable):
  - Every REQUOTE_INTERVAL_SEC, fetch the current market quote (LTP + bid/ask).
  - Compute a mid price, then place a small BUY limit a few ticks below mid
    and a small SELL limit a few ticks above mid.
  - Only one live order per side at a time — cancel & replace instead of stacking.
  - Skip a side entirely if inventory limit for that direction is hit.
  - Every loop: check risk manager. If halted, cancel all orders, square off
    any open position at market, and stop.

Run:
    python auth.py     # once per day, to log in
    python bot.py       # starts the bot
"""

import time
import sys

import config
import logger as log
from auth import get_fyers_client
from risk_manager import RiskManager
from cost_model import round_trip_cost, breakeven_spread_ticks


def round_to_tick(price: float) -> float:
    return round(round(price / config.TICK_SIZE) * config.TICK_SIZE, 2)


def effective_spread_ticks(mid_price: float) -> int:
    """
    Returns the spread (in ticks) the bot will actually quote at, after
    checking it covers real Fyers/NSE charges for the configured qty and
    product type. Never silently quotes at a guaranteed loss.
    """
    breakeven = breakeven_spread_ticks(
        mid_price, config.QUOTE_QTY, config.PRODUCT_TYPE, config.TICK_SIZE, config.SEGMENT
    )
    target = breakeven + config.MIN_PROFIT_MARGIN_TICKS

    if config.SPREAD_TICKS >= target:
        return config.SPREAD_TICKS  # your configured spread already clears costs

    if config.AUTO_WIDEN_SPREAD:
        log.warn(
            f"cost-check: configured SPREAD_TICKS={config.SPREAD_TICKS} is below "
            f"breakeven ({breakeven} ticks) at qty={config.QUOTE_QTY}. "
            f"Auto-widening to {target} ticks to actually be profitable."
        )
        return target

    cost = round_trip_cost(mid_price, config.QUOTE_QTY, config.PRODUCT_TYPE, config.SEGMENT)
    log.error(
        f"cost-check: SPREAD_TICKS={config.SPREAD_TICKS} "
        f"(Rs {config.SPREAD_TICKS * config.TICK_SIZE:.2f}) is BELOW the Rs {cost:.2f} "
        f"round-trip cost at qty={config.QUOTE_QTY}. Quoting anyway because "
        f"AUTO_WIDEN_SPREAD=False — every completed round trip will lose money."
    )
    return config.SPREAD_TICKS


def get_mid_price(fyers) -> float | None:
    resp = fyers.quotes({"symbols": config.SYMBOL})
    if resp.get("s") != "ok" or not resp.get("d"):
        log.error(f"Quote fetch failed: {resp}")
        return None
    q = resp["d"][0]["v"]
    bid = q.get("bid")
    ask = q.get("ask")
    ltp = q.get("lp")
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2
    return ltp  # fallback if depth unavailable


def get_open_orders(fyers):
    resp = fyers.orderbook()
    if resp.get("s") != "ok":
        return []
    # status 6 = pending/open in Fyers order status codes
    return [o for o in resp.get("orderBook", []) if o.get("status") == 6
            and o.get("symbol") == config.SYMBOL]


def cancel_order(fyers, order_id: str):
    resp = fyers.cancel_order({"id": order_id})
    if resp.get("s") != "ok":
        log.error(f"Cancel failed for {order_id}: {resp}")
    return resp


def place_limit(fyers, side: int, price: float, risk: RiskManager):
    if not risk.can_place_order():
        return None
    data = {
        "symbol": config.SYMBOL,
        "qty": config.QUOTE_QTY,
        "type": 1,  # limit order
        "side": side,  # 1 = buy, -1 = sell
        "productType": config.PRODUCT_TYPE,
        "limitPrice": round_to_tick(price),
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
        "stopLoss": 0,
        "takeProfit": 0,
        "isSliceOrder": False,
    }
    resp = fyers.place_order(data)
    risk.record_order_sent()
    if resp.get("s") != "ok":
        log.error(f"Order placement failed ({'BUY' if side == 1 else 'SELL'}): {resp}")
        return None
    log.trade(f"Placed {'BUY' if side == 1 else 'SELL'} {config.QUOTE_QTY} @ {round_to_tick(price)}")
    return resp.get("id")


def square_off_all(fyers):
    """Flatten any open position at market and cancel all live orders."""
    log.warn("Squaring off all open orders and positions...")
    for o in get_open_orders(fyers):
        cancel_order(fyers, o["id"])

    resp = fyers.positions()
    if resp.get("s") != "ok":
        log.error(f"Could not fetch positions to square off: {resp}")
        return
    for pos in resp.get("netPositions", []):
        if pos.get("symbol") == config.SYMBOL and pos.get("netQty", 0) != 0:
            qty = pos["netQty"]
            side = -1 if qty > 0 else 1  # opposite side to flatten
            data = {
                "symbol": config.SYMBOL,
                "qty": abs(qty),
                "type": 2,  # market order — priority is exiting, not price
                "side": side,
                "productType": config.PRODUCT_TYPE,
                "limitPrice": 0,
                "stopPrice": 0,
                "validity": "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
                "stopLoss": 0,
                "takeProfit": 0,
            }
            resp2 = fyers.place_order(data)
            log.trade(f"Square-off order: {resp2}")


def sync_position_from_broker(fyers, risk: RiskManager):
    """Pull true net position from broker rather than trusting local state."""
    resp = fyers.positions()
    if resp.get("s") != "ok":
        return
    for pos in resp.get("netPositions", []):
        if pos.get("symbol") == config.SYMBOL:
            risk.net_position = pos.get("netQty", 0)
            risk.realized_pnl = pos.get("realized_profit", risk.realized_pnl)


def run():
    fyers = get_fyers_client()
    risk = RiskManager()

    log.banner("Fyers Market Making Bot")
    log.success(f"Connected. Market making {config.SYMBOL}, qty={config.QUOTE_QTY} per side, product={config.PRODUCT_TYPE}.")

    # cost summary using current quote, so you see the economics before any order goes out
    mid_now = get_mid_price(fyers)
    if mid_now:
        cost = round_trip_cost(mid_now, config.QUOTE_QTY, config.PRODUCT_TYPE, config.SEGMENT)
        be_ticks = breakeven_spread_ticks(mid_now, config.QUOTE_QTY, config.PRODUCT_TYPE, config.TICK_SIZE, config.SEGMENT)
        log.info(
            f"Cost check @ mid={mid_now:.2f}: round-trip charges = Rs {cost:.2f}, "
            f"breakeven spread = {be_ticks} ticks (Rs {be_ticks * config.TICK_SIZE:.2f}). "
            f"Configured SPREAD_TICKS = {config.SPREAD_TICKS}."
        )

    log.info("Press Ctrl+C to stop safely (will square off).")

    try:
        while True:
            if not RiskManager.within_trading_window():
                log.status("Outside trading window, waiting...")
                time.sleep(30)
                continue

            sync_position_from_broker(fyers, risk)

            if risk.halted:
                square_off_all(fyers)
                break

            mid = get_mid_price(fyers)
            if mid is None:
                time.sleep(config.REQUOTE_INTERVAL_SEC)
                continue

            spread_ticks = effective_spread_ticks(mid)
            half_spread = (spread_ticks * config.TICK_SIZE) / 2
            bid_price = mid - half_spread
            ask_price = mid + half_spread

            # cancel existing quotes before replacing (keeps at most 1 per side)
            for o in get_open_orders(fyers):
                cancel_order(fyers, o["id"])

            if risk.can_quote_buy():
                place_limit(fyers, 1, bid_price, risk)
            else:
                log.warn("Max long inventory reached, skipping buy quote.")

            if risk.can_quote_sell():
                place_limit(fyers, -1, ask_price, risk)
            else:
                log.warn("Max short inventory reached, skipping sell quote.")

            log.status(
                f"mid={mid:.2f} spread_ticks={spread_ticks} "
                f"bid={round_to_tick(bid_price)} ask={round_to_tick(ask_price)} | {risk.status()}"
            )
            time.sleep(config.REQUOTE_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.warn("Stopping by user request...")
        square_off_all(fyers)
        sys.exit(0)


if __name__ == "__main__":
    run()