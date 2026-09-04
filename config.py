# """
# config.py
# ---------
# Fill in YOUR OWN values here. This file never leaves your machine.
# Get client_id / secret_key by creating an app at https://myapi.fyers.in/dashboard/
# Make sure your app is ACTIVATED FOR API TRADING with a static IP registered
# (mandatory under SEBI's algo trading framework for retail API users).
# """
#
# import os
#
# try:
#     from dotenv import load_dotenv
#     load_dotenv()  # loads a .env file in the current directory, if present
# except ImportError:
#     pass  # fine if not installed — you can still export env vars manually
#
# # ---- Fyers app credentials (from myapi.fyers.in/dashboard) ----
# CLIENT_ID = os.getenv("FYERS_CLIENT_ID", "0B23JHNNLH-200")     # e.g. "ABC123-100"
# SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "vprdMcFWr1ZHiQpc")
# REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "http://localhost:2000/callback")
#
# # Fail loudly and immediately if credentials were never actually set, instead of
# # letting a placeholder silently reach the Fyers API and produce a confusing
# # "invalid app id hash" error.
# if SECRET_KEY == "YOUR_SECRET_KEY" or CLIENT_ID == "YOUR_APP_ID-100":
#     raise RuntimeError(
#         "FYERS_CLIENT_ID / FYERS_SECRET_KEY are not set.\n"
#         "Either:\n"
#         "  1. Create a .env file next to this script with:\n"
#         "       FYERS_CLIENT_ID=your_app_id-XXX\n"
#         "       FYERS_SECRET_KEY=your_secret_key\n"
#         "       FYERS_REDIRECT_URI=your_redirect_uri\n"
#         "  2. Or export them in the SAME shell before running:\n"
#         "       export FYERS_CLIENT_ID=your_app_id-XXX\n"
#         "       export FYERS_SECRET_KEY=your_secret_key\n"
#         "  (pip install python-dotenv if using a .env file)"
#     )
#
# # ---- Where the access token gets cached after login (gitignore this file!) ----
# TOKEN_FILE = "fyers_access_token.txt"
#
# # ---- Trading universe ----
# SYMBOL = "NSE:SBIN-EQ"        # single equity to market-make, change as needed
# PRODUCT_TYPE = "INTRADAY"     # INTRADAY = MIS. DELIVERY(CNC) is NOT recommended for
#                                # market making — STT hits both legs and brokerage/
#                                # STT/stamp duty are all higher, see cost_model.py
#
# # ---- Market making parameters ----
# QUOTE_QTY = 1                 # small qty per side, as requested
# SPREAD_TICKS = 4              # DESIRED total bid-to-ask spread, in ticks (split evenly
#                                # each side of mid) — but see AUTO_WIDEN_SPREAD below.
#                                # At small qty, flat brokerage (Rs 5/order) usually
#                                # makes this too tight to be profitable; the bot
#                                # computes the real breakeven spread from cost_model.py
#                                # and will not quote tighter than that.
# TICK_SIZE = 0.10              # NSE equity tick size
# REQUOTE_INTERVAL_SEC = 3      # min seconds between requotes (avoid over-trading/spam)
# MAX_OPEN_ORDERS_PER_SIDE = 1  # only 1 live bid + 1 live ask at a time
#
# # ---- Cost-awareness ----
# AUTO_WIDEN_SPREAD = True      # if True, bot widens SPREAD_TICKS up to the breakeven
#                                # spread (from cost_model.py) instead of quoting at a
#                                # guaranteed loss. If False, bot warns but still quotes
#                                # your configured (possibly loss-making) SPREAD_TICKS.
# MIN_PROFIT_MARGIN_TICKS = 2   # extra ticks ABOVE breakeven you want as actual profit
#                                # margin per round trip, not just breakeven
#
# # ---- Risk management (hard limits — bot will flatten & stop if breached) ----
# MAX_POSITION_QTY = 5          # max net inventory (either direction) before it stops quoting that side
# MAX_DAILY_LOSS_RS = 500       # kill-switch: square off everything & halt for the day
# MAX_ORDERS_PER_MINUTE = 10    # throttle to avoid runaway order loops
# TRADING_START = "09:20"       # avoid the volatile first minutes of the session
# TRADING_END = "15:30"         # stop quoting before close, leave time to flatten

