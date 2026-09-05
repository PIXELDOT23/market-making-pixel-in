# FYERS Market Making Bot (Cost-Aware & Margin-Aware)

A transparent, high-performance market-making bot for NSE Equities and MCX Commodities on the **FYERS API v3**.

Runs locally on your machine with your own credentials.

---

## Key Features

1. **Pre-Trade Margin Check via FYERS API v3:**
   - Queries the official Multiorder Margin Calculator endpoint (`POST /api/v3/multiorder/margin`) before placing quotes.
   - Verifies `margin_avail >= margin_required + MIN_FREE_MARGIN_BUFFER_RS` for both single and simultaneous two-sided quotes.
   - Prevents order rejections due to insufficient margin or account over-leverage.

2. **Accurate Transaction Cost & Breakeven Modeling:**
   - Incorporates full statutory levies: Brokerage (₹20 or 0.03%), Exchange Transaction Charges (NSE 0.00297%, MCX 0.0026%), STT/CTT, SEBI fees (₹10/crore), Stamp Duty, and 18% GST.
   - Auto-widens quote spread (`AUTO_WIDEN_SPREAD=True`) to clear breakeven + minimum profit margin (`MIN_PROFIT_MARGIN_TICKS`).
   - Supports live inspection of historical broker charges via FYERS API v3 `GET /charges-history`.

3. **Multi-Segment Support:**
   - Supports both **NSE Equities** (`SEGMENT="EQUITY"`) and **MCX Commodities** (`SEGMENT="COMMODITY"`).
   - Properly accounts for contract lot multipliers (e.g. 1250 for Natural Gas futures) in turnover, charges, and PnL calculations.

4. **Multi-Mode Execution:**
   - `bot.py`: Robust polling-based market maker (requotes every N seconds, cancels and replaces).
   - `main.py`: Real-time WebSocket engine (`data_ws` for tick-by-tick prices and `order_ws` for instant fill callbacks).

5. **Strict Risk Controls:**
   - Max inventory caps (`MAX_POSITION_QTY`).
   - Daily loss kill-switch (`MAX_DAILY_LOSS_RS`) with automatic market square-off.
   - Order throttling (`MAX_ORDERS_PER_MINUTE`).
   - Safe shutdown on `Ctrl+C` (cancels live quotes and flattens inventory).

---

## Setup

```bash
pip install fyers-apiv3 requests python-dotenv
```

Configure `config.py` or export environment variables:
```bash
export FYERS_CLIENT_ID="your_app_id-XXX"
export FYERS_SECRET_KEY="your_secret_key"
export FYERS_REDIRECT_URI="http://localhost:2000/callback"
```

## Running the Bot

1. **Daily Authentication (once per day):**
   ```bash
   python3 auth.py
   ```
   Opens FYERS login in your browser, authenticates, and caches your access token to `fyers_access_token.txt`.

2. **Start the Polling Market Maker:**
   ```bash
   python3 bot.py
   ```

3. **Or Start the WebSocket Engine:**
   ```bash
   python3 main.py
   ```

---

## File Overview

- [`config.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/config.py): Configuration, credentials, risk parameters, and margin buffer settings.
- [`margin.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/margin.py): FYERS API v3 Margin Calculator integration (`/multiorder/margin`) and account funds inspection.
- [`cost_model.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/cost_model.py): Accurate statutory fees, breakeven tick calculators, and historical charges fetcher (`/charges-history`).
- [`risk_manager.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/risk_manager.py): Inventory caps, daily drawdown circuit breaker, order throttle, and margin limits.
- [`bot.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/bot.py): Main polling market maker quoting loop.
- [`main.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/main.py): WebSocket-driven market maker engine.
- [`auth.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/auth.py): OAuth2 login handshake and token caching.
- [`logger.py`](file:///home/pushpanathan/Pixel-In-Infa/market-making-pixel-in/logger.py): ANSI color terminal logger.
