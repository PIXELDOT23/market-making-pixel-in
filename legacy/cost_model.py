"""
cost_model.py
-------------
Accurate FYERS transaction charges, statutory levies, and breakeven model.
Supports both NSE Equity and MCX Commodity segments.

Statutory Levies & Charges (as per FYERS & SEBI 2026 schedule):
  - Brokerage: ₹20 or 0.03% per executed order (whichever is lower for Intraday/Futures)
  - Exchange Txn Fee: 0.00297% (NSE Equity) / 0.0026% (MCX Commodity)
  - STT: 0.025% on SELL (Equity Intraday) / 0.1% on BOTH (Equity Delivery)
  - CTT: 0.01% on SELL (MCX Commodity Futures)
  - Stamp Duty: 0.003% on BUY (Equity Intraday) / 0.015% on BUY (Equity Delivery) / 0.002% on BUY (MCX)
  - SEBI Charges: ₹10 per crore (0.000001 of turnover)
  - GST: 18% on (Brokerage + Exchange Txn + SEBI)

Also integrates with FYERS API v3 GET /charges-history endpoint to inspect
real broker-levied charges.
"""

from dataclasses import dataclass
import math
from typing import Dict, Any, List, Optional
import requests
import logger as log

# Rate constants
GST_RATE = 0.18
SEBI_RATE = 0.000001          # ₹10 per crore

# NSE Equity Rates
NSE_TXN_RATE = 0.0000297      # 0.00297%
STT_EQUITY_INTRADAY = 0.00025 # 0.025% (SELL only)
STT_EQUITY_DELIVERY = 0.001   # 0.100% (BOTH legs)
STAMP_EQUITY_INTRADAY = 0.00003 # 0.003% (BUY only)
STAMP_EQUITY_DELIVERY = 0.00015 # 0.015% (BUY only)

# MCX Commodity Rates
MCX_TXN_RATE = 0.000026       # 0.0026%
CTT_COMMODITY_RATE = 0.0001   # 0.01% (SELL only)
STAMP_COMMODITY_RATE = 0.00002# 0.002% (BUY only)


@dataclass
class ChargeBreakdown:
    turnover: float
    brokerage: float
    txn: float
    stt_or_ctt: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.txn + self.stt_or_ctt + self.sebi + self.stamp + self.gst, 2
        )


def order_charges(
    price: float,
    qty: int,
    side: str,
    product_type: str = "INTRADAY",
    segment: str = "COMMODITY",
    lot_size: int = 1,
    max_brokerage: float = 20.0
) -> ChargeBreakdown:
    """
    Calculate itemized charges for a single order leg.

    Args:
        price: Execution price.
        qty: Number of units / lots.
        side: 'BUY' or 'SELL'.
        product_type: 'INTRADAY', 'CNC', 'MARGIN', etc.
        segment: 'EQUITY' or 'COMMODITY'.
        lot_size: Contract multiplier (e.g. 1250 for Natural Gas, 1 for Equity).
        max_brokerage: Brokerage cap per order (₹20 default).
    """
    turnover = price * qty * lot_size
    side_u = side.upper()
    seg_u = segment.upper()
    prod_u = product_type.upper()

    # Fyers Brokerage: min(0.03%, ₹20) for intraday & futures; min(0.3%, ₹20) for delivery
    if prod_u == "DELIVERY" and seg_u == "EQUITY":
        brokerage = min(round(turnover * 0.003, 2), max_brokerage)
    else:
        brokerage = min(round(turnover * 0.0003, 2), max_brokerage)

    # Ensure minimum brokerage of 0 if turnover is 0
    brokerage = max(0.0, brokerage)

    if seg_u == "COMMODITY":
        txn = round(turnover * MCX_TXN_RATE, 2)
        stt_or_ctt = round(turnover * CTT_COMMODITY_RATE, 2) if side_u == "SELL" else 0.0
        stamp = round(turnover * STAMP_COMMODITY_RATE, 2) if side_u == "BUY" else 0.0
    else:  # EQUITY
        txn = round(turnover * NSE_TXN_RATE, 2)
        if prod_u == "DELIVERY":
            stt_or_ctt = round(turnover * STT_EQUITY_DELIVERY, 2)
            stamp = round(turnover * STAMP_EQUITY_DELIVERY, 2) if side_u == "BUY" else 0.0
        else:
            stt_or_ctt = round(turnover * STT_EQUITY_INTRADAY, 2) if side_u == "SELL" else 0.0
            stamp = round(turnover * STAMP_EQUITY_INTRADAY, 2) if side_u == "BUY" else 0.0

    sebi = round(turnover * SEBI_RATE, 2)
    gst = round((brokerage + txn + sebi) * GST_RATE, 2)

    return ChargeBreakdown(
        turnover=round(turnover, 2),
        brokerage=brokerage,
        txn=txn,
        stt_or_ctt=stt_or_ctt,
        sebi=sebi,
        stamp=stamp,
        gst=gst
    )