"""
config.py
---------
Fill in YOUR OWN values here. This file never leaves your machine.
Get client_id / secret_key by creating an app at https://myapi.fyers.in/dashboard/
Make sure your app is ACTIVATED FOR API TRADING with a static IP registered
(mandatory under SEBI's algo trading framework for retail API users).
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads a .env file in the current directory, if present
except ImportError:
    pass  # fine if not installed — you can still export env vars manually

# ---- Fyers app credentials (from myapi.fyers.in/dashboard) ----
CLIENT_ID = os.getenv("FYERS_CLIENT_ID", "0B23JHNNLH-200")     # e.g. "ABC123-100"
SECRET_KEY = os.getenv("FYERS_SECRET_KEY", "vprdMcFWr1ZHiQpc")
REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "http://localhost:2000/callback")

# Fail loudly and immediately if credentials were never actually set, instead of
# letting a placeholder silently reach the Fyers API and produce a confusing
# "invalid app id hash" error.
if SECRET_KEY == "YOUR_SECRET_KEY" or CLIENT_ID == "YOUR_APP_ID-100":
    raise RuntimeError(
        "FYERS_CLIENT_ID / FYERS_SECRET_KEY are not set.\n"
        "Either:\n"
        "  1. Create a .env file next to this script with:\n"
        "       FYERS_CLIENT_ID=your_app_id-XXX\n"
        "       FYERS_SECRET_KEY=your_secret_key\n"
        "       FYERS_REDIRECT_URI=your_redirect_uri\n"
        "  2. Or export them in the SAME shell before running:\n"
        "       export FYERS_CLIENT_ID=your_app_id-XXX\n"
        "       export FYERS_SECRET_KEY=your_secret_key\n"
        "  (pip install python-dotenv if using a .env file)"
    )

# ---- Where the access token gets cached after login (gitignore this file!) ----
TOKEN_FILE = "fyers_access_token.txt"

# ---- Segment selection ----
SEGMENT = "COMMODITY"         # "EQUITY" or "COMMODITY" — changes the cost model
                               # (STT vs CTT) used by cost_model.py

# ---- Trading universe ----
# MCX futures symbols encode the contract month and roll over monthly — pull the
# exact current string from the Fyers app (search "NATURALGAS") or symbol master,
# don't hardcode a guess. Shape looks like: "MCX:NATURALGAS26SEPFUT"
SYMBOL = "MCX:NATURALGAS26SEPFUT"   # <-- REPLACE with the current active contract
PRODUCT_TYPE = "INTRADAY"     # commodity MIS-equivalent on Fyers

# ---- Lot-based sizing (commodity futures trade in lots, not share count) ----
LOT_SIZE = 1250                # MCX Natural Gas MINI = 250 mmBtu/lot (main = 1250)
NUM_LOTS = 1                  # how many lots per quote — start at 1
QUOTE_QTY = NUM_LOTS   # do not set this directly for commodities — it
                               # MUST be an exact multiple of LOT_SIZE or the order
                               # gets rejected. Change NUM_LOTS instead.
TICK_VALUE_RS = LOT_SIZE * 0.10   # Rs P&L per tick per lot (Rs 25 for 1 mini lot) —
                               # use this to size MAX_DAILY_LOSS_RS realistically:
                               # a 20-tick (Rs 2) move = Rs 500 P&L on just 1 lot.

# ---- Market making parameters ----
SPREAD_TICKS = 4              # DESIRED total bid-to-ask spread, in ticks (split evenly
                               # each side of mid) — but see AUTO_WIDEN_SPREAD below.
TICK_SIZE = 0.10              # MCX Natural Gas tick size = Rs 0.10 (main & mini alike).
                               # For NSE equities this varies by stock instead —
                               # verify per-instrument if you switch segments back.
REQUOTE_INTERVAL_SEC = 3      # min seconds between requotes (avoid over-trading/spam)
MAX_OPEN_ORDERS_PER_SIDE = 1  # only 1 live bid + 1 live ask at a time

# ---- Cost-awareness ----
AUTO_WIDEN_SPREAD = True      # if True, bot widens SPREAD_TICKS up to the breakeven
                               # spread (from cost_model.py) instead of quoting at a
                               # guaranteed loss. If False, bot warns but still quotes
                               # your configured (possibly loss-making) SPREAD_TICKS.
MIN_PROFIT_MARGIN_TICKS = 2   # extra ticks ABOVE breakeven you want as actual profit
                               # margin per round trip, not just breakeven

# ---- Risk management (hard limits — bot will flatten & stop if breached) ----
MAX_POSITION_QTY = LOT_SIZE * 2   # cap net inventory at 2 lots either direction —
                               # commodity futures move fast, keep this small until
                               # you've watched the bot run for a while first.
MAX_DAILY_LOSS_RS = 2500      # YOU must set this deliberately for commodities — at
                               # Rs 25/tick/lot, natural gas can move 20-40+ ticks in
                               # minutes on a news print. The equity default of
                               # Rs 500 would trip almost instantly here.
MAX_ORDERS_PER_MINUTE = 10    # throttle to avoid runaway order loops

# ---- Trading window ----
# MCX commodity session runs 9:00 AM to 11:30 PM (11:55 PM during US daylight
# saving) — NOT the NSE equity window (9:15-15:30). Using the equity window here
# would leave the bot idle for most of the actual commodity session.
TRADING_START = "09:05"       # a few minutes after open, avoid the opening print
TRADING_END = "23:00"         # stop quoting before close, leave time to flatten.
                               # Move to "23:25" during US DST months if you want
                               # to use the extra time MCX allows then.

python3 -c "
# Reverse-engineer exact commodity rates from the new screenshots
price = 381.2
# try lot=1250 (main contract, not mini=250) since screenshot says 'NATURALGAS Fut' not 'NATURALGASMINI'
for lot in [1250]:
    turnover = 49392.18
    print(f'lot={lot} turnover={turnover:.2f}')
    print(f'  implied txn rate if txn=13.70: {13.54/turnover:.8f}')
    print(f'  implied SEBI rate if sebi=0.35: {0.35/turnover:.8f}')
    print(f'  implied stamp rate if stamp=7.02: {6.94/turnover:.8f}')
    print(f'  implied CTT rate if ctt=35.71: {34.72/turnover:.8f}')
    print(f'  implied GST rate if GST=6.13: {6.13/turnover:.8f}')
"
