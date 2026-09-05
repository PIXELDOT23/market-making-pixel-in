"""
config.py
---------
Fyers Market Making Bot Configuration.
Fill in YOUR OWN values or set via environment variables.

Get client_id / secret_key by creating an app at https://myapi.fyers.in/dashboard/
Make sure your app is ACTIVATED FOR API TRADING with a static IP registered
(mandatory under SEBI's algo trading framework for retail API users).
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a .env file in current directory if present
except ImportError:
    pass

# ---- Fyers App Credentials (from myapi.fyers.in/dashboard) ----
CLIENT_ID = os.getenv("FYERS_CLIENT_ID", "0B23JHNNLH-200")
SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "vprdMcFWr1ZHiQpc")
REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "http://localhost:2000/callback")

# Fail loudly and immediately if credentials are dummy values
if SECRET_KEY == "YOUR_SECRET_KEY" or CLIENT_ID == "YOUR_APP_ID-100":
    raise RuntimeError(
        "FYERS_CLIENT_ID / FYERS_SECRET_KEY are not set.\n"
        "Either set them in a .env file or export them in your shell."
    )

# ---- Token Cache File ----
TOKEN_FILE = "fyers_access_token.txt"

# ---- Segment Selection ("EQUITY" or "COMMODITY") ----
SEGMENT = os.getenv("SEGMENT", "COMMODITY")

# ---- Trading Universe ----
# Equity example: SYMBOL = "NSE:SBIN-EQ", LOT_SIZE = 1, TICK_SIZE = 0.05
# Commodity example: SYMBOL = "MCX:NATURALGAS26SEPFUT", LOT_SIZE = 1250, TICK_SIZE = 0.10
SYMBOL = os.getenv("SYMBOL", "MCX:NATURALGAS26SEPFUT")
PRODUCT_TYPE = os.getenv("PRODUCT_TYPE", "INTRADAY")  # INTRADAY, MARGIN, CNC (Equity only)

# ---- Sizing & Tick Parameters ----
LOT_SIZE = 1250 if SEGMENT == "COMMODITY" else 1      # contract multiplier
QUOTE_QTY = 1                                         # number of lots (commodity) or shares (equity) per quote
TICK_SIZE = 0.10 if SEGMENT == "COMMODITY" else 0.05  # minimum price increment
TICK_VALUE_RS = LOT_SIZE * TICK_SIZE                  # INR P&L per tick per quote unit

# ---- Market Making Quoting & Inventory Lifecycle ----
ENTRY_SIDE = "BOTH"           # "BOTH", "BUY", or "SELL" to initiate entry
SPREAD_TICKS = 4              # Desired total bid-to-ask spread in ticks
REQUOTE_INTERVAL_SEC = 2      # Heartbeat check interval in seconds
MAX_OPEN_ORDERS_PER_SIDE = 1  # Only 1 live bid + 1 live ask at a time

# Order Stability: do NOT cancel orders every loop if market price hasn't moved.
# An active order will only be requoted if market moves by at least REQUOTE_TOLERANCE_TICKS.
REQUOTE_TOLERANCE_TICKS = 2

# ---- Cost & Spread Protection ----
# Brokerage per executed order leg (standard Fyers: max ₹20/order or 0.03% for intraday/futures)
BROKERAGE_PER_ORDER = 20.0
AUTO_WIDEN_SPREAD = True      # Auto-widen SPREAD_TICKS to breakeven + profit margin
MIN_PROFIT_MARGIN_TICKS = 2   # Extra ticks above breakeven as target profit margin

# ---- After-Charge Profitability ----
# A quote is only placed when the FULL round trip (entry + exit legs) nets at least this
# much in INR AFTER all statutory charges. This is the floor that makes quoting profitable.
MIN_NET_PROFIT_PER_CYCLE_RS = 25.0

# ---- Pre-Trade Margin Validation (Fyers Margin API v3) ----
CHECK_MARGIN_BEFORE_ORDER = True      # Query Fyers /multiorder/margin before placing quotes
MIN_FREE_MARGIN_BUFFER_RS = 1000.0    # Cash buffer in INR to preserve as uncommitted cushion

# ---- Strict Inventory & Risk Management Limits ----
# Max 1 position at a time. Once 1 position is opened (e.g. BUY), the bot only quotes
# the opposite side (SELL) to close it. Next position is ONLY quoted after the current position is closed.
MAX_POSITION_QTY = 1          # Max net position (1 lot / unit at a time)
MAX_DAILY_LOSS_RS = 2500      # Kill-switch: square off everything & halt if daily loss is breached
MAX_ORDERS_PER_MINUTE = 15    # Throttling to prevent runaway orders

# ---- Volatility-Aware Market-Making Risk ----
# Risk is managed off the live market feed: when price is churning fast, standing quotes
# get picked off (adverse selection), so the bot widens the spread and eventually pauses
# NEW entry quotes while still managing/reducing any open inventory.
VOLATILITY_WINDOW_SEC = 30.0                     # Rolling window (sec) to measure price churn speed
VOLATILITY_WIDEN_FACTOR = 2                      # Extra spread ticks added per unit of churn (ticks/sec)
MAX_SPREAD_WIDEN_TICKS = 10                      # Absolute cap on spread (ticks) under volatility widening
VOLATILITY_HALT_QUOTING_TICKSPERSEC = 1.00       # Above this churn, stop placing NEW entry quotes

# ---- Position Lifecycle Risk ----
# Market-making carries inventory risk: a position that drifts against us or stays open too
# long is force-closed at market instead of being held indefinitely waiting for the limit exit.
MAX_POSITION_AGE_SEC = 300                       # Force market exit if a position stays open this long
MAX_LOSS_PER_POSITION_RS = 1500                  # Force market exit if unrealized loss exceeds this

# ---- Trading Hours ----
# Commodity (MCX): 09:05 to 23:00 (23:25 in US DST)
# Equity (NSE): 09:20 to 15:20
TRADING_START = "09:05" if SEGMENT == "COMMODITY" else "09:20"
TRADING_END = "23:00" if SEGMENT == "COMMODITY" else "15:20"
