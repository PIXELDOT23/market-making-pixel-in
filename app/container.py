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
from app.ml import engine as ml_engine
from app.ml import model as ml_model
import app.infra.logging as log


class EngineManager:
    def __init__(self):
        self.bus = RedisBus()
        self.db = Database()
        self.token_store = TokenStore(self.bus)
        self.engines: Dict[str, object] = {}
        self._tasks: List[asyncio.Task] = []
        self._supervisors: List[asyncio.Task] = []
        self._tasks_done = False
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

        # --- ML engine configuration ---
        ml_engine.configure(
            db=self.db,
            enabled=settings.ml_enabled,
            capture_enabled=settings.ml_capture_enabled,
            depth_persist_interval=settings.ml_depth_persist_interval_sec,
            feature_persist_interval=settings.ml_feature_persist_interval_sec,
            widen_threshold=settings.ml_widen_threshold,
            widen_extra_ticks=settings.ml_widen_extra_ticks,
        )
        if settings.ml_enabled and settings.ml_model_path:
            loaded = ml_model.load_model(
                name=settings.ml_model_name,
                path=settings.ml_model_path,
            )
            if loaded:
                ml_model.set_active_model(settings.ml_model_name)
        elif settings.ml_enabled:
            # No explicit path -> pick up the active model from the DB registry
            loaded = ml_model.load_active_from_db(self.db)

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
        self._supervisors = [
            asyncio.create_task(self._supervise(name, engine), name=f"supervise:{name}")
            for name, engine in self.engines.items()
        ]
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

    async def _supervise(self, name: str, engine, restart_sec: float = 5.0):
        """Watch one engine task; if it dies it is restarted after a short
        backoff instead of leaving the pipeline silently missing its fills/
        quotes feed (a dead execution engine while quoting continues is the
        worst case: resting orders, positions never booked)."""
        log.info(f"[boot] supervisor armed for {name}")
        while not self._tasks_done:
            task = getattr(engine, "_task", None)
            if task is None:
                await asyncio.sleep(1.0)
                continue
            try:
                await task
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if self._tasks_done:
                    return
                log.error(
                    f"[boot] engine '{name}' task died: {exc!r} — "
                    f"restarting in {restart_sec:.0f}s"
                )
                await asyncio.sleep(restart_sec)
                if self._tasks_done:
                    return
                try:
                    engine._stop = asyncio.Event()
                    task = asyncio.create_task(
                        engine._run_wrapper(), name=f"engine:{name}"
                    )
                    engine._task = task
                    self._tasks.append(task)
                    engine.status = "healthy"
                    engine.detail = "restarted by supervisor"
                    log.warn(f"[boot] engine '{name}' restarted by supervisor")
                except Exception as restart_exc:
                    log.error(
                        f"[boot] restart of '{name}' failed: {restart_exc!r} — "
                        "will retry"
                    )

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
        scanner_rows: List[schema.ScannerRow] = []
        strategy_engine = self.engines.get("strategy")
        if strategy_engine is not None and hasattr(strategy_engine, "scanner_rows"):
            scanner_rows = strategy_engine.scanner_rows()
        # Build the operator's "quoting board": quoteable instruments from EVERY
        # segment first (they are the ones actually resting quotes and, thanks to
        # the per-segment quoting budget, include MCX as well as NFO) so an MCX
        # name is never hidden behind a wall of higher-global-rank NSE futures.
        # Pad the frame with the top stood-down instruments so the ranked view
        # still has context, bounded to a light snapshot (≤ 10 cards).
        quoteable = [r for r in scanner_rows if r.quoteable]
        stood_down = [r for r in scanner_rows if not r.quoteable]
        board = list(quoteable)
        if len(board) < 10:
            board += stood_down[: 10 - len(board)]
        else:
            board = board[:10]
        scanner_rows = board
        ranked_symbols = {r.symbol for r in scanner_rows}
        markets = [m for m in markets if m.symbol in ranked_symbols]
        risk = self.engines.get("risk")
        risk_active = risk is not None
        risk_healthy = risk.constraints_healthy if risk is not None else True
        halts = list(risk.halt_reasons) if risk is not None else []

        # enrich inventory/unrealized pnl on decision metrics from risk
        for m in decisions:
            if risk is not None:
                s = strategy_engine.strategies.get(m.strategy) if strategy_engine is not None else None
                m.inventory = risk.position(s.symbol) if s is not None else 0

        return schema.PipelineSnapshot(
            ts=time.time(),
            engines=hbs,
            strategies=strategies,
            decisions=decisions,
            markets=markets,
            scanner=scanner_rows,
            risk_active=risk_active,
            risk_healthy=risk_healthy,
            risk_halts=halts,
            session_open=market_hours.any_open(),
            session_close_in_sec=max(
                (st["close_in_sec"] for st in market_hours.segment_status()
                 if st["open"]), default=0.0,
            ),
            segments=[schema.SegmentStatus(**st) for st in market_hours.segment_status()],
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

    async def flatten_orphans_at_boot(self):
        """Power cut / hard-kill recovery. Ctrl+C flattens through the lifespan,
        but a sudden power loss never runs it — so any net position a previous
        run left on the broker is squared off before the new book can quote.
        Safe to run when nothing is open (broker returns zero positions)."""
        if not settings.flatten_orphans_at_boot:
            return
        execu = self.engines.get("execution")
        if execu is None:
            return
        log.warn("[boot] checking for orphan positions from a previous run...")
        try:
            await asyncio.wait_for(self._reconcile_orphans(execu), timeout=40)
        except asyncio.TimeoutError:
            log.error("[boot] orphan check timed out (broker unreachable?)")

    async def _reconcile_orphans(self, execu):
        """Flatten any net positions the broker reports at boot (killswitch
        square-off path, bypasses the risk gate).

        Only positions on symbols the bot itself manages are considered orphans.
        Any other position the broker reports (e.g. a manually punched options
        order) is left completely untouched.
        """
        if not hasattr(execu, "_call"):
            return
        # don't flatten if the broker can't be reached or auth is missing
        if getattr(execu, "_fyers", None) is None:
            log.warn("[boot] broker not attached yet — orphan flatten skipped")
            return
        try:
            resp = await execu._call("positions", {}, get="positions", timeout=8)
        except Exception as exc:
            log.warn(f"[boot] orphan position check failed: {exc!r}")
            return
        nets = (resp or {}).get("netPositions", [])
        open_positions = [p for p in nets if abs(int(p.get("netQty") or 0)) > 0]
        if not open_positions:
            log.success("[boot] no orphan positions to flatten")
            return

        managed = [p for p in open_positions if execu._managed(str(p.get("symbol", "")))]
        skipped = [p for p in open_positions if p not in managed]
        for p in skipped:
            log.warn(
                f"[boot] orphan check: SKIP unmanaged position "
                f"{p.get('symbol', '?')} (manual order?) — will not flatten"
            )
        if not managed:
            log.success("[boot] no bot-managed orphan positions to flatten")
            return
        log.warn(
            f"[boot] FOUND {len(managed)} bot-managed orphan position(s) from a "
            f"previous run — squaring off"
        )
        try:
            await asyncio.wait_for(execu.flatten_all(), timeout=30)
            log.success("[boot] orphan positions flattened")
        except asyncio.TimeoutError:
            log.error("[boot] orphan flatten timed out")

    async def close(self, flatten: bool = True):
        """Orderly teardown. `flatten=True` (Ctrl+C / market close) first squares
        off the book through the risk+execution chain, then stops engines."""
        if flatten and self.started:
            await self.emergency_flatten()
        await self.shutdown()

    async def shutdown(self):
        self._tasks_done = True
        for sup in self._supervisors:
            sup.cancel()
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