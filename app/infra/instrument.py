"""
app/infra/instrument.py
-----------------------
Asset / instrument metadata model.

Each strategy trades one instrument (a FYERS derivative or cash equity).
This module is what makes the platform *asset friendly*: a single global
``lot_size`` / ``tick_size`` is replaced by a per-instrument ``Instrument``
that knows its segment + asset type + contract multiplier + money-per-unit,
so position sizing and margin handling can differ per market:

  * COMMODITY futures  -> traded in **lots** (margin-capped sizing, up to max_position_qty)
  * EQUITY futures     -> traded in **lots**, sized dynamically from margin
  * EQUITY (cash)      -> traded in **shares** (lot = 1), sized dynamically

Sizing policy itself lives in ``app.sizing``; this module only resolves the
static, per-instrument facts used everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class Segment(str, Enum):
    COMMODITY = "COMMODITY"
    EQUITY = "EQUITY"
    EQUITY_FUT = "EQUITY_FUT"


class AssetType(str, Enum):
    EQUITY = "equity"            # cash shares, lot = 1
    EQUITY_FUT = "equity_fut"    # index/stock futures, traded in lots
    COMMODITY_FUT = "commodity_fut"


_SEGMENT_TO_ASSET = {
    Segment.COMMODITY: AssetType.COMMODITY_FUT,
    Segment.EQUITY: AssetType.EQUITY,
    Segment.EQUITY_FUT: AssetType.EQUITY_FUT,
}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    segment: Segment
    asset_type: AssetType
    lot_size: int                 # contract multiplier (1 for cash equity)
    tick_size: float              # minimum price increment
    tick_value_rs: float          # monetary value of one tick = lot * tick
    margin_per_lot_rs: float = 0.0  # broker margin for one lot (0 = unknown)
    quote_in_lots: bool = True    # False => quote quantity is raw shares
    display_name: str = ""

    def __post_init__(self):
        object.__setattr__(self, "display_name", self.display_name or self.symbol)

    @property
    def futures(self) -> bool:
        return self.asset_type in (AssetType.EQUITY_FUT, AssetType.COMMODITY_FUT)


def is_commodity(segment: str | Segment) -> bool:
    return str(segment).upper() == "COMMODITY"


def resolve_segment(segment: str | Segment) -> Segment:
    if isinstance(segment, Segment):
        return segment
    s = str(segment).upper()
    for seg in Segment:
        if seg.value == s:
            return seg
    # common spellings: "EQ"/"CM" -> EQUITY, "FUT"/"EQ_FUT" -> equity futures
    if s in ("EQ", "CM", "EQUITY"):
        return Segment.EQUITY
    if s in ("FUT", "EQ_FUT", "FUTURES"):
        return Segment.EQUITY_FUT
    return Segment.COMMODITY


def asset_type_for(segment: str | Segment) -> AssetType:
    return _SEGMENT_TO_ASSET[resolve_segment(segment)]


def _looks_like_futures(symbol: str) -> bool:
    up = symbol.upper()
    return ("FUT" in up or "FUTURE" in up)


def _default_lot_size(segment: Segment, symbol: str) -> int:
    """Sensible defaults when LOT_SIZE is not configured (0 = auto).

    Commodity futures default to 1250 (MCX natural gas style) when the symbol
    is a futures symbol, else 1.  Equity futures default to 1 lot (the FYERS
    multiplier is resolved from the symbol at order time), cash equity to 1.
    """
    if segment == Segment.COMMODITY:
        return 1250 if _looks_like_futures(symbol) else 1
    return 1


def _default_tick_size(segment: Segment) -> float:
    if segment == Segment.COMMODITY:
        return 0.10
    return 0.05


def shares_to_lots(raw: int, lot_size: int) -> int:
    """Map a raw signed quantity in underlying SHARES (equity futures) to a
    whole-lot count, FLOORING so a partial/odd lot never rounds the position up.

    This is the single conversion rule for both the fill bookkeeping (on_fill)
    and the broker-position reconciliation: the broker reports NFO equity
    futures in shares (750 SBIN shares = 1 lot), so a raw value BELOW one lot
    must never be treated as a giant number of lots. Flooring (never
    ``round()``) is deliberate: a kill-switch flatten must close whole lots and
    leave a sub-lot remainder rather than oversell into an opposite position.
    MCX commodity futures report lots directly and must NOT call this helper.
    """
    if lot_size <= 0:
        return raw
    mag = abs(raw) // lot_size
    return -mag if raw < 0 else mag


def build_instrument(
    symbol: str,
    segment: str | Segment,
    lot_size: int = 0,
    tick_size: float = 0.0,
    margin_per_lot_rs: float = 0.0,
) -> Instrument:
    seg = resolve_segment(segment)
    asset = asset_type_for(seg)

    # A cash-equity segment pointing at a futures contract should be treated as
    # equity futures; the enum alias also resolves EQUITY_FUT/FUT spellings.
    if asset == AssetType.EQUITY and _looks_like_futures(symbol):
        asset = AssetType.EQUITY_FUT

    lot = lot_size or _default_lot_size(seg, symbol)
    tick = tick_size or _default_tick_size(seg)

    quote_in_lots = asset in (AssetType.EQUITY_FUT, AssetType.COMMODITY_FUT)
    tick_value = round(lot * tick, 6)

    return Instrument(
        symbol=symbol,
        segment=seg,
        asset_type=asset,
        lot_size=lot,
        tick_size=tick,
        tick_value_rs=tick_value,
        margin_per_lot_rs=margin_per_lot_rs,
        quote_in_lots=quote_in_lots,
    )


class InstrumentRegistry:
    """Holds the instruments currently being traded, keyed by symbol."""

    def __init__(self) -> None:
        self._instruments: Dict[str, Instrument] = {}

    def register(self, inst: Instrument) -> Instrument:
        self._instruments[inst.symbol] = inst
        return inst

    def get(self, symbol: str) -> Optional[Instrument]:
        return self._instruments.get(symbol)

    def resolve(
        self,
        symbol: str,
        segment: str | Segment,
        lot_size: int = 0,
        tick_size: float = 0.0,
        margin_per_lot_rs: float = 0.0,
    ) -> Instrument:
        existing = self.get(symbol)
        if existing is not None:
            return existing
        return self.register(build_instrument(symbol, segment, lot_size, tick_size, margin_per_lot_rs))

    def update_margin(self, symbol: str, margin_per_lot_rs: float):
        if margin_per_lot_rs <= 0:
            return
        existing = self.get(symbol)
        if existing is None:
            return
        inst = Instrument(
            symbol=existing.symbol,
            segment=existing.segment,
            asset_type=existing.asset_type,
            lot_size=existing.lot_size,
            tick_size=existing.tick_size,
            tick_value_rs=existing.tick_value_rs,
            margin_per_lot_rs=margin_per_lot_rs,
            quote_in_lots=existing.quote_in_lots,
            display_name=existing.display_name,
        )
        self._instruments[symbol] = inst

    def all(self) -> list[Instrument]:
        return list(self._instruments.values())

    def clear(self) -> None:
        self._instruments.clear()


# global registry, populated at boot from the configured universe
instrument_registry = InstrumentRegistry()


def parse_universe(
    raw: str,
    default_symbol: str,
    default_segment: str,
    default_lot: int = 0,
    default_tick: float = 0.0,
) -> Instrument:
    """Build the *first* instrument from the UNIVERSE spec.

    ``raw`` is a comma-separated list where each entry is
    ``symbol:segment:lot_size:tick_size`` (all but symbol optional).
    The first entry defines the primary/actively-traded asset; the rest are
    scanned candidates. Falls back to ``default_symbol`` when ``raw`` is empty.
    """
    spec = raw.strip()
    if not spec:
        return build_instrument(
            default_symbol, default_segment, default_lot, default_tick
        )
    parts = [p for p in (chunk.split(":") for chunk in spec.split(",")) if p]
    sym, seg, lot, tick = _split_entry(parts[0], default_segment, default_lot, default_tick)
    return build_instrument(sym, seg, lot, tick)


def parse_universe_all(
    raw: str,
    default_symbol: str,
    default_segment: str,
    default_lot: int = 0,
    default_tick: float = 0.0,
    margin_per_lot: float = 0.0,
) -> List[Instrument]:
    """All instruments in the UNIVERSE (primary first), for scanning/quoting."""
    out: List[Instrument] = []
    if not raw.strip():
        out.append(build_instrument(
            default_symbol, default_segment, default_lot, default_tick, margin_per_lot
        ))
        return out
    for chunk in raw.split(","):
        if not chunk.strip():
            continue
        sym, seg, lot, tick = _split_entry(
            chunk.split(":"), default_segment, default_lot, default_tick
        )
        out.append(build_instrument(sym, seg, lot, tick, margin_per_lot))
    if not out:
        out.append(build_instrument(
            default_symbol, default_segment, default_lot, default_tick, margin_per_lot
        ))
    return out


_SEGMENT_TOKENS = {"EQUITY", "EQUITY_FUT", "COMMODITY", "EQ", "CM", "FUT", "EQ_FUT", "FUTURES"}


def _split_entry(
    tokens: List[str],
    default_segment: str,
    default_lot: int,
    default_tick: float,
):
    """Split one ``symbol:segment:lot:tick`` entry.

    FYERS symbols carry a colon themselves (``NSE:TCS-EQ``), so we locate the
    segment token *positionally*: the first token that is a known segment name
    divides the symbol (everything before it, rejoined with ':') from the
    numeric tail (lot, tick).
    """
    idx = None
    for i, tok in enumerate(tokens):
        if tok.strip().upper() in _SEGMENT_TOKENS:
            idx = i
            break
    if idx is None:
        # no explicit segment -> the whole entry is a plain symbol
        return tokens[0].strip(), default_segment, default_lot, default_tick
    sym = ":".join(t.strip() for t in tokens[:idx]).strip()
    seg = tokens[idx].strip()
    tai = idx + 1
    lot = (
        int(tokens[tai])
        if len(tokens) > tai and tokens[tai].strip().isdigit()
        else default_lot
    )
    tai += 1
    tick = (
        float(tokens[tai])
        if len(tokens) > tai and tokens[tai].strip()
        else default_tick
    )
    return sym or tokens[0].strip(), seg, lot, tick
