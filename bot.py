"""
bot.py
------
High-Performance Market Making Bot for FYERS using Real-Time WebSockets:
  - Market Data Feed: FyersDataSocket (streaming tick-by-tick LTP, Bid, Ask, Depth)
  - Order Update Feed: FyersOrderSocket (real-time fills, executions, cancellations)
  - Decision Engine: Uses live WebSocket prices to calculate quotes & spreads
  - Strict Inventory Lifecycle:
      pos == 0 (Neutral) -> Quote Entry
      pos == 1 (Long)    -> Cancel all BUYs, ONLY quote EXIT SELL at target profit
      pos == 0 (Closed)  -> Acknowledge cycle complete, then quote next cycle
  - Order Stability: Resting orders remain in the orderbook unless market moves
    by at least REQUOTE_TOLERANCE_TICKS (prevents rapid cancels & preserves queue priority).
  - Pre-Trade Margin Gatekeeper: Fyers API v3 (/multiorder/margin).
  - Statutory Cost Model: Guarantees breakeven + profit margin over all fees & taxes.

Usage:
    python3 auth.py     # once per day to authenticate
    python3 bot.py      # run the market maker
"""

import time
import sys
import threading
from collections import deque
from typing import Optional, Dict, Any, Tuple, List

from fyers_apiv3.FyersWebsocket import data_ws, order_ws

import config
import logger as log
from auth import get_fyers_client, load_cached_token
from risk_manager import RiskManager
from cost_model import round_trip_cost, breakeven_spread_ticks, order_charges, round_trip_net_profit
from margin import get_order_margin, get_account_funds


def round_to_tick(price: float) -> float:
    return round(round(price / config.TICK_SIZE) * config.TICK_SIZE, 2)


# =============================================================================
# THREAD-SAFE REAL-TIME MARKET DATA STATE (from FyersDataSocket)
# =============================================================================
class MarketState:
    def __init__(self):
        self.lock = threading.Lock()
        self.ltp: Optional[float] = None
        self.bid: Optional[float] = None
        self.ask: Optional[float] = None
        self.bid_size: int = 0
        self.ask_size: int = 0
        self.last_tick_time: float = 0.0
        self.tick_count: int = 0
        self.is_connected: bool = False
        self.price_history: deque = deque(maxlen=10000)

    def update(
        self,
        ltp: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        bid_size: int = 0,
        ask_size: int = 0
    ):
        with self.lock:
            if ltp is not None and ltp > 0:
                self.ltp = float(ltp)
            if bid is not None and bid > 0:
                self.bid = float(bid)
            if ask is not None and ask > 0:
                self.ask = float(ask)
            if bid_size > 0:
                self.bid_size = int(bid_size)
            if ask_size > 0:
                self.ask_size = int(ask_size)
            self.last_tick_time = time.time()
            self.tick_count += 1
            if self.ltp:
                self.price_history.append((time.time(), self.ltp))

    def get_snapshot(self) -> Tuple[Optional[float], Optional[float], Optional[float], int]:
        """
        Returns: (mid_price, best_bid, best_ask, tick_count)
        """
        with self.lock:
            mid = None
            if self.bid and self.ask and self.bid > 0 and self.ask > 0:
                mid = (self.bid + self.ask) / 2.0
            elif self.ltp and self.ltp > 0:
                mid = self.ltp
            return mid, self.bid, self.ask, self.tick_count

    def get_speed_ticks_per_sec(self, window_sec: float = 30.0) -> float:
        """
        Realized price churn of the live WS feed in ticks/second over the rolling window.
        Used as a market-making risk input: fast churn => higher chance of being picked off,
        so the bot widens the spread and eventually pauses new entry quotes.
        """
        with self.lock:
            now = time.time()
            cutoff = now - window_sec
            while self.price_history and self.price_history[0][0] < cutoff - window_sec:
                self.price_history.popleft()
            pts = [p for p in self.price_history if p[0] >= cutoff]
            if len(pts) < 2:
                return 0.0
            dist = sum(abs(b - a) for (_, a), (_, b) in zip(pts, pts[1:]))
            elapsed = pts[-1][0] - pts[0][0]
            if elapsed <= 0 or dist <= 0:
                return 0.0
            return max(0.0, dist / elapsed / config.TICK_SIZE)


