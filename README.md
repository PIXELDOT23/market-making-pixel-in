# Fyers Equity Market Making Bot

A small, transparent market-making bot for a single NSE equity symbol.
You run this on your own machine with your own credentials — nothing here
connects back to this chat.

## Before you run anything

1. **Activate your Fyers app for API trading.** SEBI's algo trading rules require
   retail API apps to be explicitly activated with a registered static IP.
   Do this at https://myapi.fyers.in/dashboard/. Orders will simply be rejected
   until this is done.
2. **Understand the risk.** Market making means you're constantly quoting both
   a buy and a sell — you make money on the spread when both sides fill, but
   you can get run over in a fast-moving/trending market (you keep buying as
   price falls, or keep selling as it rises). The `MAX_POSITION_QTY` and
   `MAX_DAILY_LOSS_RS` limits in `config.py` exist specifically to cap that,
   but they don't eliminate it. Start with the smallest qty and tightest limits
   you're comfortable losing, on a low-volatility large-cap stock, before
   scaling anything up.
3. This bot uses **REST polling** every few seconds, not a websocket feed —
   simpler and more robust to get right first, but it means it reacts a
   few seconds slower than a tick-by-tick market maker. Fine for small-qty,
   wide-spread quoting; not fine if you plan to compete on very tight spreads.

## Setup

```bash
pip install fyers-apiv3
```

Edit `config.py`:
- `CLIENT_ID`, `SECRET_KEY`, `REDIRECT_URI` — from your Fyers API app
- `SYMBOL` — which equity to make markets in (default `NSE:SBIN-EQ`)
- `QUOTE_QTY`, `SPREAD_TICKS` — sizing and how wide you quote
- `MAX_POSITION_QTY`, `MAX_DAILY_LOSS_RS` — your hard risk limits

You can also set credentials via environment variables instead of editing
the file directly:
```bash
export FYERS_CLIENT_ID="ABC123-100"
export FYERS_SECRET_KEY="your_secret"
export FYERS_REDIRECT_URI="https://127.0.0.1"
```

## Run

```bash
python3 auth.py   # once per day — opens a login URL, you paste back auth_code
python3 bot.py     # starts quoting
```

Stop any time with `Ctrl+C` — it cancels open orders and flattens your
position before exiting. It also auto-halts and flattens if the daily loss
limit is breached.

## Files

- `config.py` — all your settings and credentials (keep this out of git)
- `auth.py` — one-time daily login handshake, caches the access token
- `risk_manager.py` — position limits, loss limits, order throttling
- `bot.py` — the quoting loop itself

## What this does NOT do

- No backtesting included — you're quoting live with real capital from the
  first run. Consider paper-testing the logic against logged quotes first,
  or running with `QUOTE_QTY=1` on a highly liquid stock initially.
- No adverse-selection / volatility-based spread widening — spread is fixed
  in ticks. A common next upgrade is widening the spread when recent price
  volatility increases.
- No multi-symbol support — one symbol at a time by design, to keep risk
  contained while you test it.
