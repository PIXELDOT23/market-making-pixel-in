"""
app/infra/master_contracts.py
-----------------------------
FYERS symbol-master universe: "take over the whole market".

FYERS publishes a daily-refreshed JSON master per exchange segment at
``https://public.fyers.in/sym_details/<SEGMENT>_sym_master.json`` (keyed by the
API symbol ticker, carrying ``minLotSize`` / ``tickSize`` / ``optType`` /
``tradeStatus`` / ``exSeries`` …). We use it to build the *whole* trading
universe instead of hand-picking symbols:

  * NFO equity market -> every live equity/index future (``optType == XX``)
  * MCX commodity     -> every live futures contract (``optType == XX``)

A copy is cached (``~/.fyers/sym_master``) and refreshed once it is stale so a
boot never depends on the network unless the cache is old.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import date
from typing import Dict, List, Optional, Tuple

from app.infra.instrument import AssetType, Instrument, Segment

MASTERS = ("NSE_FO", "MCX_COM")
BASE_URL = "https://public.fyers.in/sym_details/{}_sym_master.json"
CACHE_DIR = os.path.expanduser("~/.fyers/sym_master")
CACHE_MAX_AGE_SEC = 20 * 3600  # refreshes ~daily; be lenient at boot

_MCX_EXPIRY = re.compile(r"^(?P<base>.+?)(?P<yy>\d{2})(?P<mmm>[A-Z]{3})FUT$")
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"
), start=1)}


def _cache_path(master: str) -> str:
    return os.path.join(CACHE_DIR, f"{master}.json")


def _is_fresh(path: str) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < CACHE_MAX_AGE_SEC
    except OSError:
        return False


def load_master(master: str, force: bool = False) -> Dict[str, dict]:
    """Download + cache one symbol master; returns {symbol_ticker: record}."""
    if master not in MASTERS:
        raise ValueError(f"unknown master {master!r}; choose from {MASTERS}")
    path = _cache_path(master)
    if not force and _is_fresh(path):
        with open(path) as f:
            return json.load(f)
    url = BASE_URL.format(master)
    req = urllib.request.Request(url, headers={"User-Agent": "pixel-mm/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return data


async def load_master_async(master: str, force: bool = False) -> Dict[str, dict]:
    """Async download+cache: run the blocking urllib I/O off the event loop so
    a master-contract refresh can never freeze every engine (the reason the bot
    previously stalled with a Redis timeout right after boot)."""
    import asyncio

    path = _cache_path(master)
    if not force and _is_fresh(path):
        try:
            return await asyncio.to_thread(_read_cached, path)
        except OSError:
            pass
    return await asyncio.to_thread(load_master, master, force)


def _read_cached(path: str) -> Dict[str, dict]:
    with open(path) as f:
        return json.load(f)


def _active(rec: dict) -> bool:
    return int(rec.get("tradeStatus", 1)) == 1


# ------------------------------------------------------------------ builders
def _mcx_expiry(symbol: str) -> Optional[Tuple[str, int, int]]:
    """Parse ``MCX:BASE<YY><MMM>FUT`` -> (base, year, month). None if not a
    dated futures contract (e.g. cash/composite symbols)."""
    m = _MCX_EXPIRY.match(symbol)
    if m is None:
        return None
    month = _MONTHS.get(m.group("mmm"))
    if month is None:
        return None
    return m.group("base"), 2000 + int(m.group("yy")), month


def is_mcx_expired(symbol: str, today: Optional[date] = None) -> bool:
    """True once the contract's parsed expiry month has passed ``today``."""
    parsed = _mcx_expiry(symbol)
    if parsed is None:
        return False
    _, year, month = parsed
    ref = today or date.today()
    return (year, month) < (ref.year, ref.month)


def _near_contracts(instruments: List[Instrument], today: date) -> List[Instrument]:
    """Keep only the NEAREST-expiring contract per MCX underlying family.

    Expiry is parsed from the symbol itself (``BASE<YY><MMM>FUT``), so the
    rollover works straight off the cached master. When the near contract has
    already expired, the next expiring one is picked automatically.
    """
    if not instruments:
        return instruments
    by_family: Dict[str, List[Tuple[Tuple[int, int], Instrument]]] = {}
    unknown: List[Instrument] = []
    for inst in instruments:
        parsed = _mcx_expiry(inst.symbol)
        if parsed is None:
            unknown.append(inst)
            continue
        base, year, month = parsed
        by_family.setdefault(base, []).append(((year, month), inst))
    out: List[Instrument] = []
    for family, contracts in by_family.items():
        eligible = [e for e in contracts if e[0] >= (today.year, today.month)]
        if not eligible:
            eligible = contracts
        _, inst = min(eligible, key=lambda e: e[0])
        out.append(inst)
    return unknown + out


def nse_equity_futures(near_contract_only: bool = True) -> List[Instrument]:
    """Every live NFO equity/index future (optType XX = futures) as an
    Instrument.

    The master carries ``minLotSize`` (the contract a.k.a. index/stock lot) and
    ``tickSize`` directly, so no guesses are needed. With ``near_contract_only``
    (default) only the NEAREST expiring contract per underlying family is
    returned; the rest of the calendar is dropped so the book trades the liquid
    near month exclusively. Penny-stock underlyings (last close below
    ``settings.nfo_min_underlying_price_rs``, e.g. IDEAFUT @ ~₹16) are excluded:
    their per-lot SPAN margin is huge and they are not market-making targets.
    """
    master = load_master("NSE_FO")
    return _nse_equity_futures_from(master, near_contract_only)


