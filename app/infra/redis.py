"""
app/infra/redis.py
------------------
Redis-backed:
  1. Cache store  (get/set with TTL, msgspec-encoded values)
  2. Pub/Sub bus  (async, low-latency inter-engine + engine<->monitor)

Every engine publishes its msgspec Structs here so downstream engines and
the Monitor engine observe the whole pipeline without HTTP polling.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional, Set

import redis.asyncio as aioredis
from redis.asyncio.client import PubSub

from app import schema
from app.config import settings
from app.infra import logging as log


class RedisBus:
    def __init__(self, url: str = "", decode_responses: bool = False):
        self.url = url or settings.redis_url
        self._encode = False  # we always operate on raw bytes for msgspec
        self._client: Optional[aioredis.Redis] = None
        self._pubsub: Optional[PubSub] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscriber_count = 0
        self._handlers: dict[str, list] = {}
        self._pending_channels: Set[str] = set()
        self._listener_task: Optional[asyncio.Task] = None
        self._settle_task: Optional[asyncio.Task] = None
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    async def connect(self):
        if self._client is None:
            self._client = aioredis.from_url(
                self.url, decode_responses=False, health_check_interval=30
            )
            await self._client.ping()
            self._loop = asyncio.get_running_loop()
        return self

    async def close(self):
        if self._listener_task:
            self._listener_task.cancel()
            self._listener_task = None
        if self._settle_task:
            self._settle_task.cancel()
            self._settle_task = None
        if self._pubsub:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ------------------------------------------------------------------ cache
    async def set(self, key: str, value: Any, ttl_sec: Optional[float] = None) -> bool:
        payload = schema.encode(value) if not isinstance(value, (bytes, bytearray)) else value
        return bool(await self._client.set(key, payload, ex=int(ttl_sec) if ttl_sec else None))

    async def get(self, key: str, cls: Optional[type] = None) -> Any:
        raw = await self._client.get(key)
        if raw is None:
            return None
        return schema.decode(cls, raw) if cls else raw

    async def delete(self, key: str) -> int:
        return int(await self._client.delete(key))

    async def expire(self, key: str, ttl_sec: float) -> bool:
        return bool(await self._client.expire(key, int(ttl_sec)))

    async def ttl(self, key: str) -> int:
        return int(await self._client.ttl(key))

    # ------------------------------------------------------------------ pub/sub bus
    def channel_for(self, kind: str, target: str = "") -> str:
        return f"fyers:{kind}:{target}".rstrip(":")

    async def publish_bytes(self, channel: str, payload: bytes) -> int:
        return int(await self._client.publish(channel, payload))

    async def publish(self, channel: str, msg, ttl_sec: Optional[float] = None):
        raw = schema.encode(msg)
        n = int(await self._client.publish(channel, raw))
        if n == 0 and ttl_sec is not None:
            # no live subscriber: cache the latest payload so a late subscriber
            # can catch up from the "latest" key.
            await self._client.set(f"{channel}:latest", raw, ex=int(ttl_sec))
        return n

    async def latest(self, channel: str, cls) -> Any:
        raw = await self._client.get(f"{channel}:latest")
        return schema.decode(cls, raw) if raw else None

    async def subscribe(self, *channels: str, handler):
        """
        Subscribe to exact channels, or to Redis glob patterns when a channel
        contains `*`. Multiple handlers may subscribe to the same channel —
        every registered handler is invoked for each message.

        All subscriptions made during a boot (or any quick burst) are BATCHED
        and issued to Redis in one go, and the pump listener only starts after a
        short settle window. Starting `listen()` while a concurrent task is still
        mid-`psubscribe` on the same PubSub connection can corrupt the protocol
        and silently kill the pump, so we never touch the connection while it may
        be reading.
        """
        for ch in channels:
            self._handlers.setdefault(ch, []).append(handler)
            self._pending_channels.add(ch)
        self._subscriber_count += 1
        if self._listener_task:
            # pump already reading; only the command gets re-issued now.
            await self._issue_subscriptions()
        elif not self._settle_task:
            self._settle_task = asyncio.create_task(self._settle_and_start())

    async def unsubscribe(self, channel: str, handler):
        """
        Remove one handler from a channel. Used e.g. by WebSocket endpoints so
        every client connection only receives messages while it is connected.
        """
        handlers = self._handlers.get(channel)
        if not handlers:
            return
        if handler in handlers:
            handlers.remove(handler)
        if not handlers:
            self._handlers.pop(channel, None)

    async def _settle_and_start(self):
        try:
            await asyncio.sleep(0.05)
            if self._listener_task or not self._pending_channels:
                return
            if self._pubsub is None:
                self._pubsub = self._client.pubsub()
            await self._issue_subscriptions()
            self._listener_task = asyncio.create_task(self._pump())
        finally:
            self._settle_task = None

    async def _issue_subscriptions(self):
        if not self._pending_channels:
            return
        exact = [c for c in self._pending_channels if "*" not in c]
        patterns = [c for c in self._pending_channels if "*" in c]
        if exact:
            await self._pubsub.subscribe(*exact)
        if patterns:
            await self._pubsub.psubscribe(*patterns)
        self._pending_channels.clear()

    async def _pump(self):
        assert self._pubsub is not None
        try:
            async for message in self._pubsub.listen():
                type_ = message.get("type")
                if not isinstance(type_, str):
                    type_ = type_.decode() if type_ else type_
                if type_ not in ("message", "pmessage"):
                    continue
                raw = message.get("data")
                if raw is None:
                    continue
                channel = message.get("channel")
                if isinstance(channel, bytes):
                    channel = channel.decode()
                if type_ == "pmessage":
                    pattern = message.get("pattern", "*")
                    if isinstance(pattern, bytes):
                        pattern = pattern.decode()
                    handlers = self._handlers.get(channel) or self._handlers.get(pattern) or []
                else:
                    handlers = self._handlers.get(channel) or []
                if not handlers:
                    log.warn(f"no bus handler for {channel!r} ({type_})")
                for handler in handlers:
                    try:
                        result = handler(channel, raw)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        log.error(f"bus handler error on {channel!r}: {exc!r}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(f"bus pump stopped unexpectedly: {exc!r}")
            self._listener_task = None


class ObservableTickBus:
    """Small typed helper on top of RedisBus tying struct -> channel."""

    def __init__(self, bus: RedisBus):
        self.bus = bus

    async def publish_market(self, tick: "schema.MarketTick"):
        await self.bus.publish(self.bus.channel_for("market", tick.symbol), tick, ttl_sec=5)

    async def publish_signal(self, sig: "schema.SignalMetrics"):
        await self.bus.publish(self.bus.channel_for("signal", sig.symbol), sig, ttl_sec=60)

    async def publish_decision(self, decision: "schema.DecisionMetrics"):
        await self.bus.publish(self.bus.channel_for("decision", decision.strategy), decision, ttl_sec=600)

    async def publish_order(self, ev: "schema.OrderEvent"):
        await self.bus.publish(self.bus.channel_for("orders"), ev, ttl_sec=600)

    async def publish_heartbeat(self, hb: "schema.EngineHeartbeat"):
        await self.bus.publish(self.bus.channel_for("heartbeats"), hb, ttl_sec=15)

    async def publish_verdict(self, v: "schema.RiskVerdict"):
        await self.bus.publish(self.bus.channel_for("risk", v.strategy), v, ttl_sec=60)

    async def publish_command(self, cmd: "schema.Command"):
        await self.bus.publish(self.bus.channel_for("command"), cmd, ttl_sec=60)