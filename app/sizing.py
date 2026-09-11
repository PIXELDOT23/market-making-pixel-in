"""
app/sizing.py
-------------
Position sizing policy, per asset type.

This is the heart of "asset friendly" quoting:

  * COMMODITY futures  -> margin-capped lot count up to ``max_position_qty``:
    the quote size is capped by what the available account margin covers
    (slots = margin_avail / margin_per_lot) and the strategy stands down
    (0 lots) when even one lot is not affordable.
  * EQUITY futures     -> **dynamic** size: floor(margin_avail * fraction /
    margin_per_lot), then clamped to [1, max_position_qty - |inventory|].
  * EQUITY (cash)      -> **dynamic** size in shares (lot = 1), same margin math.

Volatility is a first-class input for the dynamic policies: when the signal
widens (more vol ticks), the quote size shrinks by ``vol_reduction_per_tick``
per extra widening tick so the trading book stays flat in wild moves.

The sizer is intentionally pure/stateless: given an instrument + observable
account state it returns the number of lots (or shares) to quote. All state
comes from RiskEngine.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from app.infra.instrument import AssetType, Instrument


def _finite(v: Optional[float], default: float = 0.0) -> float:
    """Coerce a possibly None / NaN / inf margin input to a sane number.

    The broker margin API is a free-form JSON field: a malformed server can
    produce ``1e999`` (parsed as ``inf``) or a non-numeric value, and one bad
    float must never take down the quoting sizer (``math.floor(inf/nan)``
    raises before any order can be sized).
    """
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def sizing_policy(inst: Instrument) -> str:
    """Human label of the sizing policy for an instrument."""
    if inst.asset_type == AssetType.COMMODITY_FUT:
        return "margin-aware-pinned-lot"
    return "dynamic-margin-volatility-inventory"


def _estimate_margin_per_lot(mid: Optional[float], lot_size: int) -> float:
    """~10% of the notional contract value at mid, until the broker reports."""
    mid = _finite(mid)
    if mid > 0 and lot_size > 0:
        return round(mid * lot_size * 0.10, 2)
    return 0.0


def _vol_scale(vol_widening_ticks: int, vol_reduction_per_tick: float) -> float:
    """1.0 at no vol; shrinks 10% per widening tick (floor at 0.25)."""
    if vol_widening_ticks <= 0:
        return 1.0
    scale = 1.0 - vol_reduction_per_tick * vol_widening_ticks
    return max(0.25, min(1.0, scale))


def commodity_size(
    inst: Instrument,
    margin_avail: float,
    margin_fraction: float,
    margin_per_lot: float,
    inventory: int = 0,
    max_position_qty: int = 1,
    mid: Optional[float] = None,
    max_lots: int = 1,
) -> Tuple[int, str]:
    """Commodity futures: margin-capped lot count up to ``max_lots``.

    Quotes up to ``max_lots`` (default 1, but `quote_size_for` passes
    ``max_position_qty`` so the account's margin headroom drives how many
    lots actually get quoted) whenever the *known* account margin covers at
    least one lot; stands down (0 lots) when it does not.

    ``margin_fraction`` is deliberately NOT applied here — the overall book
    budget across the selected instruments is enforced by the scanner's
    ``select_within_margin`` against the full available margin, and the
    broker pre-trade check is the final gate. A commodity whose single lot
    does not fit inside the raw available margin is simply not affordable.
    Unknown margin (broker has not reported yet) is treated leniently —
    quote the pinned lot and let the pre-trade margin gate be the backstop.
    """
    base = max(1, int(max_lots))
    margin_avail = _finite(margin_avail)
    margin_per_lot = _finite(margin_per_lot)
    if margin_per_lot <= 0:
        margin_per_lot = _estimate_margin_per_lot(mid, inst.lot_size)
    if margin_avail <= 0 or margin_per_lot <= 0:
        return base, f"margin unknown -> pinned {base} lot(s)"
    slots = int(math.floor(margin_avail / margin_per_lot))
    if slots < 1:
        return 0, (
            f"margin insufficient: avail ₹{margin_avail:,.0f} < 1 lot "
            f"(~₹{margin_per_lot:,.0f})"
        )
    qty = min(base, slots)
    if qty < base:
        return qty, f"margin-capped {qty}/{slots} lots (avail ₹{margin_avail:,.0f})"
    return qty, f"margin-backed {qty} lot(s) (avail ₹{margin_avail:,.0f})"


def dynamic_size(
    inst: Instrument,
    margin_avail: float,
    margin_fraction: float,
    margin_per_lot: float,
    inventory: int,
    max_position_qty: int,
    mid: Optional[float] = None,
    vol_widening_ticks: int = 0,
    vol_reduction_per_tick: float = 0.10,
    weight_mult: float = 1.0,
) -> Tuple[int, str]:
    """Equity / equity-futures dynamic size, clamped by margin & inventory.

    Returns ``(quote_size, reason)`` where quote_size is in the instrument's
    natural unit (lots for futures, shares for cash equity).

    ``mid`` (optional) is used to estimate the per-lot margin from the notional
    contract value (mid * lot_size) until the broker returns a real margin figure.

    ``weight_mult`` scales the margin-derived size by the symbol's RoM weight
    (see scanner.assign_rom_weights) so higher-profit-per-margin names quote
    more lots; it defaults to 1.0 (neutral) and only ever scales the margin
    headroom, never exceeding the position cap.
    """
    margin_per_lot = _finite(margin_per_lot)
    if margin_per_lot <= 0:
        margin_per_lot = _estimate_margin_per_lot(mid, inst.lot_size)

    if margin_per_lot <= 0:
        # No reliable margin-per-lot yet -> play safe with the minimum, and
        # rely on the pre-trade margin gate to veto if insufficient.
        return 1, "margin-per-lot unknown -> min size"

    # headroom left on the position cap after current inventory
    margin_avail = _finite(margin_avail)
    margin_per_lot = _finite(margin_per_lot)
    weight_mult = _finite(weight_mult, default=1.0)
    headroom = max_position_qty - abs(int(inventory or 0))
    size_from_margin = int(
        math.floor((margin_avail * _finite(margin_fraction) * max(weight_mult, 0.0)) / margin_per_lot)
    )

    # volatility scaling applied on top of the margin-sized slots
    scale = _vol_scale(vol_widening_ticks, _finite(vol_reduction_per_tick, default=0.10))
    size = max(1, min(int(math.floor(size_from_margin * scale)), headroom))
    if weight_mult != 1.0:
        reason = f"weighted ×{weight_mult:.2f} (margin base {size_from_margin})"
    elif scale < 1.0:
        reason = f"vol-scaled ×{scale:.2f} (margin base {size_from_margin})"
    elif size >= headroom:
        reason = f"capped by position headroom {headroom}"
    elif size <= 1:
        reason = "min size (margin too thin / headroom exhausted)"
    else:
        reason = f"margin-based {size_from_margin}"
    return size, reason


def quote_size_for(
    inst: Instrument,
    margin_avail: float,
    margin_fraction: float,
    margin_per_lot: float,
    inventory: int,
    max_position_qty: int,
    mid: Optional[float] = None,
    vol_widening_ticks: int = 0,
    vol_reduction_per_tick: float = 0.10,
    weight_mult: float = 1.0,
) -> Tuple[int, str]:
    """Top-level entry: pick the policy from the instrument's asset type."""
    if inst.asset_type == AssetType.COMMODITY_FUT:
        return commodity_size(
            inst, margin_avail, margin_fraction, margin_per_lot,
            inventory=inventory, max_position_qty=max_position_qty,
            mid=mid, max_lots=max(1, int(max_position_qty)),
        )
    return dynamic_size(
        inst, margin_avail, margin_fraction, margin_per_lot,
        inventory, max_position_qty, mid=mid,
        vol_widening_ticks=vol_widening_ticks,
        vol_reduction_per_tick=vol_reduction_per_tick,
        weight_mult=weight_mult,
    )