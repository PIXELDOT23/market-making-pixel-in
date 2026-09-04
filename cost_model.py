# """
# cost_model.py
# -------------
# Real Fyers/NSE equity charge formulas, calibrated against your own screenshots:
#
#   Delivery BUY  1 qty @ ~1665.6 -> Brokerage 5.00, Txn 0.05, STT 1.67,
#                                     GST 0.91, Stamp 0.25, Total 7.88
#   Delivery SELL 1 qty @ ~1665.6 -> Brokerage 5.00, Txn 0.05, STT 1.67,
#                                     GST 0.91, Stamp 0.00, Total 7.63
#
# Reverse-engineered rates (all match your screenshot to the paisa):
#   - Brokerage:        flat Rs 5 per executed order (delivery)
#   - Exchange txn chg:  0.00297% of turnover (NSE)
#   - STT (delivery):    0.1% of turnover, charged on BOTH buy and sell
#   - STT (intraday):    0.025% of turnover, charged ONLY on the sell leg
#   - GST:               18% of (brokerage + exchange txn charge)
#   - Stamp duty:         0.015% of turnover, BUY side only (delivery)
#                         0.003% of turnover, BUY side only (intraday)
#   - SEBI + NSE IPFT:   negligible at small qty, rounds to 0.00
#
# These are standard NSE/SEBI rates as of 2026; if Fyers changes its brokerage
# slab, update BROKERAGE_PER_ORDER below and everything else stays correct.
# """
#
# from dataclasses import dataclass
#
# BROKERAGE_PER_ORDER = 5.00          # flat, per executed order (matches screenshot)
# EXCHANGE_TXN_RATE = 0.0000297       # 0.00297%
# GST_RATE = 0.18                     # on brokerage + exchange txn charge
# STT_DELIVERY_RATE = 0.001           # 0.1%, both legs
# STT_INTRADAY_RATE = 0.00025         # 0.025%, sell leg only
# STAMP_DELIVERY_RATE = 0.00015       # 0.015%, buy leg only
# STAMP_INTRADAY_RATE = 0.00003       # 0.003%, buy leg only
#
#
# @dataclass
# class ChargeBreakdown:
#     brokerage: float
#     txn: float
#     stt: float
#     gst: float
#     stamp: float
#
#     @property
#     def total(self) -> float:
#         return round(
#             self.brokerage + self.txn + self.stt + self.gst + self.stamp, 2
#         )
#
#
# def order_charges(price: float, qty: int, side: str, product_type: str) -> ChargeBreakdown:
#     """
#     side: 'BUY' or 'SELL'
#     product_type: 'INTRADAY' or 'DELIVERY' (Fyers 'CNC')
#     """
#     turnover = price * qty
#     brokerage = BROKERAGE_PER_ORDER
#     txn = round(turnover * EXCHANGE_TXN_RATE, 2)
#     gst = round((brokerage + txn) * GST_RATE, 2)
#
#     if product_type.upper() == "DELIVERY":
#         stt = round(turnover * STT_DELIVERY_RATE, 2)          # both legs
#         stamp = round(turnover * STAMP_DELIVERY_RATE, 2) if side.upper() == "BUY" else 0.0
#     else:  # INTRADAY
#         stt = round(turnover * STT_INTRADAY_RATE, 2) if side.upper() == "SELL" else 0.0
#         stamp = round(turnover * STAMP_INTRADAY_RATE, 2) if side.upper() == "BUY" else 0.0
#
#     return ChargeBreakdown(brokerage=brokerage, txn=txn, stt=stt, gst=gst, stamp=stamp)
#
#
# def round_trip_cost(price: float, qty: int, product_type: str) -> float:
#     buy = order_charges(price, qty, "BUY", product_type)
#     sell = order_charges(price, qty, "SELL", product_type)
#     return round(buy.total + sell.total, 2)
#
#
# def breakeven_spread_ticks(price: float, qty: int, product_type: str, tick_size: float) -> int:
#     """
#     Minimum total (bid-to-ask) spread, in ticks, needed just to cover round-trip
#     charges if both legs fill at qty. Anything tighter than this guarantees a
#     loss on every completed round trip, before any market risk at all.
#     """
#     import math
#     cost = round_trip_cost(price, qty, product_type)
#     required_price_move = cost / qty
#     return math.ceil(required_price_move / tick_size)
#
#
# if __name__ == "__main__":
#     # sanity check against the screenshots
#     b = order_charges(1665.6, 1, "BUY", "DELIVERY")
#     s = order_charges(1665.6, 1, "SELL", "DELIVERY")
#     print("BUY :", b, "total=", b.total, "(screenshot: 7.88)")
#     print("SELL:", s, "total=", s.total, "(screenshot: 7.63)")


"""
cost_model.py
-------------
Real Fyers/NSE equity charge formulas, calibrated against your own screenshots:

  Delivery BUY  1 qty @ ~1665.6 -> Brokerage 5.00, Txn 0.05, STT 1.67,
                                    GST 0.91, Stamp 0.25, Total 7.88
  Delivery SELL 1 qty @ ~1665.6 -> Brokerage 5.00, Txn 0.05, STT 1.67,
                                    GST 0.91, Stamp 0.00, Total 7.63

Reverse-engineered rates (all match your screenshot to the paisa):
  - Brokerage:        flat Rs 5 per executed order (delivery)
  - Exchange txn chg:  0.00297% of turnover (NSE)
  - STT (delivery):    0.1% of turnover, charged on BOTH buy and sell
  - STT (intraday):    0.025% of turnover, charged ONLY on the sell leg
  - GST:               18% of (brokerage + exchange txn charge)
  - Stamp duty:         0.015% of turnover, BUY side only (delivery)
                        0.003% of turnover, BUY side only (intraday)
  - SEBI + NSE IPFT:   negligible at small qty, rounds to 0.00

These are standard NSE/SEBI rates as of 2026; if Fyers changes its brokerage
slab, update BROKERAGE_PER_ORDER below and everything else stays correct.
"""

