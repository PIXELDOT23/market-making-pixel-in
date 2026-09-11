"""
app/engines/signal_engine.py
----------------------------
SIGNAL GENERATION ENGINE

Consumes real-time market ticks and produces per-symbol SignalMetrics that
other engines (strategy, execution) act on. Kept cheap: single pass, no
allocation in the hot path.

Metrics produced:
  * mid price
  * churn (ticks/sec) — market-making adverse-selection risk
  * volatility widening (extra spread ticks demanded)
  * liquidity grade from best bid/ask depth
  * observed bid-ask spread (ticks)
  * quoteable flag + reasons (what gate fired)
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque, Dict, List, Optional

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.db import Database
from app.infra.instrument import instrument_registry
from app.infra.redis import RedisBus


class SignalEngine(Engine):
    name = "signal_engine"

    def __init__(self, bus: RedisBus, db: Database, tick_size: Optional[float] = None):
        super().__init__(bus, db)
        self.tick_size = tick_size or settings.resolved_tick_size
        self._last: Dict[str, schema.MarketTick] = {}
        self._listener: Optional[asyncio.Task] = None
        # Rolling (ts, ltp) history per symbol for live churn measurement — the
        # same tick/sec of absolute movement the data engine reports in its
        # snapshot. Bounded per symbol so whole-market scan stays flat memory.
        self._churn_hist: Dict[str, Deque[tuple]] = {}

    def _churn_from_hist(self, hist, window_sec: float, tick_size: float) -> float:
        now = time.time()
        cutoff = now - max(window_sec, 1.0)
        if len(hist) < 2:
            return 0.0
        # walk to the first sample inside the window (deque is FIFO by ts)
        first_i = 0
        for i, (ts, _p) in enumerate(hist):
            if ts >= cutoff:
                first_i = i
                break
        pts = [hist[j] for j in range(first_i, len(hist))]
        if len(pts) < 2:
            return 0.0
        dist = sum(abs(b - a) for (_t1, a), (_t2, b) in zip(pts, pts[1:]))
        elapsed = pts[-1][0] - pts[0][0]
        if elapsed <= 0 or dist <= 0:
            return 0.0
        return max(0.0, dist / elapsed / max(tick_size, 1e-9))

    def _on_market(self, channel: str, raw: bytes):
        self._last[channel] = raw

    async def run(self):
        async def handler(ch: str, raw: bytes):
            await self._handle(ch, raw)

        await self.bus.subscribe("fyers:market:*", handler=handler)
        hb = asyncio.create_task(self._heartbeat_loop())
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        hb.cancel()

    async def _handle(self, channel: str, raw: bytes):
        tick = schema.decode(schema.MarketTick, raw)
        sig = self.compute(tick)
        if sig is None:
            return
        self._mark()
        try:
            await self.obs.publish_signal(sig)
            await self.db.insert_signal(sig)
        except Exception:
            pass

    def compute(self, tick: schema.MarketTick) -> Optional[schema.SignalMetrics]:
        mid = tick.bid + (tick.ask - tick.bid) / 2 if (tick.bid and tick.ask) else tick.ltp
        # Whole-market scan mode mixes instruments with different tick sizes
        # (NFO 0.05, MCX 0.10). Measuring spread/churn against ONE global tick
        # silently deems half the universe un-quoteable or over-widens it, so
        # resolve the per-symbol tick and fall back to the primary default.
        inst = instrument_registry.get(tick.symbol)
        tick_size = inst.tick_size if inst is not None and inst.tick_size > 0 else self.tick_size
        spread_ticks = int(round((tick.ask - tick.bid) / tick_size)) if (tick.bid and tick.ask) else 0

        # Live churn from the raw tick stream (ticks/sec of |price movement|),
        # then demand a wider spread in fast markets. The strategy engine also
        # falls back to the data snapshot's churn for the boot warm-up window.
        hist = self._churn_hist.setdefault(tick.symbol, deque(maxlen=4096))
        hist.append((tick.ts, tick.ltp))
        churn = self._churn_from_hist(hist, settings.volatility_window_sec, tick_size)

        reasons: List[str] = []
        quoteable = True

        if spread_ticks < 1:
            quoteable = False
            reasons.append("no two-sided quote")

        # liquidity grade from depth — only meaningful when both sides present
        liq = 0.0
        if tick.bid_size > 0 and tick.ask_size > 0:
            liq = min(tick.bid_size, tick.ask_size) / max(tick.bid_size, tick.ask_size)
        elif tick.bid_size > 0 or tick.ask_size > 0:
            liq = 0.25

        widening = 0
        if churn > 0 and settings.volatility_widen_factor > 0:
            widening = int(churn * settings.volatility_widen_factor)
        widening = min(widening, settings.max_spread_widen_ticks)

        if settings.volatility_halt_quoting_tps > 0 and churn > settings.volatility_halt_quoting_tps:
            quoteable = False
            reasons.append("excessive churn")

        if spread_ticks > settings.max_spread_widen_ticks:
            quoteable = False
            reasons.append("spread beyond cap")

        return schema.SignalMetrics(
            symbol=tick.symbol,
            strategy="*",
            ts=tick.ts,
            mid=mid,
            churn_ticks_per_sec=round(churn, 3),
            vol_widening_ticks=widening,
            liquidity_grade=round(liq, 3),
            spread_ticks_now=spread_ticks,
            quoteable=quoteable,
            reasons=reasons,
        )