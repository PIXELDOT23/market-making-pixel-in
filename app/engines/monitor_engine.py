"""
app/engines/monitor_engine.py
-----------------------------
MONITOR ENGINE

Visualizes the entire pipeline and controls/manages it:

  * aggregates engine heartbeats, live strategies, decision metrics, market
    snapshots and risk health into a single PipelineSnapshot
  * publishes the snapshot on the Redis bus (consumed by the FastAPI WS
    gateway and persisted to Postgres)
  * subscribes to the command channel: PAUSE/RESUME strategy, FLATTEN,
    HALT_ALL, RESET — and carries them out via the execution engine
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, List, Optional

from app import schema
from app.config import settings
from app.engines.base import Engine
from app.infra.db import Database
from app.infra.redis import RedisBus
import app.infra.logging as log


class MonitorEngine(Engine):
    name = "monitor_engine"

    def __init__(self, bus: RedisBus, db: Database, snapshot_fn: Optional[Callable[[], "schema.PipelineSnapshot"]] = None):
        super().__init__(bus, db, heartbeat_sec=1.0)
        self.snapshot_fn = snapshot_fn          # provided by the app container
        self.engine_lookup: Optional[Callable[[], Dict[str, object]]] = None
        self.commands_processed = 0
        self.last_publish_ms = 0.0
        self._snapshot_buffer: Optional[schema.PipelineSnapshot] = None
        self.close_events = 0

        # market-close flatten latch: runs at most once per IST trading date
        self._session_date: str = ""
        self._flattened_on_session = False
        self._was_open = False

    def set_snapshot_fn(self, fn: Callable[[], "schema.PipelineSnapshot"]):
        self.snapshot_fn = fn

    def set_engine_lookup(self, fn: Callable[[], Dict[str, object]]):
        self.engine_lookup = fn

    def _engines(self) -> Dict[str, object]:
        if self.engine_lookup is not None:
            return self.engine_lookup()
        app_ = self.snapshot_fn.__self__ if callable(self.snapshot_fn) else None
        return getattr(app_, "engines", {}) if app_ is not None else {}

    def latest_snapshot(self) -> Optional[schema.PipelineSnapshot]:
        return self._snapshot_buffer

    # ------------------------------------------------------------------ control plane
    async def handle_command(self, cmd: schema.Command) -> str:
        self.commands_processed += 1
        log.warn(f"[monitor] command received: {cmd.type} target={cmd.target}")

        engines = self._engines()

        risk = engines.get("risk")
        execu = engines.get("execution")
        strategies = getattr(engines.get("strategy"), "strategies", {}) if engines.get("strategy") else {}

        if cmd.type == "HALT_ALL":
            if risk:
                risk.halt("halt-all command from monitor")
            if execu:
                await execu.flatten_all()
            return "HALT_ALL executed"

        if cmd.type == "FLATTEN":
            if execu:
                await execu.flatten_all()
            return "FLATTEN executed"

        if cmd.type == "RESET":
            if risk:
                risk.reset()
            return "RESET executed"

        if cmd.type in ("PAUSE_STRATEGY", "RESUME_STRATEGY"):
            for s in strategies.values():
                if cmd.target in ("*", s.name):
                    s.paused = cmd.type == "PAUSE_STRATEGY"
                    s.enabled = cmd.type != "PAUSE_STRATEGY"
            return f"{cmd.type} applied to {cmd.target}"

        return f"unknown command {cmd.type}"

    # ------------------------------------------------------------------ loop
    async def run(self):
        async def command_handler(ch: str, raw: bytes):
            try:
                cmd = schema.decode(schema.Command, raw)
                await self.handle_command(cmd)
            except Exception as exc:
                log.error(f"[monitor] command error: {exc}")
        # exact channel (REST + bus commands publish to `fyers:command`)
        await self.bus.subscribe("fyers:command", handler=command_handler)

        hb = asyncio.create_task(self._heartbeat_loop())
        while not self._stop.is_set():
            if self.snapshot_fn is not None:
                t0 = time.perf_counter()
                snap = self.snapshot_fn()
                self._snapshot_buffer = snap
                self._publish_snapshot(snap)
                self.last_publish_ms = (time.perf_counter() - t0) * 1000
                self._mark()

            await self._check_market_close()

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        hb.cancel()

    # ------------------------------------------------------------------ market-close risk management
    async def _check_market_close(self):
        """
        Session-aware safety net: when the exchange session for this asset is
        about to end (or just ended), every position and resting order is
        flattened through the risk/execution chain at most once per IST date.
        """
        from app.infra import market_hours

        now_open = market_hours.is_open()
        secs_to_close = market_hours.seconds_until_close() if now_open else 0.0

        # roll the per-day latch
        today = market_hours.ist_date()
        if today != self._session_date:
            self._session_date = today
            self._flattened_on_session = False

        # keep the risk engine's market_hours constraint live even with no ticks
        risk = self._engines().get("risk")
        if risk is not None and hasattr(risk, "update_market_state"):
            risk.update_market_state(now_open)

        guarded = (
            (now_open and secs_to_close <= settings.close_flatten_seconds)
            or (self._was_open and not now_open)
        )
        if guarded and not self._flattened_on_session:
            await self._market_close_flatten(market_hours.session_label(), secs_to_close)
            self._flattened_on_session = True

        self._was_open = now_open

    async def _market_close_flatten(self, session_label: str, secs_to_close: float):
        self.close_events += 1
        log.warn(
            f"[monitor] market session ending ({session_label}; {secs_to_close:.0f}s to close) "
            f"— flattening all positions & canceling resting orders"
        )
        execu = self._engines().get("execution")
        risk = self._engines().get("risk")
        if risk is not None and hasattr(risk, "update_market_state"):
            risk.update_market_state(False)
        if execu is not None and hasattr(execu, "flatten_all"):
            try:
                await asyncio.wait_for(execu.flatten_all(), timeout=30)
            except asyncio.TimeoutError:
                log.error("[monitor] market-close flatten timed out")

    def _publish_snapshot(self, snap: schema.PipelineSnapshot):
        asyncio.create_task(self._publish_async(snap))

    async def _publish_async(self, snap: schema.PipelineSnapshot):
        try:
            await self.bus.publish(self.bus.channel_for("pipeline"), snap, ttl_sec=15)
        except Exception:
            pass