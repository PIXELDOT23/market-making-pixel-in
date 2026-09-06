"""
app/infra/db.py
---------------
PostgreSQL persistence using psycopg (v3) with an async connection pool.

Values are serialized with msgspec into JSONB columns; writes are batched
and flushed on a timer so the hot path (ticks, signals) never blocks on a
network round-trip to Postgres.

Tables (see db/schema.sql):
  market_ticks, signals, cost_quotes, risk_verdicts, orders, order_events,
  decisions, engine_heartbeats, strategies
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import msgspec
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings
from app import schema


class Database:
    def __init__(self, dsn: str = ""):
        self.dsn = dsn or settings.database_url
        self.pool: Optional[AsyncConnectionPool] = None
        self._pending: Dict[str, List[tuple]] = {}   # table -> list of row tuples
        self._flush_task: Optional[asyncio.Task] = None
        self._encoder = msgspec.json.Encoder()
        self._decoder = msgspec.json.Decoder()
        self._lock = asyncio.Lock()

    @staticmethod
    def _ts(value: float) -> datetime:
        """Convert an epoch float to a timezone-aware UTC timestamp for TIMESTAMPTZ columns."""
        if isinstance(value, datetime):
            return value
        return datetime.fromtimestamp(value, tz=timezone.utc)

    # ------------------------------------------------------------------ lifecycle
    async def connect(self):
        if self.pool is None:
            self.pool = AsyncConnectionPool(self.dsn, min_size=1, max_size=8, open=False)
            await self.pool.open()
            self._flush_task = asyncio.create_task(self._periodic_flush())
        return self

    async def close(self):
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        if self.pool is not None:
            pool, self.pool = self.pool, None
            try:
                await pool.close()
            except Exception:
                pass

    async def ping(self) -> bool:
        try:
            async with self.pool.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ batching
    async def buffer_write(self, table: str, row: Dict[str, Any]):
        """Enqueue a row for the batched flush (low latency: never awaits a write)."""
        async with self._lock:
            self._pending.setdefault(table, []).append(row)

    async def flush(self) -> int:
        async with self._lock:
            pending = self._pending
            self._pending = {}
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
                        else:
                            await cur.executemany(
                                f"INSERT INTO {table} VALUES (%s)", rows
                            )
                        await conn.commit()
                total += len(rows)
            except Exception:
                # swallow persistence errors on the hot path; monitor sees the gap
                pass
        return total

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