# =============================================================================
# THREAD-SAFE ORDER EVENT STATE (from FyersOrderSocket)
# =============================================================================
class OrderEventState:
    def __init__(self):
        self.lock = threading.Lock()
        self.fill_events: List[Dict[str, Any]] = []
        self.is_connected: bool = False

    def record_order_event(self, order_dict: Dict[str, Any]):
        with self.lock:
            if order_dict.get("symbol") == config.SYMBOL and order_dict.get("status") == 2:
                # status 2 = Traded / Filled
                self.fill_events.append(order_dict)

    def pop_fills(self) -> List[Dict[str, Any]]:
        with self.lock:
            fills = list(self.fill_events)
            self.fill_events.clear()
            return fills


# =============================================================================
# WEBSOCKET MANAGER
# =============================================================================
class WebSocketManager:
    def __init__(self, client_id: str, access_token: str, symbol: str, market_state: MarketState, order_state: OrderEventState):
        self.client_id = client_id
        self.access_token = access_token
        self.symbol = symbol
        self.market_state = market_state
        self.order_state = order_state
        self.full_token = f"{self.client_id}:{self.access_token}"

        self.dws: Optional[data_ws.FyersDataSocket] = None
        self.ows: Optional[order_ws.FyersOrderSocket] = None

    def start(self):
        log.info("Connecting to FYERS Real-Time WebSockets (Market Data & Orders)...")

        # 1. Market Data WebSocket Feed
        def on_data_message(msg):
            if msg.get("type") == "sf" and msg.get("symbol") == self.symbol:
                self.market_state.update(
                    ltp=msg.get("ltp"),
                    bid=msg.get("bid_price"),
                    ask=msg.get("ask_price"),
                    bid_size=msg.get("bid_size", 0),
                    ask_size=msg.get("ask_size", 0)
                )

        def on_data_connect():
            self.market_state.is_connected = True
            log.success("Market Data WebSocket Connected!")

        def on_data_error(err):
            log.warn(f"Market Data WebSocket error: {err}")

        self.dws = data_ws.FyersDataSocket(
            access_token=self.full_token,
            write_to_file=False,
            log_path="",
            litemode=False,
            reconnect=True,
            on_connect=on_data_connect,
            on_message=on_data_message,
            on_error=on_data_error
        )
        self.dws.connect()
        self.dws.subscribe(symbols=[self.symbol], data_type="SymbolUpdate")

        # 2. Order Update WebSocket Feed
        def on_order_update(msg):
            orders_data = msg.get("orders", {})
            if orders_data:
                self.order_state.record_order_event(orders_data)
                order_id = orders_data.get("id")
                status = orders_data.get("status")
                if status == 2:
                    side = "BUY" if orders_data.get("side") == 1 else "SELL"
                    qty = orders_data.get("filledQty", 0)
                    price = orders_data.get("tradedPrice", 0.0)
                    log.trade(f"⚡ [WS FILL] {side} {qty} @ ₹{price:.2f} | ID: {order_id}")

        def on_order_connect():
            self.order_state.is_connected = True
            log.success("Order Update WebSocket Connected!")

        def on_order_error(err):
            log.warn(f"Order WebSocket error: {err}")

        self.ows = order_ws.FyersOrderSocket(
            access_token=self.full_token,
            write_to_file=False,
            log_path="",
            reconnect=True,
            on_connect=on_order_connect,
            on_orders=on_order_update,
            on_error=on_order_error
        )
        self.ows.connect()
        time.sleep(1)
        self.ows.subscribe(data_type="OnOrders,OnTrades,OnPositions")

    def stop(self):
        log.info("Closing WebSocket connections...")
        if self.dws:
            try:
                self.dws.close_connection()
            except BaseException:
                pass
        if self.ows:
            try:
                self.ows.close_connection()
            except BaseException:
                pass


