"""
main.py
-------
WebSocket-driven, Cost-Aware & Margin-Aware Market Making Engine for FYERS.
Incorporates:
  - Real-time quote stream (data_ws)
  - Real-time order fill updates (order_ws)
  - Pre-trade Margin check via Fyers API v3 (/api/v3/multiorder/margin)
  - Accurate statutory charges & breakeven calculation
  - Drawdown monitoring & emergency flush
"""

import time
import logging
import webbrowser
import sys
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws, order_ws

import config
from cost_model import round_trip_cost, order_charges
from margin import get_order_margin, get_multiorder_margin
from auth import load_cached_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ==========================================
# 1. AUTHENTICATION & TOKEN RETRIEVAL
# ==========================================
def get_access_token(client_id: str, secret_key: str, redirect_uri: str) -> str:
    cached = load_cached_token()
    if cached:
        try:
            test_fyers = fyersModel.FyersModel(client_id=client_id, token=cached, is_async=False, log_path="")
            if test_fyers.get_profile().get("s") == "ok":
                logging.info("Using valid cached access token.")
                return cached
        except Exception:
            pass

    session = fyersModel.SessionModel(
        client_id=client_id,
        secret_key=secret_key,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )

    auth_url = session.generate_authcode()
    print("\n--------------------------------------------------")
    print("Opening browser for FYERS Authentication...")
    print(f"URL: {auth_url}")
    print("--------------------------------------------------\n")
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    redirected_url = input(
        "\nPaste the FULL redirected URL or auth_code: "
    ).strip()

    parsed_url = urlparse(redirected_url)
    auth_code = parse_qs(parsed_url.query).get('auth_code', [None])[0]
    if not auth_code:
        auth_code = redirected_url

    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") == "ok":
        token = response.get("access_token")
        with open(config.TOKEN_FILE, "w") as f:
            f.write(token)
        print("\n✓ Access Token successfully generated and cached!\n")
        return token
    else:
        raise RuntimeError(f"Authentication failed: {response}")


