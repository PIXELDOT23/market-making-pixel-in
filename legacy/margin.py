"""
margin.py
---------
Fyers API v3 Margin Calculator Integration.
Endpoint: POST /api/v3/multiorder/margin

Provides pre-trade margin calculations and validations:
  - Single order margin requirement
  - Multiorder (simultaneous two-sided quotes) margin requirement
  - Available account balance & margin cushion verification
  - Prevents order rejection due to insufficient funds
"""

from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import requests
import logger as log
import config

FYERS_API_BASE = "https://api-t1.fyers.in/api/v3"
MARGIN_ENDPOINT = "/multiorder/margin"


@dataclass
class MarginRequirement:
    symbol: str
    qty: int
    side: int                   # 1 = BUY, -1 = SELL
    limit_price: float
    margin_avail: float         # Available margin in the trading account
    margin_required: float      # Margin required for this new order
    margin_total: float         # Total margin required including existing positions
    is_sufficient: bool         # Whether available margin covers requirement + buffer
    buffer_rs: float            # Safety buffer configured
    code: int = 200
    message: str = ""

    @property
    def side_str(self) -> str:
        return "BUY" if self.side == 1 else "SELL"


@dataclass
class MultiOrderMarginRequirement:
    order_count: int
    margin_avail: float
    margin_required: float
    margin_total: float
    is_sufficient: bool
    buffer_rs: float
    code: int = 200
    message: str = ""


