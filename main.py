import time
import logging
import webbrowser
import sys
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws, order_ws

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ==========================================
# 1. TRANSACTION COST & BREAKEVEN CALCULATOR
# ==========================================
def calculate_roundtrip_cost(price: float, qty: int, is_intraday: bool = True) -> float:
    """
    Calculates total roundtrip costs (Buy + Sell) based on standard FYERS fee structures.
    Reflects STT/CTT, Exchange Txn Fees, GST, and Stamp Duty.
    """
    turnover = price * qty * 2  # Total buy + sell turnover

    if is_intraday:
        # Intraday Brokerage: min(0.03%, ₹20) per leg
        brokerage = min(0.0003 * turnover, 40.0)
        stt = 0.00025 * (price * qty)  # 0.025% on Sell side only
    else:
        # Delivery Brokerage: min(0.3%, ₹20) per leg
        brokerage = min(0.003 * turnover, 40.0)
        stt = 0.001 * turnover  # 0.1% on both Buy and Sell sides

    exchange_txn = 0.0000345 * turnover  # NSE Txn Charge (~0.00345%)
    sebi_charges = 0.000001 * turnover  # ₹10 per crore
    stamp_duty = 0.00003 * (price * qty)  # 0.003% on Buy side
    gst = 0.18 * (brokerage + exchange_txn + sebi_charges)  # 18% GST

    total_cost = brokerage + stt + exchange_txn + sebi_charges + stamp_duty + gst
    return round(total_cost, 2)


# ==========================================
# 2. AUTHENTICATION & TOKEN RETRIEVAL
# ==========================================
def get_access_token(client_id: str, secret_key: str, redirect_uri: str) -> str:
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
    webbrowser.open(auth_url)

    redirected_url = input(
        "\nPaste the FULL redirected URL (e.g., http://localhost:2000/callback?auth_code=...): ").strip()

    # Extract auth_code from redirected URL query string
    parsed_url = urlparse(redirected_url)
    auth_code = parse_qs(parsed_url.query).get('auth_code', [None])[0]

    if not auth_code:
        # Fallback if user pastes raw code instead of full URL
        auth_code = redirected_url

    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") == "ok":
        token = response.get("access_token")
        print("\n✓ Access Token successfully generated!\n")
        return token
    else:
        raise RuntimeError(f"Authentication failed: {response}")


# ==========================================
# 3. COST-AWARE MARKET MAKER BOT
# ==========================================
class CostAwareMarketMaker:
    def __init__(self, client_id: str, access_token: str, symbol: str, qty: int, min_profit_margin: float,
                 max_loss: float):
        self.client_id = client_id
        self.access_token = access_token
        self.symbol = symbol
        self.qty = qty
        self.min_profit_margin = min_profit_margin  # Desired net profit in INR per trade cycle
        self.max_daily_loss = abs(max_loss)

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
        total_costs = calculate_roundtrip_cost(price, self.qty, is_intraday=True)
        required_full_spread = (total_costs + self.min_profit_margin) / self.qty
        half_spread = required_full_spread / 2.0
        return max(half_spread, 0.10)  # Ensures spread covers costs

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
                if item.get("title") == "Realized PnL":
                    realized_pnl = float(item.get("equityAmount", 0.0))
                elif item.get("title") == "Unrealized PnL":
                    unrealized_pnl = float(item.get("equityAmount", 0.0))

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

            if orders_data.get("symbol") == self.symbol and orders_data.get("status") in [1, 6]:
                side = orders_data.get("side")
                filled_qty = orders_data.get("filledQty", 0)

                if side == 1:
                    self.inventory += filled_qty
                    logging.info(f"⚡ BUY FILL DETECTED | Qty: {filled_qty} | Inventory: {self.inventory}")
                    self.active_bid_id = None
                elif side == -1:
                    self.inventory -= filled_qty
                    logging.info(f"⚡ SELL FILL DETECTED | Qty: {filled_qty} | Inventory: {self.inventory}")
                    self.active_ask_id = None

                self.on_tick()

        ows = order_ws.FyersOrderSocket(
            access_token=f"{self.client_id}:{self.access_token}",
            on_orders=on_order_update
        )
        ows.connect()

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

    def place_limit_order(self, side: int, price: float) -> str:
        if self.is_circuit_broken:
            return None

        order_data = {
            "symbol": self.symbol,
            "qty": self.qty,
            "type": 1,  # Limit Order
            "side": side,  # 1 = Buy, -1 = Sell
            "productType": "INTRADAY",
            "limitPrice": round(price, 2),
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False
        }
        res = self.fyers.place_order(data=order_data)
        if res.get("s") == "ok":
            order_id = res.get("id")
            logging.info(f"Placed {'BUY' if side == 1 else 'SELL'} {self.qty} {self.symbol} @ {price:.2f} | ID: {order_id}")
            return order_id
        return None

    def cancel_order(self, order_id: str):
        if order_id:
            self.fyers.cancel_order(data={"id": order_id})

    def sync_quotes(self, new_bid: float, new_ask: float):
        if self.is_circuit_broken:
            return

        tick_size = 0.05
        if abs(self.current_bid_price - new_bid) >= tick_size:
            if self.active_bid_id:
                self.cancel_order(self.active_bid_id)
            self.active_bid_id = self.place_limit_order(side=1, price=new_bid)
            self.current_bid_price = new_bid

        if abs(self.current_ask_price - new_ask) >= tick_size:
            if self.active_ask_id:
                self.cancel_order(self.active_ask_id)
            self.active_ask_id = self.place_limit_order(side=-1, price=new_ask)
            self.current_ask_price = new_ask

    def on_tick(self):
        if not self.ltp or self.is_circuit_broken:
            return

        half_spread = self.get_effective_half_spread(self.ltp)
        inventory_skew = self.inventory * 0.05
        reservation_price = self.ltp - inventory_skew

        target_bid = round(reservation_price - half_spread, 2)
        target_ask = round(reservation_price + half_spread, 2)

        self.sync_quotes(target_bid, target_ask)

    def run(self):
        logging.info("Starting Cost-Aware Market Maker Engine...")
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
# 4. CONFIGURATION & EXECUTION
# ==========================================
if __name__ == "__main__":
    CLIENT_ID = "0B23JHNNLH-200"
    SECRET_KEY = "vprdMcFWr1ZHiQpc"
    REDIRECT_URI = "http://localhost:2000/callback"

    # Execution Setup
    SYMBOL = "NSE:HDFCBANK-EQ"  # Trading Paytm / One97 Communications
    QTY = 1  # 1 Quantity per leg
    MIN_PROFIT_PER_TRADE = 2.00  # Target profit after all statutory charges (in INR)
    MAX_DAILY_LOSS = 500.0  # Max daily drawdown threshold (in INR)

    # Step A: Authentication Flow
    token = get_access_token(CLIENT_ID, SECRET_KEY, REDIRECT_URI)

    # Step B: Start Bot Engine
    bot = CostAwareMarketMaker(
        client_id=CLIENT_ID,
        access_token=token,
        symbol=SYMBOL,
        qty=QTY,
        min_profit_margin=MIN_PROFIT_PER_TRADE,
        max_loss=MAX_DAILY_LOSS
    )
    bot.run()