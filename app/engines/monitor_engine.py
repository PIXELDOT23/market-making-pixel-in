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
        self._publish_tasks: "set[asyncio.Task]" = set()
        self.close_events = 0

        # market-close wind-down latches: ONE per segment, per IST trading date.
        # Segment NSE (15:15 wind-down / 15:30 close) must square off its own
        # book while MCX keeps quoting into the night (and vice-versa).
        self._session_date: str = ""
        self._flattened_segments: Dict[str, str] = {}
        self._seg_was_open: Dict[str, bool] = {}

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
                try:
                    t0 = time.perf_counter()
                    snap = self.snapshot_fn()
                    self._snapshot_buffer = snap
                    self._publish_snapshot(snap)
                    self.last_publish_ms = (time.perf_counter() - t0) * 1000
                    self._mark()
                except Exception as exc:
                    log.error(f"[monitor] snapshot build failed: {exc!r}")

            try:
                await self._check_market_close()
            except Exception as exc:
                log.error(f"[monitor] market-close check failed: {exc!r}")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        hb.cancel()
        await self._shutdown_publish_tasks()

    async def _shutdown_publish_tasks(self):
        """Cancel and await every live snapshot-publish task so none is left
        pending when the event loop closes."""
        tasks = list(self._publish_tasks)
        self._publish_tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass

    # ------------------------------------------------------------------ market-close risk management
    async def _check_market_close(self):
        """
        Session-aware safety net that flattens EACH SEGMENT independently when
        its own session is about to end (wind-down) or has just ended.

        NSE closes at 15:30 IST: its wind-down (default 15 min -> 15:15) squares
        off the NSE book and cancels resting NSE orders while MCX keeps quoting
        into the night. MCX gets the same treatment near 23:30/23:55. A segment
        is flattened at most once per IST trading date.
        """
        from app.infra import market_hours

        statuses = market_hours.segment_status()
        now_open = market_hours.any_open()
        today = market_hours.ist_date()

        # roll the per-day latch
        if today != self._session_date:
            self._session_date = today
            self._flattened_segments = {}
            self._seg_was_open = {}

        # keep the risk engine's market_hours constraint live even with no ticks
        risk = self._engines().get("risk")
        if risk is not None and hasattr(risk, "update_market_state"):
            risk.update_market_state(now_open)

        for st in statuses:
            seg = st["segment"]
            if seg in self._flattened_segments:
                self._seg_was_open[seg] = st["open"]
                continue

            entering_winddown = (
                st["open"]
                and 0 < st["close_in_sec"] <= settings.close_winddown_seconds
            ) if settings.close_winddown_seconds > 0 else False
            just_closed = self._seg_was_open.get(seg, False) and not st["open"]

            if entering_winddown or just_closed:
                self._flattened_segments[seg] = today
                await self._mark_segment_close(seg, st["label"], st["close_in_sec"])

            self._seg_was_open[seg] = st["open"]

    async def _mark_segment_close(self, segment: str, session_label: str, secs_to_close: float):
        self.close_events += 1
        execu = self._engines().get("execution")
        if execu is None or not hasattr(execu, "flatten_segment"):
            log.warn(
                f"[monitor] {segment} session ending ({session_label}; {secs_to_close:.0f}s to close) "
                f"— flatten_segment unavailable, skipping"
            )
            return
        log.warn(
            f"[monitor] {segment} session wind-down ({session_label}; {secs_to_close:.0f}s to close) "
            f"— squaring off {segment} positions & canceling resting {segment} orders"
        )
        try:
            await asyncio.wait_for(execu.flatten_segment(segment), timeout=30)
        except asyncio.TimeoutError:
            log.error(f"[monitor] {segment} wind-down flatten timed out")

    def _publish_snapshot(self, snap: schema.PipelineSnapshot):
        def _consume(t: asyncio.Task):
            self._publish_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.error(f"[monitor] publish task error: {exc!r}")
        try:
            task = asyncio.create_task(self._publish_async(snap))
            self._publish_tasks.add(task)
            task.add_done_callback(_consume)
        except Exception:
            pass

    async def _publish_async(self, snap: schema.PipelineSnapshot):
        try:
            await self.bus.publish(self.bus.channel_for("pipeline"), snap, ttl_sec=15)
        except Exception:
            pass