# =============================================================================
# PRICING & ORDER MANAGEMENT
# =============================================================================
def effective_spread_ticks(mid_price: float, speed_ticks_per_sec: float = 0.0) -> int:
    """
    Computes required spread in ticks based on live price, ensuring full
    statutory cost recovery plus configured net profit margin.

    With speed_ticks_per_sec > 0 the spread is additionally widened for market-making
    risk: fast price churn means resting quotes are more likely to be picked off, so the
    market maker demands a wider edge per cycle.
    """
    breakeven = breakeven_spread_ticks(
        price=mid_price,
        qty=config.QUOTE_QTY,
        product_type=config.PRODUCT_TYPE,
        tick_size=config.TICK_SIZE,
        segment=config.SEGMENT,
        lot_size=config.LOT_SIZE,
        max_brokerage=config.BROKERAGE_PER_ORDER
    )
    target = breakeven + config.MIN_PROFIT_MARGIN_TICKS
    if config.SPREAD_TICKS >= target:
        base = config.SPREAD_TICKS
    else:
        base = target if config.AUTO_WIDEN_SPREAD else config.SPREAD_TICKS

    if speed_ticks_per_sec > 0 and config.VOLATILITY_WIDEN_FACTOR > 0:
        widen = int(speed_ticks_per_sec * config.VOLATILITY_WIDEN_FACTOR)
        base = min(base + widen, config.MAX_SPREAD_WIDEN_TICKS)
    return max(1, base)


def profitable_quote_pair(mid_price: float, base_spread_ticks: int) -> Tuple[float, float, int, float]:
    """
    Rounds the target bid/ask around mid so the FULL spread (bid->ask round trip) nets at
    least MIN_NET_PROFIT_PER_CYCLE_RS AFTER all statutory charges.

    Widening stops at MAX_SPREAD_WIDEN_TICKS; if the floor still isn't met the caller
    receives net < floor and must NOT place the quote.

    Returns: (bid, ask, used_spread_ticks, net_profit_after_charges)
    """
    cap = max(base_spread_ticks, config.MAX_SPREAD_WIDEN_TICKS)
    spread = max(1, base_spread_ticks)
    bid = round_to_tick(mid_price - (spread * config.TICK_SIZE) / 2.0)
    ask = round_to_tick(mid_price + (spread * config.TICK_SIZE) / 2.0)
    net = 0.0
    while spread <= cap:
        half = (spread * config.TICK_SIZE) / 2.0
        bid = round_to_tick(mid_price - half)
        ask = round_to_tick(mid_price + half)
        net = round_trip_net_profit(
            bid, ask, config.QUOTE_QTY, 1,
            config.PRODUCT_TYPE, config.SEGMENT, config.LOT_SIZE, config.BROKERAGE_PER_ORDER
        )
        if net >= config.MIN_NET_PROFIT_PER_CYCLE_RS:
            break
        spread += 1
    return bid, ask, spread, net


def net_profit_exit_price(entry_price: float, side: int) -> Tuple[float, float]:
    """
    Minimum favourable exit price (tick-rounded) at which a full round trip nets at least
    MIN_NET_PROFIT_PER_CYCLE_RS AFTER charges.

    side: 1 = long (SELL exit above entry), -1 = short (BUY cover below entry).
    Returns: (exit_price, net_profit_after_charges_at_that_price)
    """
    step = (1 if side == 1 else -1) * config.TICK_SIZE
    price = round_to_tick(entry_price + step)
    for _ in range(200):
        net = round_trip_net_profit(
            entry_price, price, config.QUOTE_QTY, side,
            config.PRODUCT_TYPE, config.SEGMENT, config.LOT_SIZE, config.BROKERAGE_PER_ORDER
        )
        if net >= config.MIN_NET_PROFIT_PER_CYCLE_RS:
            return price, net
        price = round_to_tick(price + step)
    net = round_trip_net_profit(
        entry_price, price, config.QUOTE_QTY, side,
        config.PRODUCT_TYPE, config.SEGMENT, config.LOT_SIZE, config.BROKERAGE_PER_ORDER
    )
    return price, net


