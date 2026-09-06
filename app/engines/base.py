"""
app/engines/base.py
-------------------
Base class shared by all seven engines.

An Engine:
  * runs as an asyncio task (single event loop -> low latency, no locks)
  * beats a monitor heartbeat at a fixed interval
  * exposes a typed publish() to the Redis message bus
  * records processed-count + loop-latency metrics used by Monitor
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Optional, Set

import msgspec
from app import schema
from app.config import settings
from app.infra.redis import RedisBus, ObservableTickBus
from app.infra.db import Database


class Engine(ABC):
    name: str = "engine"

    def __init__(
        self,
        bus: RedisBus,
        db: Database,
        heartbeat_sec: float = 0.0,
    ):
        self.bus = bus
        self.obs = ObservableTickBus(bus)
        self.db = db
        self.heartbeat_sec = heartbeat_sec or settings.engine_heartbeat_sec
        self.started_ts = time.time()
        self.processed_count = 0
        self.status = "healthy"
        self.detail = ""
        self._loop_latency_ms = 0.0
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle
    async def start(self):
        self._task = asyncio.create_task(self._run_wrapper(), name=f"engine:{self.name}")
        return self._task

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_wrapper(self):
        try:
            await self.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover
            self.status = "halted"
            self.detail = str(exc)
            await self.obs.publish_heartbeat(self.heartbeat())
            raise

    @abstractmethod
    async def run(self):
        """Main engine loop."""

    # ------------------------------------------------------------------ metrics / heartbeat
    def _mark(self):
        self.processed_count += 1

    def heartbeat(self, latency_ms: float = 0.0) -> "schema.EngineHeartbeat":
        return schema.EngineHeartbeat(
            engine=self.name,
            ts=time.time(),
            status=self.status,
            process_uptime_sec=time.time() - self.started_ts,
            loop_latency_ms=latency_ms or self._loop_latency_ms,
            processed_count=self.processed_count,
            detail=self.detail,
        )

    async def _heartbeat_loop(self):
        while not self._stop.is_set():
            t0 = time.perf_counter()
            await self.obs.publish_heartbeat(self.heartbeat())
            await self.db.insert_heartbeat(self.heartbeat())
            self._loop_latency_ms = (time.perf_counter() - t0) * 1000
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_sec)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def _supervise(self, engine_task: asyncio.Task):
        hb_task = asyncio.create_task(self._heartbeat_loop())
        try:
            await engine_task
        finally:
            hb_task.cancel()