"""
app/scanner.py
--------------
Market-making scanner: turns raw market state into a per-asset attractiveness
rank so the strategy engine quotes only the best candidates.

Inputs per asset:
  * price book (ltp/bid/ask + sizes)  -> spread & liquidity
  * volatility signal (widening ticks, churn)
  * margin view (margin available, margin per lot)
  * cost context (breakeven spread ticks)

Output: a ``ScannerRow`` with a composite ``score``; the engine keeps them
sorted by score so ``rank 1`` is the most attractive market-making target.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, List, Optional

from app import schema
from app.config import settings
from app.infra.instrument import AssetType, Instrument
from app import sizing as sizing_mod


def compute_row(
    symbol: str,
    inst: Instrument,
    snap: schema.MarketSnapshot,
    signal: Optional[schema.SignalMetrics],
    margin_avail: float,
    margin_per_lot: float,
    max_position_qty: int,
    margin_risk_fraction: float,
    vol_reduction_per_tick: float,
    min_liquidity: float,
    inventory: int = 0,
    cost_calc: Optional[Callable[[float, int], Optional[schema.ScanCost]]] = None,
) -> schema.ScannerRow:
    """Score one asset. Everything is derived; no network calls.

    ``cost_calc`` (optional) is called with ``(mid, quote_qty)`` and returns
    post-charges economics. When present and the asset is *not* profitable
    after charges it is de-ranked and anything other than a bare hold is
    blocked.
    """
    tick_size = inst.tick_size
    ltp = snap.ltp
    bid = snap.bid
    ask = snap.ask
    mid = snap.mid
    spread_price = (ask - bid) if (bid is not None and ask is not None and ask >= bid) else None
    spread_ticks = int(round(spread_price / tick_size)) if (spread_price and tick_size > 0) else 0
    liq = signal.liquidity_grade if signal is not None else 0.0
    if liq == 0.0 and (snap.bid_size > 0 or snap.ask_size > 0):
        liq = min(snap.bid_size, snap.ask_size) / max(snap.bid_size, snap.ask_size)
        if liq <= 0:
            liq = 0.25
    is_connected = snap.is_connected and mid is not None
    vol_widening = signal.vol_widening_ticks if signal is not None else 0

    # ---------------- composite score ----------------
    # 1.0 = perfect market. Start full, subtract penalties.
    score = 1.0
    reasons: List[str] = []
    if not is_connected:
        score -= 0.9
        reasons.append("feed down")
    if spread_ticks <= 0:
        score -= 0.5
        reasons.append("no spread")
    else:
        # wider than ideal -> less attractive (10% per tick over 1 tick)
        score -= 0.10 * max(0, spread_ticks - 1)
    if liq < min_liquidity:
        score -= 0.4
        reasons.append(f"thin book {liq:.2f}")
    if liq <= 0.0:
        score -= 0.3
        reasons.append("no depth")
    # churn = volatility flow; very high vol is bad for inventory risk
    score -= min(0.6, 0.12 * vol_widening)
    if vol_widening >= 3:
        reasons.append(f"vol widen {vol_widening}t")
    # NOTE: volume is intentionally NOT part of the composite `score` — volume
    # is the primary rank key downstream (see rank_rows), so doubling it here
    # would distort the market-quality score. Profitable rows sort ahead of no-
    # edge rows regardless of volume; volume orders within each tier.

    quoteable = (
        is_connected
        and spread_ticks > 0
        and liq >= min_liquidity
        and (signal is None or signal.quoteable)
    )

    # dynamic size (lots / shares) from margin + inventory + mid + volatility;
    # commodity futures are sized the same way (up to max_position_qty) so the
    # greedy budget reserves the size the live strategy will actually quote.
    quote_qty, reason = sizing_mod.quote_size_for(
        inst=inst,
        margin_avail=margin_avail,
        margin_fraction=margin_risk_fraction,
        margin_per_lot=margin_per_lot,
        inventory=inventory,
        max_position_qty=max_position_qty,
        mid=mid,
        vol_widening_ticks=vol_widening,
        vol_reduction_per_tick=vol_reduction_per_tick,
    )
    if reason:
        reasons.append(reason)
    # with unknown broker margin commodity_size pins to max_position_qty lots;
    # a sized 0 lot here means the margin gate rejects.
    if quote_qty <= 0:
        quoteable = False
        reasons.append("margin < 1 lot")
    elif "margin" in reason:
        reasons.append(reason)
    # dynamic sizing never returns 0 (it floors at 1 lot), so a margin-thin
    # equity/future target is flagged here explicitly
    elif (
        margin_avail > 0 and margin_per_lot > 0 and margin_avail < margin_per_lot
        and inst.asset_type != AssetType.COMMODITY_FUT
    ):
        quoteable = False
        reasons.append("margin thin")

    score = round(max(0.0, min(1.0, score)), 4)

    # ---- post-charges profitability (the "is it worth it" gate) ----
    net_profit_rs, charges_rs, breakeven_ticks, required_ticks = 0.0, 0.0, 0, 0
    profitable = False
    if mid is not None and cost_calc is not None:
        try:
            cost = cost_calc(mid, int(quote_qty))
        except Exception:
            cost = None
        if cost is not None:
            net_profit_rs = cost.net_profit_rs
            charges_rs = cost.round_trip_charges_rs
            breakeven_ticks = cost.breakeven_spread_ticks
            required_ticks = cost.required_spread_ticks
            profitable = cost.profitable
            if not profitable:
                quoteable = False
                score -= 0.4
                reasons.append(
                    f"charges eat spread (net ₹{net_profit_rs:.2f}; breakeven {breakeven_ticks}t)"
                )

    # Report the per-lot margin requirement even when stood down for margin,
    # so the UI shows what it would take to quote instead of a blank req.
    margin_req_rs = round(int(quote_qty) * margin_per_lot, 2) if margin_per_lot > 0 else 0.0
    if quote_qty <= 0 and margin_per_lot > 0:
        margin_req_rs = round(margin_per_lot, 2)

    return schema.ScannerRow(
        symbol=symbol,
        rank=0,  # assigned after sorting
        segment=inst.segment.value,
        asset_type=inst.asset_type.value,
        ltp=ltp, bid=bid, ask=ask, mid=mid,
        bid_size=snap.bid_size, ask_size=snap.ask_size,
        spread_ticks=spread_ticks,
        liquidity_grade=round(liq, 4),
        churn_ticks_per_sec=round(snap.churn_ticks_per_sec, 3),
        vol_widening_ticks=vol_widening,
        volume=snap.volume,
        quoteable=quoteable,
        margin_avail=round(margin_avail, 2),
        margin_per_lot=round(margin_per_lot, 2),
        quote_qty=int(quote_qty),
        lot_size=int(inst.lot_size),
        margin_req_rs=margin_req_rs,
        score=score,
        net_profit_rs=round(net_profit_rs, 2),
        round_trip_charges_rs=round(charges_rs, 2),
        breakeven_spread_ticks=breakeven_ticks,
        required_spread_ticks=required_ticks,
        profitable=profitable,
        reasons=reasons,
    )


def rank_rows(rows: List[schema.ScannerRow], active_limit: int) -> List[schema.ScannerRow]:
    """Rank the candidates.

    Profitable rows always top the list. Within a profitability tier ranking
    is by the smoothed RoM ``weight`` (cycle-profit-per-margin) FIRST, then by
    traded volume for liquid-name tie-breaking, then by the market-quality
    ``score``. When weighting is disabled every weight is 0 so the sort simply
    falls back to the old volume-first order. Ranks are assigned after sorting;
    ``active_limit`` just marks how many of them may rest quotes. (Margin
    filtering happens next, in the cache.)"""
    ordered = sorted(
        rows,
        key=lambda r: (
            r.profitable is False,       # profitable first
            -r.weight,                   # most profitable-per-margin first
            -r.volume,                   # then most-traded (highest volume)
            -r.score,                    # market-quality tiebreaker
        ),
    )
    for i, r in enumerate(ordered, start=1):
        r.rank = i
    return ordered


def assign_rom_weights(
    rows: List[schema.ScannerRow],
    tracker: "WeightTracker",
    realized_of,
    booked_of,
    *,
    min_cycles: int,
    volume_floor: float,
    weight_max: float,
    size_min: float,
    size_max: float,
) -> None:
    """Compute the per-row RoM weight + size multiplier in place.

    ``realized_of(symbol)`` returns ``(realized_pnl_rs, cycles_completed)`` and
    ``booked_of(symbol)`` the margin this symbol has reserved — both provided by
    the strategy engine's live state. Once a symbol has completed ``min_cycles``
    cycles its weight is REALIZED pnl per booked rupee; below that it falls back
    to the theoretical touch RoM (net_profit / margin_req) already on the row.

    Weights are EWMA-smoothed, blended against a normalized log-volume factor
    (so an ultra-wide slow book cannot win on spread alone) and capped. The size
    multiplier is the weight normalized across this refresh's rows into
    ``[size_min, size_max]`` and feeds equity/equity-futures sizing."""
    if not rows:
        return
    vols = [math.log1p(max(r.volume, 0)) for r in rows]
    vmin, vmax = min(vols), max(vols)
    vrange = vmax - vmin
    for r in rows:
        realized_pnl, cycles = realized_of(r.symbol)
        booked = booked_of(r.symbol)
        if cycles >= min_cycles and booked > 0:
            raw = max(0.0, realized_pnl / booked)
        elif r.margin_req_rs > 0 and r.net_profit_rs > 0:
            raw = max(0.0, r.net_profit_rs / r.margin_req_rs)
        else:
            raw = 0.0
        raw = min(raw, weight_max)
        ewma = tracker.smooth(r.symbol, raw)
        logv = math.log1p(max(r.volume, 0))
        vnorm = (logv - vmin) / vrange if vrange > 0 else 0.5
        vfactor = volume_floor + (1.0 - volume_floor) * vnorm
        r.weight = round(ewma * vfactor, 6)

    ws = [r.weight for r in rows if r.weight > 0]
    if ws:
        wmin, wmax = min(ws), max(ws)
        wr = wmax - wmin
    else:
        wmin = wmax = wr = 0.0
    for r in rows:
        if ws and wr > 1e-9:
            n = max(0.0, min(1.0, (r.weight - wmin) / wr))
        else:
            n = 0.5
        r.size_mult = round(size_min + (size_max - size_min) * n, 3)


def select_within_margin(
    rows: List[schema.ScannerRow],
    active_limit: int,
    margin_avail: Optional[float] = None,
    buffer_rs: float = 0.0,
    reserve_multiplier: float = 1.0,
) -> List[schema.ScannerRow]:
    """Greedy top-N selection respecting the account margin.

    Rows arrive rank-ordered. We walk them from rank 1 keeping each candidate
    quoteable only while the margin it consumes (quote_qty x margin_per_lot,
    scaled by ``reserve_multiplier`` plus ``buffer_rs``) still fits inside the
    available account margin; candidates that exceed the remaining margin are
    stood down so the live book never pre-commits margin beyond what the
    account holds. Ranks are preserved for display; only the ``quoteable`` flag
    gates quoting.

    ``buffer_rs`` is charged into the budget so the top-N reservation never
    corners the account's free cash. ``reserve_multiplier`` lets a standing
    two-sided pair (buy + sell) reserve margin for BOTH legs. An unknown
    account margin (``margin_avail <= 0``) disables the margin budget entirely
    — top-N is then capped by count only and the pre-trade broker gate remains
    the backstop.
    """
    if not rows:
        return rows
    if margin_avail is None or margin_avail <= 0:
        margin_avail = rows[0].margin_avail
    known = margin_avail > 0
    used = 0.0
    slots = 0
    for r in rows:
        if slots >= active_limit:
            r.quoteable = False
            continue
        if not r.quoteable:
            continue
        charge = r.margin_req_rs * reserve_multiplier + buffer_rs
        if known and charge > 0 and used + charge > margin_avail:
            r.quoteable = False
            r.reasons.append(f"margin block: +₹{charge:,.0f} > avail ₹{margin_avail:,.0f}")
            continue
        used += charge
        slots += 1
    return rows


RowBuilder = Callable[[Instrument], Optional[schema.ScannerRow]]


class ScannerCache:
    """Stateful ranked view of the candidate universe, refreshed by the
    strategy engine on a timer. Pure computation each cycle; no I/O here.

    Strategies read ``rank_of`` / ``is_quoteable`` per decision so the whole
    swarm stays gated behind the scanner output.

    Quoting gate is PER-SEGMENT: each exchange/segment gets its own
    ``active_limit`` quoting slots, so a liquid NFO batch can never starve MCX
    of its own quoting budget. ``rank`` remains the GLOBAL rank (across the
    whole market) for cross-market comparison; only ``quoteable`` is gated
    per-segment.
    """

    def __init__(self, instruments: List[Instrument], active_limit: int = 2):
        self.instruments: List[Instrument] = list(instruments)
        self.active_limit = max(1, active_limit)
        self.refresh_sec: float = 1.0
        self._rows: List[schema.ScannerRow] = []
        self._rank_by_symbol: Dict[str, int] = {}
        self._quoteable_by_symbol: Dict[str, bool] = {}
        self._weight_by_symbol: Dict[str, float] = {}
        self._size_mult_by_symbol: Dict[str, float] = {}
        # Optional RoM weighting hook (set by the strategy engine). Called with
        # the freshly built, unsorted rows each refresh to set weight/size_mult.
        self._weight_assigner: Optional[Callable[[List[schema.ScannerRow]], None]] = None
        self.last_scan_ts: float = 0.0

    # ------------------------------------------------------------------ reads
    def rows(self) -> List[schema.ScannerRow]:
        return list(self._rows)

    def rank_of(self, symbol: str) -> int:
        return self._rank_by_symbol.get(symbol, 0)

    def is_quoteable(self, symbol: str) -> bool:
        return self._quoteable_by_symbol.get(symbol, False)

    def weight_of(self, symbol: str) -> float:
        return self._weight_by_symbol.get(symbol, 0.0)

    def size_mult_of(self, symbol: str) -> float:
        return self._size_mult_by_symbol.get(symbol, 1.0)

    def top_n(self, n: Optional[int] = None) -> List[schema.ScannerRow]:
        cap = n or self.active_limit
        return [r for r in self._rows if 0 < r.rank <= cap]

    def available_symbols(self) -> List[str]:
        return [r.symbol for r in self._rows if r.quoteable]

    # ------------------------------------------------------------------ write
    def refresh(self, build_row: RowBuilder):
        rows: List[schema.ScannerRow] = []
        for inst in self.instruments:
            try:
                row = build_row(inst)
            except Exception:
                continue
            if row is not None:
                rows.append(row)
        if self._weight_assigner is not None:
            try:
                self._weight_assigner(rows)
            except Exception:
                pass
        ordered = rank_rows(rows, self.active_limit)
        margin_avail = next((r.margin_avail for r in ordered if r.margin_avail > 0), 0.0)
        # margin-only gating here (account margin is global); the per-segment
        # quoting COUNT is applied afterward by _gate_per_segment, so each
        # exchange keeps its own quoting budget.
        if settings.margin_ledger_enabled:
            budget = margin_avail * settings.margin_utilization_target
            select_within_margin(
                ordered, len(ordered), budget,
                buffer_rs=settings.min_free_margin_buffer_rs,
                reserve_multiplier=2.0 if settings.margin_reserve_both_sides else 1.0,
            )
        else:
            select_within_margin(ordered, len(ordered), margin_avail)
        self._rows = ordered
        self._rank_by_symbol = {r.symbol: r.rank for r in ordered}
        # per-segment quoting gate: each segment gets its own quoting-slot budget,
        # proportional to its summed RoM weight (min 1). Allocates by the same
        # ordering rank_rows uses so each exchange keeps its own budget while
        # the winning segment earns a larger share.
        self._quoteable_by_symbol = self._gate_per_segment(ordered)
        # keep the per-row `quoteable` flag in sync so the API/UI agree
        for r in self._rows:
            r.quoteable = self._quoteable_by_symbol.get(r.symbol, False)
        self._weight_by_symbol = {r.symbol: r.weight for r in self._rows}
        self._size_mult_by_symbol = {r.symbol: r.size_mult for r in self._rows}
        if ordered:
            self.last_scan_ts = time.time()

    def _gate_per_segment(self, rows: List[schema.ScannerRow]) -> Dict[str, bool]:
        out: Dict[str, bool] = {}
        by_seg: Dict[str, List[schema.ScannerRow]] = {}
        for r in rows:
            by_seg.setdefault(r.segment, []).append(r)

        # Segment quoting budget is RoM-proportional by default: the summed
        # weight of each segment's quoteable candidates decides its share of
        # the shared ``active_limit`` slots, so the segment proving the most
        # cycle-profit-per-margin gets more of the book (MCX can dominate NFO
        # at night and vice-versa mid-day). Each segment keeps at least 1 slot
        # so a quieter exchange is never completely squeezed out.
        seg_weights = {
            seg: sum(max(r.weight, 0.0) for r in seg_rows if r.quoteable)
            for seg, seg_rows in by_seg.items()
        }
        total_w = sum(seg_weights.values())

        for seg, seg_rows in by_seg.items():
            # candidates that already pass the margin/quoteable checks, sorted
            # by the same key as the global rank
            eligible = sorted(
                (r for r in seg_rows if r.quoteable),
                key=lambda r: (r.profitable is False, -r.weight, -r.volume, -r.score),
            )
            slots = self.active_limit
            if settings.slot_weight_enabled and total_w > 0 and seg_weights[seg] > 0:
                slots = max(
                    1,
                    min(
                        self.active_limit,
                        int(round(self.active_limit * seg_weights[seg] / total_w)),
                    ),
                )
            for i, r in enumerate(eligible):
                out[r.symbol] = i < slots
        return out