def _call_margin_api(fyers, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Executes the POST request to the FYERS /multiorder/margin endpoint.
    Uses fyers.service.post_call if available, with a fallback to direct requests.
    """
    try:
        if hasattr(fyers, "service") and hasattr(fyers.service, "post_call"):
            return fyers.service.post_call(MARGIN_ENDPOINT, fyers.header, payload)
    except Exception as e:
        log.warn(f"fyers.service.post_call failed ({e}), falling back to direct HTTP...")

    # Fallback to direct requests if service is unavailable
    headers = {
        "Authorization": getattr(fyers, "header", ""),
        "Content-Type": "application/json"
    }
    url = f"{FYERS_API_BASE}{MARGIN_ENDPOINT}"
    response = requests.post(url, headers=headers, json=payload, timeout=10)
    return response.json()


def get_order_margin(
    fyers,
    symbol: str,
    qty: int,
    side: int,
    product_type: str,
    limit_price: float,
    buffer_rs: float = 0.0,
    order_type: int = 1,
    stop_loss: float = 0.0,
    stop_price: float = 0.0,
    take_profit: float = 0.0
) -> MarginRequirement:
    """
    Query FYERS API v3 to calculate required margin for a single order.

    Args:
        fyers: Authenticated FyersModel instance.
        symbol: Instrument symbol (e.g. 'MCX:NATURALGAS26SEPFUT' or 'NSE:SBIN-EQ').
        qty: Order quantity (number of lots for derivatives, or shares for equities).
        side: 1 for Buy, -1 for Sell.
        product_type: 'INTRADAY', 'CNC', 'MARGIN', etc.
        limit_price: Order limit price.
        buffer_rs: Safety buffer in INR to preserve as uncommitted cushion.

    Returns:
        MarginRequirement instance with available vs required margin.
    """
    payload = {
        "data": [
            {
                "symbol": symbol,
                # NFO equity futures trade in underlying SHARES - convert the
                # lot quantity so the margin matches a real lot, not 1 share
                # (a 1-share quote would report a tiny fraction of the margin).
                "qty": config.broker_qty(qty, symbol),
                "side": side,
                "type": order_type,
                "productType": product_type,
                "limitPrice": round(float(limit_price), 2),
                "stopLoss": float(stop_loss),
                "stopPrice": float(stop_price),
                "takeProfit": float(take_profit),
            }
        ]
    }

    try:
        resp = _call_margin_api(fyers, payload)
        code = resp.get("code", 0)
        s = resp.get("s", "")
        msg = resp.get("message", "")

        if s == "ok" and "data" in resp:
            data = resp["data"]
            avail = float(data.get("margin_avail", 0.0))
            # Use the marginal margin of THIS order. `margin_new_order` is the
            # projected account margin after the order (parked margin + new
            # order), so it inflates the requirement for any account holding
            # unrelated positions. `margin_total` scales linearly with qty.
            new_ord = float(data.get("margin_total", 0.0))
            total = float(data.get("margin_total", new_ord))
            sufficient = avail >= (new_ord + buffer_rs)

            return MarginRequirement(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=limit_price,
                margin_avail=avail,
                margin_required=new_ord,
                margin_total=total,
                is_sufficient=sufficient,
                buffer_rs=buffer_rs,
                code=code,
                message=msg
            )
        else:
            log.error(f"Margin API error: code={code} message='{msg}' payload={payload}")
            return MarginRequirement(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=limit_price,
                margin_avail=0.0,
                margin_required=float("inf"),
                margin_total=float("inf"),
                is_sufficient=False,
                buffer_rs=buffer_rs,
                code=code,
                message=msg or "Failed to calculate margin"
            )
    except Exception as e:
        log.error(f"Exception calling Fyers Margin API: {e}")
        return MarginRequirement(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=limit_price,
            margin_avail=0.0,
            margin_required=float("inf"),
            margin_total=float("inf"),
            is_sufficient=False,
            buffer_rs=buffer_rs,
            code=-1,
            message=str(e)
        )


def get_multiorder_margin(
    fyers,
    orders: List[Dict[str, Any]],
    buffer_rs: float = 0.0
) -> MultiOrderMarginRequirement:
    """
    Query FYERS API v3 to calculate consolidated margin required for multiple orders
    (e.g., simultaneous two-sided BUY + SELL quotes). Considers hedging benefits.

    Args:
        fyers: Authenticated FyersModel instance.
        orders: List of order dictionaries with keys (symbol, qty, side, type, productType, limitPrice).
        buffer_rs: Extra cash buffer in INR to preserve.

    Returns:
        MultiOrderMarginRequirement instance.
    """
    payload_data = []
    for o in orders:
        payload_data.append({
            "symbol": o["symbol"],
            # convert LOTS -> underlying SHARES for NFO futures; commodities
            # and cash equity pass through unchanged
            "qty": config.broker_qty(o["qty"], o["symbol"]),
            "side": o["side"],
            "type": o.get("type", 1),
            "productType": o.get("productType", "INTRADAY"),
            "limitPrice": round(float(o.get("limitPrice", 0.0)), 2),
            "stopLoss": float(o.get("stopLoss", 0.0)),
            "stopPrice": float(o.get("stopPrice", 0.0)),
            "takeProfit": float(o.get("takeProfit", 0.0)),
        })

    payload = {"data": payload_data}

    try:
        resp = _call_margin_api(fyers, payload)
        code = resp.get("code", 0)
        s = resp.get("s", "")
        msg = resp.get("message", "")

        if s == "ok" and "data" in resp:
            data = resp["data"]
            avail = float(data.get("margin_avail", 0.0))
            # Same field semantics as get_order_margin: `margin_total` is the
            # marginal margin for the quoted basket; `margin_new_order` would
            # include the account's existing/parked margin.
            new_ord = float(data.get("margin_total", 0.0))
            total = float(data.get("margin_total", new_ord))
            sufficient = avail >= (new_ord + buffer_rs)

            return MultiOrderMarginRequirement(
                order_count=len(orders),
                margin_avail=avail,
                margin_required=new_ord,
                margin_total=total,
                is_sufficient=sufficient,
                buffer_rs=buffer_rs,
                code=code,
                message=msg
            )
        else:
            log.error(f"Multiorder Margin API error: code={code} msg='{msg}'")
            return MultiOrderMarginRequirement(
                order_count=len(orders),
                margin_avail=0.0,
                margin_required=float("inf"),
                margin_total=float("inf"),
                is_sufficient=False,
                buffer_rs=buffer_rs,
                code=code,
                message=msg or "Failed to calculate multiorder margin"
            )
    except Exception as e:
        log.error(f"Exception calling Fyers Multiorder Margin API: {e}")
        return MultiOrderMarginRequirement(
            order_count=len(orders),
            margin_avail=0.0,
            margin_required=float("inf"),
            margin_total=float("inf"),
            is_sufficient=False,
            buffer_rs=buffer_rs,
            code=-1,
            message=str(e)
        )


def get_account_funds(fyers) -> Dict[str, Any]:
    """
    Fetch comprehensive funds & margin limits from fyers.funds().
    Returns dictionary with key balances:
      total_balance, available_balance, clear_balance, collateral, realized_pnl
    """
    try:
        funds = fyers.funds()
        if funds.get("s") != "ok":
            log.error(f"Failed to fetch account funds: {funds}")
            return {}

        results = {}
        for item in funds.get("fund_limit", []):
            title = item.get("title", "")
            eq_amt = float(item.get("equityAmount", 0.0))
            com_amt = float(item.get("commodityAmount", 0.0))
            results[title] = {"equity": eq_amt, "commodity": com_amt, "total": eq_amt + com_amt}

        return results
    except Exception as e:
        log.error(f"Error fetching account funds: {e}")
        return {}
