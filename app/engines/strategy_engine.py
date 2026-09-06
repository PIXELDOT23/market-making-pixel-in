"""
app/engines/strategy_engine.py
------------------------------
STRATEGY ENGINE

  * Lists every strategy that is live (registry -> StrategyUniverse).
  * Runs each strategy's decision loop against fresh snapshots, signal
    metrics and cost quotes (all pushed on the Redis bus by their engines).
  * Measures the decisions: latency, quote placement/cancel counts,
    fills, cycles completed, realized/unrealized PnL, and publishes
    DecisionMetrics for the Monitor engine.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from app import schema
from app.engines.base import Engine
from app.infra import logging as log
from app.infra.db import Database
from app.infra.redis import RedisBus
from app.strategies.base import BaseStrategy, StrategyUniverse


class StrategyEngine(Engine):
    name = "strategy_engine"

    def __init__(self, bus: RedisBus, db: Database, swarm: StrategyUniverse, engines: Dict[str, object]):
        super().__init__(bus, db)
        self.swarm = swarm
        self.engines = engines                # {risk, execution, data, cost, signal}
        self.strategies: Dict[str, BaseStrategy] = {}
        self._latest_signal: Dict[str, schema.SignalMetrics] = {}
        self._last_decision_ts: Dict[str, float] = {}
        self._listener_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ lifecycle
    async def start_strategies(self):
        for strategy in self.swarm.build():
            self.strategies[strategy.name] = strategy
            await self.db.upsert_strategy(strategy.info())
        # register all strategy symbols in the data engine after build
        symbols = [s.symbol for s in self.strategies.values()]
        if self.engines.get("data") is not None:
            self.engines["data"].register_symbols(symbols)

    def strategy_list(self) -> List[schema.StrategyInfo]:
        return [s.info() for s in self.strategies.values()]

    def strategy_metrics(self) -> List[schema.DecisionMetrics]:
        out = []
        for s in self.strategies.values():
            now = time.time()
            window = max(now - s.started_ts, 1)
            out.append(schema.DecisionMetrics(
                strategy=s.name,
                ts=now,
                decisions_total=s.decisions_total,
                decisions_per_min=round(s.decisions_total / (window / 60.0), 2),
                quotes_placed=s.quotes_placed,
                quotes_cancelled=s.quotes_cancelled,
                fills_received=s.fills_received,
                cycles_completed=s.cycles_completed,
                realized_pnl_rs=round(s.realized_pnl_rs, 2),
                unrealized_pnl_rs=0.0,  # refreshed from risk inventory in monitor
                inventory=0,
                avg_decision_latency_ms=round(s.avg_latency_ms, 3),
                p99_decision_latency_ms=round(s.p99_latency_ms, 3),
                last_decision=s.__dict__.get("last_decision", "-"),
            ))
        return out

    # ------------------------------------------------------------------ main loop
    async def run(self):
        await self.start_strategies()

        # collect market ticks as they land (through the data engine memory)
        async def market_handler(ch: str, raw: bytes):
            try:
                tick = schema.decode(schema.MarketTick, raw)
                await self._on_tick(tick)
            except Exception as exc:
                log.error(f"[market handler] {exc!r}")
        await self.bus.subscribe("fyers:market:*", handler=market_handler)

        hb = asyncio.create_task(self._heartbeat_loop())
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            await self._publish_metrics()
        hb.cancel()

    async def _on_tick(self, tick: schema.MarketTick):
        decision_interval = 2.0
        last = self._last_decision_ts.get(tick.symbol, 0.0)
        if time.time() - last < decision_interval:
            return
        self._last_decision_ts[tick.symbol] = time.time()

        strategy = self.strategies.get(self._strategy_for_symbol(tick.symbol))
        if strategy is None:
            log.warn(f"[strategy] no strategy for {tick.symbol}")
            return
        t0 = time.perf_counter()

        snapshot = self.engines["data"].snapshot(tick.symbol)
        signal = self._latest_signal.get(tick.symbol)
        if snapshot is None or snapshot.mid is None:
            log.warn(f"[strategy] no snapshot/mid for {tick.symbol}: {snapshot}")
            return
        cost = await self.engines["cost"].quote_for(
            symbol=tick.symbol, strategy=strategy.name, qty=strategy.qty,
            segment=strategy.segment, lot_size=self._lot_size(strategy.segment),
            tick_size=self._tick_size(strategy.segment), price=snapshot.mid,
        )
        sig = signal
        if sig is None:
            sig = schema.SignalMetrics(
                symbol=tick.symbol, strategy=strategy.name, ts=tick.ts, mid=snapshot.mid,
                churn_ticks_per_sec=snapshot.churn_ticks_per_sec,
                vol_widening_ticks=0, liquidity_grade=0.5,
                spread_ticks_now=0, quoteable=True,
            )

        decision = await strategy.on_signal(snapshot, sig, cost, self.engines)

        latency_ms = (time.perf_counter() - t0) * 1000
        strategy.decisions_total += 1
        strategy.record_latency(latency_ms)
        strategy.__dict__["last_decision"] = f"{decision} ({latency_ms:.1f}ms)"
        self._latest_signal[tick.symbol] = sig
        if decision in ("EXIT_SELL", "EXIT_BUY"):
            risk = self.engines["risk"]
            if risk.net_position == 0:
                strategy.on_cycle_closed(risk.realized_pnl)

    def _strategy_for_symbol(self, symbol: str) -> Optional[str]:
        for name, s in self.strategies.items():
            if s.symbol == symbol:
                return name
        return None

    def _lot_size(self, segment: str) -> int:
        from app.config import settings
        return settings.resolved_lot_size

    def _tick_size(self, segment: str) -> float:
        from app.config import settings
        return settings.resolved_tick_size

    async def _publish_metrics(self):
        for m in self.strategy_metrics():
            try:
                await self.obs.publish_decision(m)
                await self.db.insert_decision(m)
            except Exception:
                pass