def position_risk_breached(net_lots: int, entry_price: float, mid: float, position_open_time: Optional[float]) -> bool:
    """
    Market-making inventory risk checks on an OPEN position:
      1. Per-position stop-loss: unrealized loss worse than MAX_LOSS_PER_POSITION_RS.
      2. Stale position: held longer than MAX_POSITION_AGE_SEC without the limit exit filling.
    Returns True when the position must be squared off at market.
    """
    if net_lots == 0 or entry_price <= 0 or mid <= 0:
        return False
    if net_lots > 0:
        unrealized = (mid - entry_price) * config.LOT_SIZE * net_lots
    else:
        unrealized = (entry_price - mid) * config.LOT_SIZE * abs(net_lots)

    if unrealized <= -config.MAX_LOSS_PER_POSITION_RS:
        log.risk(
            f"[risk] Per-position stop hit: unrealized ₹{unrealized:,.2f} <= "
            f"-₹{config.MAX_LOSS_PER_POSITION_RS:,.2f}. Forcing market exit."
        )
        return True
    if (config.MAX_POSITION_AGE_SEC > 0 and position_open_time
            and time.time() - position_open_time >= config.MAX_POSITION_AGE_SEC):
        log.risk(
            f"[risk] Position open {time.time() - position_open_time:.0f}s >= "
            f"{config.MAX_POSITION_AGE_SEC}s without exit fill. Forcing market exit."
        )
        return True
    return False


def get_open_orders_map(fyers) -> Dict[str, Dict[str, Any]]:
    resp = fyers.orderbook()
    if resp.get("s") != "ok":
        return {}
    return {
        str(o.get("id")): o
        for o in resp.get("orderBook", [])
        if o.get("status") == 6 and o.get("symbol") == config.SYMBOL
    }


def cancel_order(fyers, order_id: str) -> bool:
    resp = fyers.cancel_order({"id": order_id})
    if resp.get("s") != "ok":
        log.error(f"Cancel failed for order {order_id}: {resp}")
        return False
    log.info(f"Canceled order {order_id}")
    return True


