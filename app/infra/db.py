"""
app/infra/db.py
---------------
PostgreSQL persistence using psycopg (v3) with an async connection pool.

Values are serialized with msgspec into JSONB columns; writes are batched
and flushed on a timer so the hot path (ticks, signals) never blocks on a
network round-trip to Postgres.

Tables (see db/schema.sql):
  market_ticks, signals, cost_quotes, risk_verdicts, orders, order_events,
  decisions, engine_heartbeats, strategies,
  order_book_depths, ml_features, ml_predictions (ML persistence)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import msgspec
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings
from app import schema

# psycopg_pool logs every "error connecting in 'pool-1'" retry at ERROR level.
# When Postgres is down we fully disable, but the few retries during connect()
# still hit the console; silence them so a DB-less boot is clean.
logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
logging.getLogger("psycopg.pool.AsyncConnectionPool").setLevel(logging.CRITICAL)


class _UnknownTableError(Exception):
    """Raised when a buffered table has no explicit INSERT handler in flush()."""

    def __init__(self, table: str):
        super().__init__(f"no INSERT handler for table {table!r}")
        self.table = table


class Database:
    def __init__(self, dsn: str = ""):
        self.dsn = dsn or settings.database_url
        self.pool: Optional[AsyncConnectionPool] = None
        self._pending: Dict[str, List[tuple]] = {}   # table -> list of row tuples
        self._flush_task: Optional[asyncio.Task] = None
        self._encoder = msgspec.json.Encoder()
        self._decoder = msgspec.json.Decoder()
        self._buf_lock = threading.Lock()
        self._lock = asyncio.Lock()
        # Guards the buffered-write state (_pending/_buffered/_dropped). A plain
        # threading.Lock (uncontended ~50ns) also lets synchronous, non-awaitable
        # producers enqueue rows (e.g. the ML engine's hot path) without ever
        # creating an un-awaited coroutine.
        # No-op mode: set when DSN is empty/unset or Postgres is unreachable.
        # In this mode every write is dropped and read queries return ``[]`` so
        # the whole app runs fine without persistence (zero boot/periodic noise).
        self._disabled = not self.dsn
        # Rows dropped because the buffered flush exceeded ``db_pending_cap``
        # (e.g. Postgres stays unreachable for a long time); tracked so the
        # warning is throttled instead of spamming the hot path.
        self._dropped = 0
        self._last_drop_warn_ts = 0.0
        self._logger = logging.getLogger("app.db")
        # Running total of buffered rows: maintained O(1) on every write and
        # resets on flush. Used to enforce ``db_pending_cap`` without re-walking
        # the whole buffer on the hot path.
        self._buffered = 0
        self._last_error_log_ts = 0.0

    @property
    def disabled(self) -> bool:
        return self._disabled or self.pool is None

    @staticmethod
    def _ts(value: float) -> datetime:
        """Convert an epoch float to a timezone-aware UTC timestamp for TIMESTAMPTZ columns."""
        if isinstance(value, datetime):
            return value
        return datetime.fromtimestamp(value, tz=timezone.utc)

    # ------------------------------------------------------------------ lifecycle
    async def connect(self):
        if self.pool is None and not self._disabled:
            pool = None
            try:
                pool = AsyncConnectionPool(
                    self.dsn, min_size=1, max_size=8, open=False, timeout=2
                )
                await asyncio.wait_for(pool.open(), timeout=5)
                # Connectivity probe: a psycopg_pool with open=False may return
                # before any connection succeeds and then retry in the
                # background forever, spamming "error connecting". If the very
                # first SELECT fails we disable completely (no retry noise).
                async with pool.connection(timeout=5) as conn:
                    await conn.execute("SELECT 1")
                self.pool = pool
                self._flush_task = asyncio.create_task(self._periodic_flush())
            except Exception:
                self._disabled = True
                if pool is not None:
                    try:
                        await pool.close()
                    except Exception:
                        pass
        return self

    async def close(self):
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        # Flush whatever is still buffered (e.g. the final order_events from a
        # flatten-on-close) so an orderly shutdown doesn't lose the tail.
        try:
            await self.flush()
        except Exception:
            pass
        if self.pool is not None:
            pool, self.pool = self.pool, None
            try:
                await pool.close()
            except Exception:
                pass

    async def ping(self) -> bool:
        if self.disabled:
            return False
        try:
            async with self.pool.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ batching
    def buffer_write_sync(self, table: str, row: Dict[str, Any]):
        """Synchronous enqueue (never awaits, never blocks on I/O).

        This is the safe way to enqueue from a synchronous producer such as the
        ML engine's hot path — calling the async buffer_write() there would
        create a coroutine that is never awaited and silently lose every row.

        If the buffer is already at ``db_pending_cap`` rows the write is dropped
        and counted; the flush-buffer can never grow unbounded and risk OOM even
        when Postgres is down for a long stretch. Dropping here is safe because
        the hot path (ticks/signals) is best-effort and the monitor reads back
        whatever the DB actually persisted."""
        if self.disabled:
            return
        with self._buf_lock:
            if self._buffered >= settings.db_pending_cap:
                self._dropped += 1
                now = time.time()
                if now - self._last_drop_warn_ts >= 30.0:
                    self._last_drop_warn_ts = now
                    self._logger.warning(
                        "db buffer at cap=%d — dropping row (total dropped: %d). "
                        "Postgres likely unreachable; flush will reset the counter.",
                        settings.db_pending_cap, self._dropped,
                    )
                return
            self._pending.setdefault(table, []).append(row)
            self._buffered += 1

    async def buffer_write(self, table: str, row: Dict[str, Any]):
        """Enqueue a row for the batched flush (low latency: never awaits a write).

        Enqueuing is pure in-memory work, so it delegates to the synchronous
        path; this async wrapper exists for the existing `await`-style callers."""
        self.buffer_write_sync(table, row)

    async def flush(self) -> int:
        if self.disabled:
            return 0
        with self._buf_lock:
            pending = self._pending
            self._pending = {}
            self._buffered = 0
            dropped = self._dropped
            self._dropped = 0
        if dropped:
            self._logger.warning(
                "flush recovered: %d buffered rows persisted; %d rows were dropped "
                "during the outage (buffer cap %d).",
                sum(len(rows) for rows in pending.values()), dropped,
                settings.db_pending_cap,
            )
        total = 0
        for table, rows in pending.items():
            if not rows:
                continue
            try:
                async with self.pool.connection() as conn:
                    async with conn.cursor() as cur:
                        if table == "market_ticks":
                            await cur.executemany(
                                "INSERT INTO market_ticks(ts,symbol,ltp,bid,ask,bid_size,ask_size,source) "
                                "VALUES(%(ts)s,%(symbol)s,%(ltp)s,%(bid)s,%(ask)s,%(bid_size)s,%(ask_size)s,%(source)s)",
                                rows,
                            )
                        elif table == "signals":
                            await cur.executemany(
                                "INSERT INTO signals(symbol,strategy,ts,metrics) "
                                "VALUES(%(symbol)s,%(strategy)s,%(ts)s,%(metrics)s::jsonb)",
                                rows,
                            )
                        elif table == "cost_quotes":
                            await cur.executemany(
                                "INSERT INTO cost_quotes(symbol,strategy,ts,quote) "
                                "VALUES(%(symbol)s,%(strategy)s,%(ts)s,%(quote)s::jsonb)",
                                rows,
                            )
                        elif table == "risk_verdicts":
                            await cur.executemany(
                                "INSERT INTO risk_verdicts(symbol,strategy,ts,verdict) "
                                "VALUES(%(symbol)s,%(strategy)s,%(ts)s,%(verdict)s::jsonb)",
                                rows,
                            )
                        elif table == "order_events":
                            await cur.executemany(
                                "INSERT INTO order_events(ts,broker_order_id,strategy,symbol,side,status,event) "
                                "VALUES(%(ts)s,%(broker_order_id)s,%(strategy)s,%(symbol)s,%(side)s,%(status)s,%(event)s::jsonb)",
                                rows,
                            )
                        elif table == "decisions":
                            await cur.executemany(
                                "INSERT INTO decisions(strategy,ts,metrics) "
                                "VALUES(%(strategy)s,%(ts)s,%(metrics)s::jsonb)",
                                rows,
                            )
                        elif table == "engine_heartbeats":
                            await cur.executemany(
                                "INSERT INTO engine_heartbeats(engine,ts,status,latency_ms,processed_count,heartbeat) "
                                "VALUES(%(engine)s,%(ts)s,%(status)s,%(latency_ms)s,%(processed_count)s,%(heartbeat)s::jsonb)",
                                rows,
                            )
                        elif table == "strategies":
                            # name is the PRIMARY KEY — last-writer-wins upsert of
                            # the live strategy registry.
                            await cur.executemany(
                                "INSERT INTO strategies(name,symbol,segment,enabled,started_ts,params) "
                                "VALUES(%(name)s,%(symbol)s,%(segment)s,%(enabled)s,%(started_ts)s,%(params)s::jsonb) "
                                "ON CONFLICT (name) DO UPDATE SET "
                                "symbol=EXCLUDED.symbol, segment=EXCLUDED.segment, "
                                "enabled=EXCLUDED.enabled, started_ts=EXCLUDED.started_ts, "
                                "params=EXCLUDED.params",
                                rows,
                            )
                        elif table == "order_book_depths":
                            await cur.executemany(
                                "INSERT INTO order_book_depths(ts,symbol,ltp,mid,spread,bids,asks,volume,churn_tps) "
                                "VALUES(%(ts)s,%(symbol)s,%(ltp)s,%(mid)s,%(spread)s,"
                                "%(bids)s::jsonb,%(asks)s::jsonb,%(volume)s,%(churn_tps)s)",
                                rows,
                            )
                        elif table == "ml_features":
                            await cur.executemany(
                                "INSERT INTO ml_features(ts,symbol,features,label,label_ts) "
                                "VALUES(%(ts)s,%(symbol)s,%(features)s::jsonb,%(label)s,%(label_ts)s)",
                                rows,
                            )
                        elif table == "ml_predictions":
                            await cur.executemany(
                                "INSERT INTO ml_predictions(ts,symbol,model_name,model_version,"
                                "features,prediction,action) "
                                "VALUES(%(ts)s,%(symbol)s,%(model_name)s,%(model_version)s,"
                                "%(features)s::jsonb,%(prediction)s,%(action)s)",
                                rows,
                            )
                        else:
                            # A buffer write landed for a table with no flush
                            # handler. Persisting it via a generic column-less
                            # INSERT would never work for dict rows (and dropping
                            # it silently hides a bug), so log it loudly and drop.
                            await conn.rollback()
                            raise _UnknownTableError(table)
                        await conn.commit()
                total += len(rows)
            except _UnknownTableError as exc:
                total += len(rows)  # nothing persisted — already dropped safely
                remaining = self._throttle_error()
                self._logger.warning(
                    "db flush: no INSERT handler for table %r — dropped %d row(s) "
                    "instead of writing invalid SQL (%s)",
                    exc.table, len(rows), remaining,
                )
            except Exception as exc:
                # Persistence is best-effort on the hot path: the monitor reads
                # back whatever actually landed, so we never raise and never
                # retry stale rows. Log throttled so a prolonged DB outage does
                # not flood the console.
                remaining = self._throttle_error()
                self._logger.warning(
                    "db flush to %r failed (%s) — dropped %d row(s); "
                    "next periodic flush will retry fresh rows%s",
                    table, type(exc).__name__, len(rows), remaining,
                )
        return total

    def _throttle_error(self) -> str:
        now = time.time()
        if now - self._last_error_log_ts >= 30.0:
            self._last_error_log_ts = now
            return ""
        return f" (last error logged {now - self._last_error_log_ts:.0f}s ago)"

    async def _periodic_flush(self):
        while True:
            await asyncio.sleep(settings.db_flush_interval_sec)
            try:
                await self.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------ queries (monitor / read side)
    async def fetch_all(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        if self.pool is None:
            return []
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(sql, params or {})
                    rows = await cur.fetchall()
            return rows
        except Exception:
            return []

    async def recent_ticks(self, symbol: str, limit: int = 500) -> List[Dict[str, Any]]:
        return await self.fetch_all(
            "SELECT ts,symbol,ltp,bid,ask FROM market_ticks WHERE symbol=%s ORDER BY ts DESC LIMIT %s",
            (symbol, limit),
        )

    async def recent_signals(self, symbol: str, limit: int = 200) -> List[Dict[str, Any]]:
        return await self.fetch_all(
            "SELECT ts,symbol,strategy,metrics FROM signals WHERE symbol=%s ORDER BY ts DESC LIMIT %s",
            (symbol, limit),
        )

    async def recent_decisions(self, limit: int = 200) -> List[Dict[str, Any]]:
        return await self.fetch_all(
            "SELECT ts,strategy,metrics FROM decisions ORDER BY ts DESC LIMIT %s", (limit,)
        )

    async def upsert_strategy(self, info: "schema.StrategyInfo"):
        await self.buffer_write("strategies", {
            "name": info.name, "symbol": info.symbol, "segment": info.segment,
            "enabled": info.enabled, "started_ts": self._ts(info.started_ts),
            "params": self._encoder.encode(info.params).decode(),
        })

    async def insert_market_tick(self, tick: "schema.MarketTick"):
        await self.buffer_write("market_ticks", {
            "ts": self._ts(tick.ts), "symbol": tick.symbol, "ltp": tick.ltp, "bid": tick.bid,
            "ask": tick.ask, "bid_size": tick.bid_size, "ask_size": tick.ask_size,
            "source": tick.source,
        })

    async def insert_signal(self, sig: "schema.SignalMetrics"):
        await self.buffer_write("signals", {
            "symbol": sig.symbol, "strategy": sig.strategy, "ts": self._ts(sig.ts),
            "metrics": self._encoder.encode(sig).decode(),
        })

    async def insert_cost_quote(self, cq: "schema.CostQuote"):
        await self.buffer_write("cost_quotes", {
            "symbol": cq.symbol, "strategy": cq.strategy, "ts": self._ts(cq.computed_at),
            "quote": self._encoder.encode(cq).decode(),
        })

    async def insert_risk_verdict(self, v: "schema.RiskVerdict"):
        await self.buffer_write("risk_verdicts", {
            "symbol": v.symbol, "strategy": v.strategy, "ts": self._ts(v.ts),
            "verdict": self._encoder.encode(v).decode(),
        })

    async def insert_order_event(self, ev: "schema.OrderEvent"):
        await self.buffer_write("order_events", {
            "ts": self._ts(ev.ts), "broker_order_id": ev.broker_order_id,
            "strategy": ev.strategy, "symbol": ev.symbol, "side": ev.side,
            "status": ev.status, "event": self._encoder.encode(ev).decode(),
        })

    async def insert_decision(self, decision: "schema.DecisionMetrics"):
        await self.buffer_write("decisions", {
            "strategy": decision.strategy, "ts": self._ts(decision.ts),
            "metrics": self._encoder.encode(decision).decode(),
        })

    async def insert_heartbeat(self, hb: "schema.EngineHeartbeat"):
        await self.buffer_write("engine_heartbeats", {
            "engine": hb.engine, "ts": self._ts(hb.ts), "status": hb.status,
            "latency_ms": hb.loop_latency_ms, "processed_count": hb.processed_count,
            "heartbeat": self._encoder.encode(hb).decode(),
        })

    async def insert_order_book_depth(self, depth: "schema.OrderBookDepth"):
        await self.buffer_write("order_book_depths", {
            "ts": self._ts(depth.ts), "symbol": depth.symbol, "ltp": depth.ltp,
            "mid": depth.mid, "spread": depth.spread,
            "bids": self._encoder.encode(depth.bids).decode() if depth.bids else "[]",
            "asks": self._encoder.encode(depth.asks).decode() if depth.asks else "[]",
            "volume": depth.volume, "churn_tps": depth.churn_tps,
        })

    async def insert_ml_prediction(self, pred: "schema.MLPrediction"):
        await self.buffer_write("ml_predictions", {
            "ts": self._ts(pred.ts), "symbol": pred.symbol,
            "model_name": pred.model_name, "model_version": pred.model_version,
            "features": "{}", "prediction": pred.prediction, "action": pred.action,
        })