from dataclasses import dataclass

BROKERAGE_PER_ORDER = 5.00          # flat, per executed order (matches screenshot)
EXCHANGE_TXN_RATE = 0.00027413       # 0.00297%
GST_RATE = 0.18                     # on brokerage + exchange txn charge
STT_DELIVERY_RATE = 0.001           # 0.1%, both legs
STT_INTRADAY_RATE = 0.00025         # 0.025%, sell leg only
STAMP_DELIVERY_RATE = 0.00015       # 0.015%, buy leg only
STAMP_INTRADAY_RATE = 0.00003       # 0.003%, buy leg only

# --- Commodity futures (MCX) rates ---
# NOTE: unlike the equity rates above, these are NOT yet calibrated against an
# actual Fyers commodity contract note — I don't have a screenshot for that
# segment. Rates below are the standard government/exchange-published figures
# as of 2026; verify against your own contract note after your first order and
# adjust BROKERAGE_PER_ORDER_COMMODITY / CTT_RATE below if they don't match.
BROKERAGE_PER_ORDER_COMMODITY = 20.00   # flat per order — CONFIRM against Fyers'
                                          # actual commodity brokerage slab
CTT_RATE = 0.0001                        # Commodity Transaction Tax: 0.01%,
                                          # non-agri commodities (incl. natural
                                          # gas), SELL leg only — mirrors how
                                          # equity intraday STT works
MCX_TXN_RATE = 0.000026                  # approx MCX exchange transaction charge
STAMP_COMMODITY_RATE = 0.00002           # 0.002%, BUY leg only


@dataclass
class ChargeBreakdown:
    brokerage: float
    txn: float
    stt: float
    gst: float
    stamp: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.txn + self.stt + self.gst + self.stamp, 2
        )


def order_charges(price: float, qty: int, side: str, product_type: str, segment: str = "COMMODITY") -> ChargeBreakdown:
    """
    side: 'BUY' or 'SELL'
    product_type: 'INTRADAY' or 'DELIVERY' (Fyers 'CNC') — equity only
    segment: 'EQUITY' or 'COMMODITY'
    """
    turnover = price * qty

    if segment.upper() == "COMMODITY":
        brokerage = BROKERAGE_PER_ORDER_COMMODITY
        txn = round(turnover * MCX_TXN_RATE, 2)
        gst = round((brokerage + txn) * GST_RATE, 2)
        ctt = round(turnover * CTT_RATE, 2) if side.upper() == "SELL" else 0.0
        stamp = round(turnover * STAMP_COMMODITY_RATE, 2) if side.upper() == "BUY" else 0.0
        return ChargeBreakdown(brokerage=brokerage, txn=txn, stt=ctt, gst=gst, stamp=stamp)

    brokerage = BROKERAGE_PER_ORDER_COMMODITY
    txn = round(turnover * EXCHANGE_TXN_RATE, 2)
    gst = round((brokerage + txn) * GST_RATE, 2)

    if product_type.upper() == "DELIVERY":
        stt = round(turnover * STT_DELIVERY_RATE, 2)          # both legs
        stamp = round(turnover * STAMP_DELIVERY_RATE, 2) if side.upper() == "BUY" else 0.0
    else:  # INTRADAY
        stt = round(turnover * STT_INTRADAY_RATE, 2) if side.upper() == "SELL" else 0.0
        stamp = round(turnover * STAMP_INTRADAY_RATE, 2) if side.upper() == "BUY" else 0.0

    return ChargeBreakdown(brokerage=brokerage, txn=txn, stt=stt, gst=gst, stamp=stamp)

print(order_charges(281.2, 1250, "BUY", "INTRADAY"))

def round_trip_cost(price: float, qty: int, product_type: str, segment: str = "EQUITY") -> float:
    buy = order_charges(price, qty, "BUY", product_type, segment)
    sell = order_charges(price, qty, "SELL", product_type, segment)
    return round(buy.total + sell.total, 2)


def breakeven_spread_ticks(price: float, qty: int, product_type: str, tick_size: float, segment: str = "EQUITY") -> int:
    """
    Minimum total (bid-to-ask) spread, in ticks, needed just to cover round-trip
    charges if both legs fill at qty. Anything tighter than this guarantees a
    loss on every completed round trip, before any market risk at all.
    """
    import math
    cost = round_trip_cost(price, qty, product_type, segment)
    required_price_move = cost / qty
    return math.ceil(required_price_move / tick_size)


if __name__ == "__main__":
    # sanity check against the equity screenshots
    b = order_charges(1665.6, 1, "BUY", "DELIVERY")
    s = order_charges(1665.6, 1, "SELL", "DELIVERY")
    print("EQUITY BUY :", b, "total=", b.total, "(screenshot: 7.88)")
    print("EQUITY SELL:", s, "total=", s.total, "(screenshot: 7.63)")

    # commodity example — 1 mini lot of natural gas around Rs 250/mmBtu (unverified rates, see note above)
    cb = order_charges(250, 250, "BUY", "INTRADAY", segment="COMMODITY")
    cs = order_charges(250, 250, "SELL", "INTRADAY", segment="COMMODITY")
    print("\nCOMMODITY BUY :", cb, "total=", cb.total)
    print("COMMODITY SELL:", cs, "total=", cs.total, "(rates unverified — confirm vs your contract note)")