def place_limit(fyers, side: int, price: float, risk: RiskManager) -> Optional[str]:
    """
    Validates pre-trade margin with Fyers API v3 and risk limits before placing order.
    """
    if not risk.can_place_order():
        return None

    rounded_price = round_to_tick(price)
    side_name = "BUY" if side == 1 else "SELL"

    # Pre-trade Margin Validation via Fyers API v3
    if getattr(config, "CHECK_MARGIN_BEFORE_ORDER", True):
        margin_res = get_order_margin(
            fyers=fyers,
            symbol=config.SYMBOL,
            qty=config.QUOTE_QTY,
            side=side,
            product_type=config.PRODUCT_TYPE,
            limit_price=rounded_price,
            buffer_rs=getattr(config, "MIN_FREE_MARGIN_BUFFER_RS", 0.0)
        )

        if not margin_res.is_sufficient or not risk.can_afford_margin(margin_res.margin_required, margin_res.margin_avail):
            log.warn(
                f"[margin] Insufficient margin for {side_name} {config.QUOTE_QTY} @ {rounded_price:.2f} — "
                f"Req ₹{margin_res.margin_required:,.2f} (+ buffer ₹{config.MIN_FREE_MARGIN_BUFFER_RS:,.2f}) "
                f"> Avail ₹{margin_res.margin_avail:,.2f}. Skipping quote."
            )
            return None
        else:
            log.info(
                f"[margin] Check passed for {side_name}: Req ₹{margin_res.margin_required:,.2f} | "
                f"Avail ₹{margin_res.margin_avail:,.2f}"
            )

    data = {
        "symbol": config.SYMBOL,
        "qty": config.QUOTE_QTY,
        "type": 1,  # limit order
        "side": side,  # 1 = buy, -1 = sell
        "productType": config.PRODUCT_TYPE,
        "limitPrice": rounded_price,
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
        log.error(f"Order placement failed ({side_name} @ {rounded_price:.2f}): {resp}")
        return None

    order_id = str(resp.get("id"))
    log.trade(f"Placed {side_name} {config.QUOTE_QTY} @ {rounded_price:.2f} | Order ID: {order_id}")
    return order_id


def square_off_all(fyers):
    """Emergency or clean shutdown: cancel all orders and flatten position at market."""
    log.warn("Squaring off all live orders and flattening positions...")
    open_orders = get_open_orders_map(fyers)
    for oid in open_orders:
        cancel_order(fyers, oid)

    resp = fyers.positions()
    if resp.get("s") != "ok":
        log.error(f"Could not fetch positions to square off: {resp}")
        return

    for pos in resp.get("netPositions", []):
        if pos.get("symbol") == config.SYMBOL and pos.get("netQty", 0) != 0:
            raw_qty = pos["netQty"]
            if config.SEGMENT == "COMMODITY" and abs(raw_qty) >= config.LOT_SIZE:
                order_qty = int(round(abs(raw_qty) / config.LOT_SIZE))
            else:
                order_qty = abs(raw_qty)

            side = -1 if raw_qty > 0 else 1
            side_name = "SELL" if side == -1 else "BUY"
            data = {
                "symbol": config.SYMBOL,
                "qty": order_qty,
                "type": 2,  # market order
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
            log.trade(f"Square-off market order ({side_name} {order_qty}): {resp2}")


def sync_broker_position(fyers, risk: RiskManager) -> Tuple[int, float, float]:
    """
    Sync ground truth net position and entry average from broker clearing.
    Returns: (net_lots, entry_price, realized_pnl)
    """
    resp = fyers.positions()
    if resp.get("s") != "ok":
        return risk.net_position, 0.0, risk.realized_pnl

    for pos in resp.get("netPositions", []):
        if pos.get("symbol") == config.SYMBOL:
            raw_qty = pos.get("netQty", 0)
            if config.SEGMENT == "COMMODITY" and abs(raw_qty) >= config.LOT_SIZE:
                net_lots = int(round(raw_qty / config.LOT_SIZE))
            else:
                net_lots = int(raw_qty)

            entry_price = float(pos.get("buyAvg", 0.0)) if net_lots > 0 else float(pos.get("sellAvg", 0.0))
            if entry_price == 0.0:
                entry_price = float(pos.get("netAvg", 0.0))

            realized_pnl = float(pos.get("realized_profit", risk.realized_pnl))
            risk.net_position = net_lots
            risk.realized_pnl = realized_pnl
            return net_lots, entry_price, realized_pnl

    risk.net_position = 0
    return 0, 0.0, risk.realized_pnl


# =============================================================================
# MAIN DECISION LOOP DRIVEN BY WEBSOCKET MARKET DATA
# =============================================================================
def run():
    fyers = get_fyers_client()
    risk = RiskManager()
    market_state = MarketState()
    order_state = OrderEventState()

    token = load_cached_token()
    if not token:
        raise RuntimeError("No cached token found. Run python3 auth.py first.")

    log.banner("Fyers Real-Time WebSocket Market Maker Bot")
    log.success(
        f"Connected. Symbol: {config.SYMBOL} ({config.SEGMENT}) | "
        f"Qty: {config.QUOTE_QTY} lot(s) | Max Position: {config.MAX_POSITION_QTY}"
    )

    # 1. Fetch Account Funds
    funds = get_account_funds(fyers)
    if funds:
        avail = funds.get("Available Balance", {}).get("total", 0.0)
        tot = funds.get("Total Balance", {}).get("total", 0.0)
        log.risk(f"Account Funds: Available ₹{avail:,.2f} | Total ₹{tot:,.2f}")

    # 2. Initial Seed Quote via REST (until WebSocket streams)
    init_quote = fyers.quotes({"symbols": config.SYMBOL})
    if init_quote.get("s") == "ok" and init_quote.get("d"):
        qv = init_quote["d"][0]["v"]
        market_state.update(
            ltp=qv.get("lp"),
            bid=qv.get("bid"),
            ask=qv.get("ask")
        )

    # 3. Start Real-Time WebSockets
    ws_mgr = WebSocketManager(
        client_id=config.CLIENT_ID,
        access_token=token,
        symbol=config.SYMBOL,
        market_state=market_state,
        order_state=order_state
    )
    ws_mgr.start()
    time.sleep(2)  # brief pause for socket handshakes

    # Order tracking state to avoid rapid cancels
    active_buy_id: Optional[str] = None
    active_buy_price: float = 0.0
    active_sell_id: Optional[str] = None
    active_sell_price: float = 0.0

    last_cycle_position = 0
    price_tolerance = config.REQUOTE_TOLERANCE_TICKS * config.TICK_SIZE
    position_open_time: Optional[float] = None
    force_off_sent_at: float = 0.0

    log.info("WebSocket Decision Engine started. Press Ctrl+C to stop safely (will cancel quotes & square off).")

    try:
        while True:
            if not RiskManager.within_trading_window():
                log.status("Outside trading window, waiting...")
                time.sleep(30)
                continue

            # Check if WebSocket pushed any instant order fills
            ws_fills = order_state.pop_fills()
            if ws_fills:
                log.trade(f"Processing {len(ws_fills)} instant WebSocket fill event(s)...")

            # Ground truth position sync
            net_lots, entry_price, realized_pnl = sync_broker_position(fyers, risk)

            # Cycle completion detection
            if last_cycle_position != 0 and net_lots == 0:
                log.success(
                    f"🎉 [CYCLE COMPLETED] Position closed! Net inventory = 0. "
                    f"Realized PnL = ₹{realized_pnl:,.2f}. Ready to quote next trade."
                )
                active_buy_id = None
                active_sell_id = None

            # Track when a new position is opened so lifecycle risk can act on it
            if last_cycle_position == 0 and net_lots != 0:
                position_open_time = time.time()
                log.risk(f"[risk] Inventory opened: {net_lots:+d} lot(s) @ ₹{entry_price:.2f}.")
            elif net_lots == 0:
                position_open_time = None
            last_cycle_position = net_lots

            if risk.halted:
                square_off_all(fyers)
                break

            # Pull real-time WebSocket market snapshot
            mid, best_bid, best_ask, tick_count = market_state.get_snapshot()
            if mid is None or mid <= 0:
                time.sleep(0.5)
                continue

            # Live market churn (ticks/sec) drives market-making risk
            speed = market_state.get_speed_ticks_per_sec(config.VOLATILITY_WINDOW_SEC)

            # Inventory risk: square off an open position that breached stop-loss / age limits
            if net_lots != 0 and position_risk_breached(net_lots, entry_price, mid, position_open_time):
                if time.time() - force_off_sent_at > 5.0:
                    square_off_all(fyers)
                    force_off_sent_at = time.time()
                continue

            # Pricing decisions driven directly by WebSocket market feed
            spread_ticks = effective_spread_ticks(mid)
            half_spread = (spread_ticks * config.TICK_SIZE) / 2.0

            # Inspect active open orders on broker
            open_orders = get_open_orders_map(fyers)

            if active_buy_id and active_buy_id not in open_orders:
                active_buy_id = None
                active_buy_price = 0.0

            if active_sell_id and active_sell_id not in open_orders:
                active_sell_id = None
                active_sell_price = 0.0

            # =================================================================
            # SCENARIO A: FLAT / NEUTRAL (net_lots == 0) -> QUOTE ENTRY
            # =================================================================
            if net_lots == 0:
                # After-charge profitable + volatility-aware quote pair around mid
                entry_spread = effective_spread_ticks(mid, speed)
                target_bid, target_ask, used_spread, quote_net = profitable_quote_pair(mid, entry_spread)

                vol_halted = (
                    config.VOLATILITY_HALT_QUOTING_TICKSPERSEC > 0
                    and speed >= config.VOLATILITY_HALT_QUOTING_TICKSPERSEC
                )
                quote_ok = quote_net >= config.MIN_NET_PROFIT_PER_CYCLE_RS and not vol_halted

                if vol_halted:
                    log.warn(f"[quote] Pausing entry quotes: churn {speed:.2f} t/s >= {config.VOLATILITY_HALT_QUOTING_TICKSPERSEC:.2f} t/s.")
                elif not quote_ok:
                    log.warn(
                        f"[quote] Skipping entry quotes: full cycle nets ₹{quote_net:.2f} < "
                        f"₹{config.MIN_NET_PROFIT_PER_CYCLE_RS:.2f} after charges (spread {used_spread} ticks)."
                    )

                if not quote_ok:
                    if active_buy_id:
                        cancel_order(fyers, active_buy_id)
                        active_buy_id = None
                    if active_sell_id:
                        cancel_order(fyers, active_sell_id)
                        active_sell_id = None
                else:
                    # Manage BUY entry quote
                    if config.ENTRY_SIDE in ("BOTH", "BUY") and risk.can_quote_buy():
                        if active_buy_id:
                            # Order is resting in book: check if live WS price moved >= tolerance
                            if abs(target_bid - active_buy_price) >= price_tolerance:
                                log.info(f"[WS Price Shift] Bid moved from {active_buy_price:.2f} to {target_bid:.2f}. Requoting BUY...")
                                cancel_order(fyers, active_buy_id)
                                active_buy_id = place_limit(fyers, 1, target_bid, risk)
                                active_buy_price = target_bid if active_buy_id else 0.0
                        else:
                            active_buy_id = place_limit(fyers, 1, target_bid, risk)
                            active_buy_price = target_bid if active_buy_id else 0.0
                    elif active_buy_id:
                        cancel_order(fyers, active_buy_id)
                        active_buy_id = None

                    # Manage SELL entry quote
                    if config.ENTRY_SIDE in ("BOTH", "SELL") and risk.can_quote_sell():
                        if active_sell_id:
                            if abs(target_ask - active_sell_price) >= price_tolerance:
                                log.info(f"[WS Price Shift] Ask moved from {active_sell_price:.2f} to {target_ask:.2f}. Requoting SELL...")
                                cancel_order(fyers, active_sell_id)
                                active_sell_id = place_limit(fyers, -1, target_ask, risk)
                                active_sell_price = target_ask if active_sell_id else 0.0
                        else:
                            active_sell_id = place_limit(fyers, -1, target_ask, risk)
                            active_sell_price = target_ask if active_sell_id else 0.0
                    elif active_sell_id:
                        cancel_order(fyers, active_sell_id)
                        active_sell_id = None

                log.status(
                    f"[WS FEED | Ticks: {tick_count} | churn: {speed:.2f} t/s] mid={mid:.2f} (bid={best_bid} ask={best_ask}) | "
                    f"Spread: {used_spread} tick(s) | Est Net P/L: ₹{quote_net:.2f} | "
                    f"Active BUY: {f'₹{active_buy_price:.2f}' if active_buy_id else 'None'} | "
                    f"Active SELL: {f'₹{active_sell_price:.2f}' if active_sell_id else 'None'} | "
                    f"{risk.status()}"
                )

            # =================================================================
            # SCENARIO B: HOLDING LONG (net_lots > 0) -> ONLY QUOTE EXIT SELL
            # =================================================================
            elif net_lots > 0:
                # Cancel pending BUY quotes immediately (stop buying more)
                if active_buy_id:
                    log.warn("Holding LONG: canceling active BUY quote to prevent accumulation.")
                    cancel_order(fyers, active_buy_id)
                    active_buy_id = None

                for oid, o in open_orders.items():
                    if o.get("side") == 1:
                        cancel_order(fyers, oid)

                # Target exit price anchored to AFTER-charge net profit floor
                min_exit_price, _ = net_profit_exit_price(entry_price, 1)
                market_ask = round_to_tick(mid + half_spread)
                target_exit_price = max(min_exit_price, market_ask)

                if active_sell_id:
                    if abs(target_exit_price - active_sell_price) >= price_tolerance:
                        log.info(f"[WS Price Shift] Adjusting exit SELL from {active_sell_price:.2f} to {target_exit_price:.2f}...")
                        cancel_order(fyers, active_sell_id)
                        active_sell_id = place_limit(fyers, -1, target_exit_price, risk)
                        active_sell_price = target_exit_price if active_sell_id else 0.0
                else:
                    active_sell_id = place_limit(fyers, -1, target_exit_price, risk)
                    active_sell_price = target_exit_price if active_sell_id else 0.0

                sell_px = active_sell_price if active_sell_price else target_exit_price
                profit_rs = round_trip_net_profit(
                    entry_price, sell_px, config.QUOTE_QTY, 1,
                    config.PRODUCT_TYPE, config.SEGMENT, config.LOT_SIZE, config.BROKERAGE_PER_ORDER
                )
                log.status(
                    f"[INVENTORY: LONG {net_lots}] Entry: ₹{entry_price:.2f} | WS Mid: ₹{mid:.2f} | "
                    f"Quoting EXIT SELL: ₹{sell_px:.2f} (Est Net P/L: ₹{profit_rs:.2f}) | "
                    f"Waiting for exit fill to close cycle."
                )

            # =================================================================
            # SCENARIO C: HOLDING SHORT (net_lots < 0) -> ONLY QUOTE COVER BUY
            # =================================================================
            elif net_lots < 0:
                # Cancel pending SELL quotes immediately (stop shorting more)
                if active_sell_id:
                    log.warn("Holding SHORT: canceling active SELL quote to prevent accumulation.")
                    cancel_order(fyers, active_sell_id)
                    active_sell_id = None

                for oid, o in open_orders.items():
                    if o.get("side") == -1:
                        cancel_order(fyers, oid)

                # Target cover price anchored to AFTER-charge net profit floor
                max_cover_price, _ = net_profit_exit_price(entry_price, -1)
                market_bid = round_to_tick(mid - half_spread)
                target_cover_price = min(max_cover_price, market_bid)

                if active_buy_id:
                    if abs(target_cover_price - active_buy_price) >= price_tolerance:
                        log.info(f"[WS Price Shift] Adjusting cover BUY from {active_buy_price:.2f} to {target_cover_price:.2f}...")
                        cancel_order(fyers, active_buy_id)
                        active_buy_id = place_limit(fyers, 1, target_cover_price, risk)
                        active_buy_price = target_cover_price if active_buy_id else 0.0
                else:
                    active_buy_id = place_limit(fyers, 1, target_cover_price, risk)
                    active_buy_price = target_cover_price if active_buy_id else 0.0

                buy_px = active_buy_price if active_buy_price else target_cover_price
                profit_rs = round_trip_net_profit(
                    entry_price, buy_px, config.QUOTE_QTY, -1,
                    config.PRODUCT_TYPE, config.SEGMENT, config.LOT_SIZE, config.BROKERAGE_PER_ORDER
                )
                log.status(
                    f"[INVENTORY: SHORT {abs(net_lots)}] Entry: ₹{entry_price:.2f} | WS Mid: ₹{mid:.2f} | "
                    f"Quoting COVER BUY: ₹{buy_px:.2f} (Est Net P/L: ₹{profit_rs:.2f}) | "
                    f"Waiting for cover fill to close cycle."
                )

            time.sleep(config.REQUOTE_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.warn("Stopping by user request...")
        try:
            ws_mgr.stop()
            square_off_all(fyers)
        except KeyboardInterrupt:
            log.warn("Second interrupt received; forcing exit...")
        finally:
            sys.exit(0)


if __name__ == "__main__":
    run()