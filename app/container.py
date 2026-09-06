"""
app/container.py
----------------
Wires the seven engines onto shared infra (Redis, PostgreSQL) and provides
the PipelineSnapshot used by the Monitor engine and the API.

This is the composition root: `boot()` creates every engine, starts the
data flow, and returns the running EngineManager.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from app import schema
from app.config import settings
from app.infra.auth import TokenStore
from app.infra.db import Database
from app.infra.redis import RedisBus
from app.infra import market_hours
from app.engines.data_engine import DataEngine
from app.engines.cost_engine import CostEngine
from app.engines.risk_engine import RiskEngine
from app.engines.signal_engine import SignalEngine
from app.engines.execution_engine import ExecutionEngine
from app.engines.strategy_engine import StrategyEngine
from app.engines.monitor_engine import MonitorEngine
from app.strategies.base import StrategyUniverse
import app.infra.logging as log


class EngineManager:
    def __init__(self):
        self.bus = RedisBus()
        self.db = Database()
        self.token_store = TokenStore(self.bus)
        self.engines: Dict[str, object] = {}
        self._tasks: List[asyncio.Task] = []
        self.started = False
        self._flattened = False

    # ------------------------------------------------------------------ boot
    async def boot(self, dry_run: bool = False):
        log.banner("Booting low-latency multi-engine architecture")
        log.info("[boot] connecting redis...")
        await self.bus.connect()
        log.info("[boot] connecting postgres...")
        await self.db.connect()
        log.info("[boot] infra online")

        data = DataEngine(self.bus, self.db, self.token_store)
        cost = CostEngine(self.bus, self.db)
        risk = RiskEngine(self.bus, self.db, self.token_store)
        signal = SignalEngine(self.bus, self.db)
        execu = ExecutionEngine(self.bus, self.db, self.token_store, risk=risk)
        strategy = StrategyEngine(
            self.bus, self.db, StrategyUniverse(),
            engines={"data": data, "cost": cost, "risk": risk,
                     "execution": execu, "signal": signal},
        )
        monitor = MonitorEngine(self.bus, self.db)

        self.engines = {
            "data": data, "cost": cost, "risk": risk, "signal": signal,
            "execution": execu, "strategy": strategy, "monitor": monitor,
        }
        monitor.set_snapshot_fn(self._build_snapshot)
        monitor.set_engine_lookup(lambda: self.engines)

        # start engines
        for name, engine in self.engines.items():
            log.info(f"[boot] starting {name}...")
            self._tasks.append(await engine.start())
            log.success(f"[boot] {name} started")
        self.started = True

        # reconcile any fresher token the user logged-in with elsewhere (a new
        # FYERS token invalidates previous ones, so cache drift must never win)
        reconciled = await self.token_store.reconcile_from_file()
        if reconciled:
            log.info("[boot] token reconciled across stores")
        await self.token_store.ensure_valid()
        log.success("[boot] all engines online")
        return self

    async def _seed_token_from_disk(self):
        """Legacy file -> Redis backfill (superseded by reconcile_from_file)."""
        return None

    # ------------------------------------------------------------------ snapshot
    def _build_snapshot(self) -> schema.PipelineSnapshot:
        hbs: List[schema.EngineHeartbeat] = []
        for name, engine in self.engines.items():
            hbs.append(engine.heartbeat(engine._loop_latency_ms))
        strategies: List[schema.StrategyInfo] = []
        decisions: List[schema.DecisionMetrics] = []
        strategy_engine = self.engines.get("strategy")
        if strategy_engine is not None:
            strategies = strategy_engine.strategy_list()
            decisions = strategy_engine.strategy_metrics()
        markets: List[schema.MarketSnapshot] = []
        data_engine = self.engines.get("data")
        if data_engine is not None:
            for sym in data_engine.symbols:
                snap = data_engine.snapshot(sym)
                if snap is not None:
                    markets.append(snap)
        risk = self.engines.get("risk")
        risk_active = risk is not None
        risk_healthy = risk.constraints_healthy if risk is not None else True
        halts = list(risk.halt_reasons) if risk is not None else []

        # enrich inventory/unrealized pnl on decision metrics from risk
        for m in decisions:
            if risk is not None:
                m.inventory = risk.net_position

        return schema.PipelineSnapshot(
            ts=time.time(),
            engines=hbs,
            strategies=strategies,
            decisions=decisions,
            markets=markets,
            risk_active=risk_active,
            risk_healthy=risk_healthy,
            risk_halts=halts,
            session_open=market_hours.is_open(),
            session_close_in_sec=market_hours.seconds_until_close(),
        )

    async def emergency_flatten(self):
        """Kill-switch used on shutdown / interruption / market close: halts the
        risk gate and flattens every position + cancels resting orders."""
        if self._flattened:
            return
        self._flattened = True
        log.warn("[shutdown] emergency flatten (risk-engine driven)")
        risk = self.engines.get("risk")
        execu = self.engines.get("execution")
        if risk is not None:
            risk.halt("server shutdown — auto flatten")
        if execu is not None and hasattr(execu, "flatten_all"):
            try:
                await asyncio.wait_for(execu.flatten_all(), timeout=30)
            except asyncio.TimeoutError:
                log.error("[shutdown] flatten timed out")

    async def close(self, flatten: bool = True):
        """Orderly teardown. `flatten=True` (Ctrl+C / market close) first squares
        off the book through the risk+execution chain, then stops engines."""
        if flatten and self.started:
            await self.emergency_flatten()
        await self.shutdown()

    async def shutdown(self):
        for engine in self.engines.values():
            try:
                await engine.stop()
            except Exception:
                pass
        try:
            await self.db.close()
        except Exception:
            pass
        try:
            await self.bus.close()
        except Exception:
            pass
        self.started = False
        log.warn("[shutdown] engines stopped, infra closed")