async def nse_equity_futures_async(near_contract_only: bool = True) -> List[Instrument]:
    master = await load_master_async("NSE_FO")
    return _nse_equity_futures_from(master, near_contract_only)


def _nse_equity_futures_from(master: Dict[str, dict], near_contract_only: bool) -> List[Instrument]:
    from app.config import settings
    min_price = settings.nfo_min_underlying_price_rs
    out: List[Instrument] = []
    for ticker, rec in master.items():
        if not ticker.startswith("NSE:"):
            continue
        if rec.get("optType", "XX") not in ("XX", ""):
            continue
        if not _active(rec):
            continue
        prev_close = float(rec.get("previousClose") or rec.get("prevClose") or 0.0)
        if min_price > 0 and prev_close > 0 and prev_close < min_price:
            continue
        lot = int(rec.get("minLotSize") or rec.get("qtyMultiplier") or 0)
        tick = float(rec.get("tickSize") or 0.0)
        if lot <= 0 or tick <= 0:
            continue
        out.append(Instrument(
            symbol=ticker,
            segment=Segment.EQUITY_FUT,
            asset_type=AssetType.EQUITY_FUT,
            lot_size=lot,
            tick_size=tick,
            tick_value_rs=round(lot * tick, 6),
            margin_per_lot_rs=0.0,
            quote_in_lots=True,
            display_name=rec.get("symDetails") or ticker,
        ))
    if not near_contract_only:
        return out
    near = _near_contracts(out, date.today())
    dropped = len(out) - len(near)
    from app.infra import logging as log
    picked = ", ".join(inst.symbol for inst in near) if near else "-"
    log.info(f"NFO near-contract rollover: {len(near)}/{len(out)} kept (dropped {dropped} back months)")
    log.info(f"NFO near contracts: {picked}")
    return near


def mcx_futures(near_contract_only: bool = True) -> List[Instrument]:
    """Every live MCX commodity future (optType XX = futures) as an Instrument.

    With ``near_contract_only`` (default) only the nearest expiring contract per
    underlying family is returned; the rest of the calendar is dropped so the
    book trades the liquid near month exclusively.
    """
    master = load_master("MCX_COM")
    return _mcx_futures_from(master, near_contract_only)


async def mcx_futures_async(near_contract_only: bool = True) -> List[Instrument]:
    master = await load_master_async("MCX_COM")
    return _mcx_futures_from(master, near_contract_only)


def _mcx_futures_from(master: Dict[str, dict], near_contract_only: bool) -> List[Instrument]:
    out: List[Instrument] = []
    for ticker, rec in master.items():
        if not ticker.startswith("MCX:"):
            continue
        if rec.get("optType", "XX") not in ("XX", ""):
            continue
        if not _active(rec):
            continue
        # FYERS sets minLotSize=1 for MCX in the master; the real contract
        # size is qtyMultiplier (e.g. NATURALGAS=1250, CRUDEOIL=100, GOLD=100).
        lot = int(rec.get("qtyMultiplier") or rec.get("minLotSize") or 0)
        tick = float(rec.get("tickSize") or 0.0)
        if lot <= 0 or tick <= 0:
            continue
        out.append(Instrument(
            symbol=ticker,
            segment=Segment.COMMODITY,
            asset_type=AssetType.COMMODITY_FUT,
            lot_size=lot,
            tick_size=tick,
            tick_value_rs=round(lot * tick, 6),
            margin_per_lot_rs=0.0,
            quote_in_lots=True,
            display_name=rec.get("symDetails") or ticker,
        ))
    if not near_contract_only:
        return out
    near = _near_contracts(out, date.today())
    dropped = len(out) - len(near)
    from app.infra import logging as log
    picked = ", ".join(inst.symbol for inst in near) if near else "-"
    log.info(f"MCX near-contract rollover: {len(near)}/{len(out)} kept (dropped {dropped} back months)")
    log.info(f"MCX near contracts: {picked}")
    return near


async def whole_market_async(
    include_equity: bool,
    include_mcx: bool,
    mcx_near_contract_only: bool = True,
    nfo_near_contract_only: bool = True,
) -> List[Instrument]:
    """Async (non-blocking) combined NFO-equity-futures + MCX-futures universe."""
    insts: List[Instrument] = []
    if include_equity:
        insts.extend(await nse_equity_futures_async(near_contract_only=nfo_near_contract_only))
    if include_mcx:
        insts.extend(await mcx_futures_async(near_contract_only=mcx_near_contract_only))
    return insts


def whole_market(
    include_equity: bool,
    include_mcx: bool,
    mcx_near_contract_only: bool = True,
    nfo_near_contract_only: bool = True,
) -> List[Instrument]:
    """Combined NFO-equity-futures + MCX-futures universe, unknown margin/lot."""
    insts: List[Instrument] = []
    if include_equity:
        insts.extend(nse_equity_futures(near_contract_only=nfo_near_contract_only))
    if include_mcx:
        insts.extend(mcx_futures(near_contract_only=mcx_near_contract_only))
    return insts