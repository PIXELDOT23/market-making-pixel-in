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


class _HandlerSlot:
    """One registered (channel/pattern, handler) — owns a dedicated FIFO queue
    and worker task so a slow consumer can never stall the bus pump.

    Each subscription gets its own bounded queue: the pump only enqueues
    (never awaits a handler), and if a consumer falls behind it drops the
    NEWEST messages with a throttled warning instead of blocking Redis reads.
    Per-slot ordering is strict FIFO; different subscriptions run
    independently, which is what keeps one slow engine from starving the feed."""

    __slots__ = ("handler", "queue", "task", "dropped", "_warn_ts")

    def __init__(self, handler, queue_size: int):
        self.handler = handler
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.task: Optional[asyncio.Task] = None
        self.dropped = 0
        self._warn_ts = 0.0

    def enqueue(self, channel: str, raw: bytes):
        """Pump-side enqueue: never blocks, never awaits. Overflows drop-newest."""
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._drain(channel))
        try:
            self.queue.put_nowait((channel, raw))
        except asyncio.QueueFull:
            self.dropped += 1
            now = time.time()
            if now - self._warn_ts >= 10.0:
                self._warn_ts = now
                log.warn(
                    f"bus handler overloaded on {channel!r} — dropping newest "
                    f"(total drops={self.dropped}, queue cap={self.queue.maxsize}); "
                    f"a consumer is too slow to keep up"
                )

    async def _drain(self, channel: str):
        while True:
            ch, raw = await self.queue.get()
            try:
                result = self.handler(ch, raw)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error(f"bus handler error on {ch!r}: {exc!r}")

    def cancel(self):
        if self.task is not None:
            self.task.cancel()
            self.task = None


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
            # redis-py caps the async pool at max_connections (default 100) and
            # raises MaxConnectionsError instead of waiting when it is exhausted.
            # Use a high cap and keep publish concurrency bounded so transient
            # fan-out bursts can never turn a busy pipeline into 500s.
            self._client = aioredis.from_url(
                self.url, decode_responses=False, health_check_interval=30, max_connections=256
            )
            await self._client.ping()
            self._loop = asyncio.get_running_loop()
        return self

    async def close(self):
        if self._listener_task:
            self._listener_task.cancel()
            task = self._listener_task
            self._listener_task = None
            try:
                await task
            except BaseException:
                pass
        if self._settle_task:
            self._settle_task.cancel()
            task = self._settle_task
            self._settle_task = None
            try:
                await task
            except BaseException:
                pass
        # cancel every subscription worker (drains nothing — bounded drop)
        for slots in list(self._handlers.values()):
            for slot in list(slots):
                slot.cancel()
        self._handlers.clear()
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
        every registered handler is invoked for each message, each on its own
        bounded worker (see `_HandlerSlot`).

        All subscriptions made during a boot (or any quick burst) are BATCHED
        and issued to Redis in one go, and the pump listener only starts after a
        short settle window. Starting `listen()` while a concurrent task is still
        mid-`psubscribe` on the same PubSub connection can corrupt the protocol
        and silently kill the pump, so we never touch the connection while it may
        be reading.
        """
        for ch in channels:
            self._handlers.setdefault(ch, []).append(_HandlerSlot(
                handler, max(1, settings.bus_handler_queue_size)
            ))
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
        The slot's worker task is cancelled so a closed connection stops
        consuming (and never leaves a stalled worker behind).
        """
        slots = self._handlers.get(channel)
        if not slots:
            return
        for slot in list(slots):
            if slot.handler == handler:
                slot.cancel()
                slots.remove(slot)
        if not slots:
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
        # Re-issue EVERY registered channel (not just pending ones) so a
        # reconnect can restore the full exact+pattern subscription set even
        # though the initial settle already cleared `_pending_channels`.
        channels = set(self._handlers) | self._pending_channels
        if not channels or self._pubsub is None:
            return
        exact = [c for c in channels if "*" not in c]
        patterns = [c for c in channels if "*" in c]
        try:
            if exact:
                await self._pubsub.subscribe(*exact)
            if patterns:
                await self._pubsub.psubscribe(*patterns)
            self._pending_channels.clear()
        except Exception as exc:
            # publish-side failure is fine: channels stay in `_handlers` and
            # the pump's reconnect loop re-issues the full set on its own.
            log.warn(f"bus subscribe deferred (will re-issue on reconnect): {exc!r}")

    async def _reconnect_pubsub(self):
        """Torn-down Redis conn: close the dead PubSub, open a fresh one and
        re-issue the complete registered subscription set."""
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
        self._pubsub = self._client.pubsub()
        await self._issue_subscriptions()
        log.warn(
            f"bus pump reconnected and resubscribed "
            f"({len(self._handlers)} channel(s))"
        )

    async def _pump(self):
        """Drive the pub/sub reader forever. A transient network error (internet
        drop, Redis restart) must never kill the bus permanently: the listener
        is torn down, the connection retried with capped exponential backoff,
        and the full subscription set restored before listening resumes."""
        assert self._pubsub is not None
        backoff = 1.0
        while True:
            try:
                async for message in self._pubsub.listen():
                    backoff = 1.0  # healthy traffic resets the backoff
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
                        slots = self._handlers.get(channel) or self._handlers.get(pattern) or []
                    else:
                        slots = self._handlers.get(channel) or []
                    if not slots:
                        log.warn(f"no bus handler for {channel!r} ({type_})")
                        continue
                    # NEVER await a handler on the pump. Enqueue to each slot's
                    # bounded worker queue; a slow consumer drops-newest and gets
                    # a throttled warning instead of stalling Redis reads.
                    for slot in slots:
                        slot.enqueue(channel, raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                dbg = getattr(exc, "args", None)
                log.error(f"bus pump connection lost ({exc!r}) — reconnecting in {backoff:.0f}s")
                try:
                    await asyncio.sleep(backoff)
                    await self._client.ping()
                    await self._reconnect_pubsub()
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc2:
                    log.error(f"bus pump reconnect attempt failed ({exc2!r})")
                    backoff = min(backoff * 2, 60.0)


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