def round_trip_cost(
    price: float,
    qty: int,
    product_type: str = "INTRADAY",
    segment: str = "COMMODITY",
    lot_size: int = 1,
    max_brokerage: float = 20.0
) -> float:
    """Calculates total round-trip charges (BUY leg + SELL leg) in INR."""
    buy = order_charges(price, qty, "BUY", product_type, segment, lot_size, max_brokerage)
    sell = order_charges(price, qty, "SELL", product_type, segment, lot_size, max_brokerage)
    return round(buy.total + sell.total, 2)


def round_trip_net_profit(
    entry_price: float,
    exit_price: float,
    qty: int,
    side: int,
    product_type: str = "INTRADAY",
    segment: str = "COMMODITY",
    lot_size: int = 1,
    max_brokerage: float = 20.0
) -> float:
    """
    Net P&L (INR) AFTER all statutory charges for a full round-trip market-making cycle.

    side: 1  = BUY entry  -> SELL exit  (exit_price should be >= entry_price)
          -1 = SELL entry -> BUY cover  (exit_price should be <= entry_price)

    Each leg's charges are computed at that leg's actual execution price, unlike the
    single-price approximation used by round_trip_cost().
    """
    if side == 1:
        buy_price, sell_price = entry_price, exit_price
        gross = (exit_price - entry_price) * lot_size * qty
    else:
        buy_price, sell_price = exit_price, entry_price
        gross = (entry_price - exit_price) * lot_size * qty

    buy_total = order_charges(buy_price, qty, "BUY", product_type, segment, lot_size, max_brokerage).total
    sell_total = order_charges(sell_price, qty, "SELL", product_type, segment, lot_size, max_brokerage).total
    return round(gross - buy_total - sell_total, 2)


def breakeven_spread_ticks(
    price: float,
    qty: int,
    product_type: str = "INTRADAY",
    tick_size: float = 0.05,
    segment: str = "COMMODITY",
    lot_size: int = 1,
    max_brokerage: float = 20.0
) -> int:
    """
    Computes the minimum bid-to-ask spread (in ticks) required to cover round-trip charges.
    Value per tick = tick_size * lot_size * qty.
    Breakeven ticks = ceil(cost / value_per_tick).
    """
    cost = round_trip_cost(price, qty, product_type, segment, lot_size, max_brokerage)
    tick_value = tick_size * lot_size * qty
    if tick_value <= 0:
        return 1
    return max(1, math.ceil(cost / tick_value))


def fetch_fyers_charges_history(
    fyers,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Query FYERS API v3 GET /charges-history endpoint to inspect actual broker charges.

    Args:
        fyers: Authenticated FyersModel instance.
        from_date: Optional "YYYY-MM-DD" start date.
        to_date: Optional "YYYY-MM-DD" end date.

    Returns:
        List of charge records returned by Fyers.
    """
    params = {}
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date

    try:
        if hasattr(fyers, "service") and hasattr(fyers.service, "get_call"):
            resp = fyers.service.get_call("/charges-history", fyers.header, params)
        else:
            headers = {"Authorization": getattr(fyers, "header", "")}
            url = "https://api-t1.fyers.in/api/v3/charges-history"
            resp = requests.get(url, headers=headers, params=params, timeout=10).json()

        if resp.get("s") == "ok" and "data" in resp:
            return resp["data"]
        else:
            log.warn(f"Failed to fetch charges history from Fyers: {resp}")
            return []
    except Exception as e:
        log.error(f"Error calling /charges-history API: {e}")
        return []


if __name__ == "__main__":
    # Sanity checks
    print("=== Cost Model Sanity Check ===")
    eq_b = order_charges(800.0, 1000, "BUY", "INTRADAY", "EQUITY", lot_size=1)
    eq_s = order_charges(800.0, 1000, "SELL", "INTRADAY", "EQUITY", lot_size=1)
    print(f"Equity 10 qty SBIN @ ₹800 Intraday: BUY={eq_b.total} SELL={eq_s.total} RoundTrip={round(eq_b.total+eq_s.total, 2)}")
    print(f"Breakeven ticks (0.05 tick): {breakeven_spread_ticks(800.0, 10, 'INTRADAY', 0.05, 'EQUITY', 1)} ticks")

    mcx_b = order_charges(280.0, 1, "BUY", "INTRADAY", "COMMODITY", lot_size=1250)
    mcx_s = order_charges(280.0, 1, "SELL", "INTRADAY", "COMMODITY", lot_size=1250)
    print(f"\nMCX 1 lot NatGas @ ₹280 Intraday: BUY={mcx_b.total} SELL={mcx_s.total} RoundTrip={round(mcx_b.total+mcx_s.total, 2)}")
    print(f"Breakeven ticks (0.10 tick, ₹125/tick): {breakeven_spread_ticks(280.0, 1, 'INTRADAY', 0.10, 'COMMODITY', 1250)} ticks")