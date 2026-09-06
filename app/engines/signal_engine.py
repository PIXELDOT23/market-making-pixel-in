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
from typing import Dict, List, Optional

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.db import Database
from app.infra.redis import RedisBus


class SignalEngine(Engine):
    name = "signal_engine"

    def __init__(self, bus: RedisBus, db: Database, tick_size: Optional[float] = None):
        super().__init__(bus, db)
        self.tick_size = tick_size or settings.resolved_tick_size
        self._last: Dict[str, schema.MarketTick] = {}
        self._listener: Optional[asyncio.Task] = None

    def _on_market(self, channel: str, raw: bytes):
        self._last[channel] = raw

    async def _decode_market(self, raw: bytes) -> schema.MarketTick:
        return schema.decode(schema.MarketTick, raw)

    async def run(self):
        def handler(ch: str, raw: bytes):
            asyncio.create_task(self._handle(ch, raw))

        await self.bus.subscribe("fyers:market:*", handler=handler)
        hb = asyncio.create_task(self._heartbeat_loop())
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        hb.cancel()

    async def _handle(self, channel: str, raw: bytes):
        try:
            tick = await self._decode_market(raw)
        except Exception:
            return
        sig = await self.compute(tick)
        if sig is None:
            return
        self._mark()
        try:
            await self.obs.publish_signal(sig)
            await self.db.insert_signal(sig)
        except Exception:
            pass

    async def compute(self, tick: schema.MarketTick) -> Optional[schema.SignalMetrics]:
        mid = tick.bid + (tick.ask - tick.bid) / 2 if (tick.bid and tick.ask) else tick.ltp
        spread_ticks = int(round((tick.ask - tick.bid) / self.tick_size)) if (tick.bid and tick.ask) else 0

        # churn is computed by the data engine snapshot; approximate here from
        # single-tick flow. Strategy engine overwrites with the snapshot metric.
        churn = 0.0
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

        if spread_ticks > settings.max_spread_widen_ticks:
            quoteable = False
            reasons.append("spread beyond cap")

        return schema.SignalMetrics(
            symbol=tick.symbol,
            strategy="*",
            ts=tick.ts,
            mid=mid,
            churn_ticks_per_sec=churn,
            vol_widening_ticks=widening,
            liquidity_grade=round(liq, 3),
            spread_ticks_now=spread_ticks,
            quoteable=quoteable,
            reasons=reasons,
        )