# ==========================================
# 2. COST-AWARE & MARGIN-AWARE MARKET MAKER
# ==========================================
class CostAwareMarketMaker:
    def __init__(
        self,
        client_id: str,
        access_token: str,
        symbol: str,
        qty: int,
        min_profit_margin: float,
        max_loss: float,
        segment: str = "COMMODITY",
        lot_size: int = 1,
        tick_size: float = 0.05
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.symbol = symbol
        self.qty = qty
        self.min_profit_margin = min_profit_margin  # Net profit target in INR per trade cycle
        self.max_daily_loss = abs(max_loss)
        self.segment = segment
        self.lot_size = lot_size
        self.tick_size = tick_size

        self.fyers = fyersModel.FyersModel(
            client_id=self.client_id,
            token=self.access_token,
            is_async=False,
            log_path=""
        )

        self.ltp = None
        self.active_bid_id = None
        self.active_ask_id = None
        self.current_bid_price = 0.0
        self.current_ask_price = 0.0
        self.inventory = 0
        self.is_circuit_broken = False

    def get_effective_half_spread(self, price: float) -> float:
        """
        Calculates the required half-spread based on total charges + target profit margin.
        """
        total_costs = round_trip_cost(
            price=price,
            qty=self.qty,
            product_type="INTRADAY",
            segment=self.segment,
            lot_size=self.lot_size
        )
        tick_value = self.tick_size * self.lot_size * self.qty
        required_full_spread_inr = total_costs + self.min_profit_margin
        required_price_move = required_full_spread_inr / (self.lot_size * self.qty)
        half_spread = required_price_move / 2.0
        return max(half_spread, self.tick_size)

    def flush_all_positions(self):
        logging.warning("🚨 FLUSHING POSITIONS & CANCELING QUOTES 🚨")
        if self.active_bid_id:
            self.cancel_order(self.active_bid_id)
            self.active_bid_id = None
        if self.active_ask_id:
            self.cancel_order(self.active_ask_id)
            self.active_ask_id = None

        try:
            res = self.fyers.exit_positions(data={})
            logging.info(f"Exit Positions API Response: {res}")
        except Exception as e:
            logging.error(f"Failed to execute exit_positions: {e}")

    def check_drawdown(self) -> bool:
        if self.is_circuit_broken:
            return True

        funds = self.fyers.funds()
        if funds.get("s") == "ok":
            fund_data = funds.get("fund_limit", [])
            realized_pnl = 0.0
            unrealized_pnl = 0.0

            for item in fund_data:
                if item.get("title") == "Realized Profit and Loss" or item.get("title") == "Realized PnL":
                    realized_pnl = float(item.get("equityAmount", 0.0)) + float(item.get("commodityAmount", 0.0))
                elif item.get("title") == "Unrealized PnL":
                    unrealized_pnl = float(item.get("equityAmount", 0.0)) + float(item.get("commodityAmount", 0.0))

            total_pnl = realized_pnl + unrealized_pnl
            if total_pnl <= -self.max_daily_loss:
                logging.error(f"⛔ MAX DRAWDOWN BREACHED! Net PnL ({total_pnl:.2f}) <= Loss Limit (-{self.max_daily_loss})")
                self.is_circuit_broken = True
                self.flush_all_positions()
                return True

        return False

    def start_order_socket(self):
        def on_order_update(message):
            if self.is_circuit_broken:
                return

            orders_data = message.get("orders", {})
            if not orders_data:
                return

            if orders_data.get("symbol") == self.symbol:
                status = orders_data.get("status")
                side = orders_data.get("side")
                order_id = str(orders_data.get("id", ""))

                # Status 2 = Traded / Filled
                if status == 2:
                    filled_qty = orders_data.get("filledQty", self.qty)
                    traded_price = orders_data.get("tradedPrice", orders_data.get("limitPrice", 0.0))

                    if side == 1:
                        self.inventory += 1
                        logging.info(f"⚡ BUY FILL DETECTED via WebSocket | Qty: {filled_qty} @ ₹{traded_price:.2f} | Inventory: {self.inventory}")
                        self.active_bid_id = None
                        self.current_bid_price = 0.0
                        # Cancel any pending opposite side order
                        if self.active_ask_id:
                            self.cancel_order(self.active_ask_id)
                            self.active_ask_id = None
                    elif side == -1:
                        self.inventory -= 1
                        logging.info(f"⚡ SELL FILL DETECTED via WebSocket | Qty: {filled_qty} @ ₹{traded_price:.2f} | Inventory: {self.inventory}")
                        self.active_ask_id = None
                        self.current_ask_price = 0.0
                        if self.active_bid_id:
                            self.cancel_order(self.active_bid_id)
                            self.active_bid_id = None

                    self.on_tick()

                # Status 1 = Canceled, 5 = Rejected
                elif status in (1, 5):
                    if order_id == self.active_bid_id:
                        self.active_bid_id = None
                        self.current_bid_price = 0.0
                    elif order_id == self.active_ask_id:
                        self.active_ask_id = None
                        self.current_ask_price = 0.0

        ows = order_ws.FyersOrderSocket(
            access_token=f"{self.client_id}:{self.access_token}",
            write_to_file=False,
            log_path="",
            on_orders=on_order_update
        )
        ows.connect()
        time.sleep(1)
        ows.subscribe(data_type="OnOrders,OnTrades,OnPositions")

    def start_data_socket(self):
        def on_message(message):
            if self.is_circuit_broken:
                return
            if message.get("type") == "sf" and "ltp" in message:
                self.ltp = float(message["ltp"])
                self.on_tick()

        dws = data_ws.FyersDataSocket(
            access_token=f"{self.client_id}:{self.access_token}",
            on_message=on_message
        )
        dws.connect()
        dws.subscribe(symbols=[self.symbol], data_type="SymbolUpdate")

    def place_limit_order(self, side: int, price: float) -> str | None:
        if self.is_circuit_broken:
            return None

        rounded_price = round(round(price / self.tick_size) * self.tick_size, 2)
        side_name = "BUY" if side == 1 else "SELL"

        # Pre-trade Margin Validation using Fyers API v3
        margin_check = get_order_margin(
            fyers=self.fyers,
            symbol=self.symbol,
            qty=self.qty,
            side=side,
            product_type="INTRADAY",
            limit_price=rounded_price,
            buffer_rs=config.MIN_FREE_MARGIN_BUFFER_RS
        )

        if not margin_check.is_sufficient:
            logging.warning(
                f"[Margin Check] Cannot place {side_name} {self.qty} @ {rounded_price:.2f} — "
                f"Required: ₹{margin_check.margin_required:,.2f} (+ buffer ₹{config.MIN_FREE_MARGIN_BUFFER_RS:,.2f}) "
                f"> Available: ₹{margin_check.margin_avail:,.2f}"
            )
            return None

        logging.info(
            f"[Margin Check] Passed for {side_name}: Req ₹{margin_check.margin_required:,.2f} | "
            f"Avail ₹{margin_check.margin_avail:,.2f}"
        )

        order_data = {
            "symbol": self.symbol,
            "qty": self.qty,
            "type": 1,  # Limit Order
            "side": side,  # 1 = Buy, -1 = Sell
            "productType": "INTRADAY",
            "limitPrice": rounded_price,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False
        }
        res = self.fyers.place_order(data=order_data)
        if res.get("s") == "ok":
            order_id = res.get("id")
            logging.info(f"Placed {side_name} {self.qty} {self.symbol} @ {rounded_price:.2f} | ID: {order_id}")
            return order_id
        else:
            logging.error(f"Order placement failed ({side_name}): {res}")
            return None

    def cancel_order(self, order_id: str):
        if order_id:
            self.fyers.cancel_order(data={"id": order_id})

    def sync_quotes(self, new_bid: float, new_ask: float):
        if self.is_circuit_broken:
            return

        tolerance = config.REQUOTE_TOLERANCE_TICKS * self.tick_size

        # Case 1: Neutral / Flat -> Quote Entry
        if self.inventory == 0:
            if not self.active_bid_id or abs(self.current_bid_price - new_bid) >= tolerance:
                if self.active_bid_id:
                    self.cancel_order(self.active_bid_id)
                self.active_bid_id = self.place_limit_order(side=1, price=new_bid)
                self.current_bid_price = new_bid if self.active_bid_id else 0.0

            if not self.active_ask_id or abs(self.current_ask_price - new_ask) >= tolerance:
                if self.active_ask_id:
                    self.cancel_order(self.active_ask_id)
                self.active_ask_id = self.place_limit_order(side=-1, price=new_ask)
                self.current_ask_price = new_ask if self.active_ask_id else 0.0

        # Case 2: Long Position -> ONLY Quote Exit SELL
        elif self.inventory > 0:
            if self.active_bid_id:
                self.cancel_order(self.active_bid_id)
                self.active_bid_id = None
                self.current_bid_price = 0.0

            if not self.active_ask_id or abs(self.current_ask_price - new_ask) >= tolerance:
                if self.active_ask_id:
                    self.cancel_order(self.active_ask_id)
                self.active_ask_id = self.place_limit_order(side=-1, price=new_ask)
                self.current_ask_price = new_ask if self.active_ask_id else 0.0

        # Case 3: Short Position -> ONLY Quote Cover BUY
        elif self.inventory < 0:
            if self.active_ask_id:
                self.cancel_order(self.active_ask_id)
                self.active_ask_id = None
                self.current_ask_price = 0.0

            if not self.active_bid_id or abs(self.current_bid_price - new_bid) >= tolerance:
                if self.active_bid_id:
                    self.cancel_order(self.active_bid_id)
                self.active_bid_id = self.place_limit_order(side=1, price=new_bid)
                self.current_bid_price = new_bid if self.active_bid_id else 0.0

    def on_tick(self):
        if not self.ltp or self.is_circuit_broken:
            return

        half_spread = self.get_effective_half_spread(self.ltp)
        target_bid = round(round((self.ltp - half_spread) / self.tick_size) * self.tick_size, 2)
        target_ask = round(round((self.ltp + half_spread) / self.tick_size) * self.tick_size, 2)

        self.sync_quotes(target_bid, target_ask)

    def run(self):
        logging.info("Starting Cost-Aware & Margin-Aware Market Maker Engine...")
        self.start_order_socket()
        time.sleep(1)
        self.start_data_socket()

        try:
            while True:
                if self.check_drawdown():
                    logging.error("Stop-loss circuit breaker triggered. Shutting down...")
                    sys.exit(0)
                time.sleep(5)
        except KeyboardInterrupt:
            logging.info("Keyboard Interrupt. Stopping bot...")
            self.flush_all_positions()


# ==========================================
# 3. EXECUTION
# ==========================================
if __name__ == "__main__":
    token = get_access_token(config.CLIENT_ID, config.SECRET_KEY, config.REDIRECT_URI)

    bot = CostAwareMarketMaker(
        client_id=config.CLIENT_ID,
        access_token=token,
        symbol=config.SYMBOL,
        qty=config.QUOTE_QTY,
        min_profit_margin=config.MIN_PROFIT_MARGIN_TICKS * config.TICK_VALUE_RS,
        max_loss=config.MAX_DAILY_LOSS_RS,
        segment=config.SEGMENT,
        lot_size=config.LOT_SIZE,
        tick_size=config.TICK_SIZE
    )
    bot.run()