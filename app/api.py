"""
app/api.py
----------
FastAPI gateway for the Monitor Engine. Serves:

  * REST: /api/pipeline (snapshot), /api/strategies, /api/markets,
          /api/orders, /api/decisions, /api/commands
  * WebSocket: /ws/live  - streams msgspec-encoded PipelineSnapshots to
    the Monitor frontend as they are published by the Monitor Engine.

Responses are JSON-encoded with msgspec (low latency, no pydantic overhead).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse

from app import schema
from app.config import settings
from app.container import EngineManager
from app.infra import market_hours
import app.infra.logging as log
import msgspec


def _session_open() -> bool:
    return market_hours.is_open()


def _session_label() -> str:
    return market_hours.session_label()


class MsgspecResponse(Response):
    """JSON response serialized with msgspec (fast path)."""

    media_type = "application/json"

    def __init__(self, content: Any, to: Optional[type] = None, **kwargs):
        self._encoder = msgspec.json.Encoder()
        self._payload = content
        super().__init__(content=b"", **kwargs)

    def render(self, content: Any) -> bytes:
        return self._encoder.encode(self._payload)


def create_app(manager: EngineManager, lifespan=None) -> FastAPI:
    app = FastAPI(title="Market-Making Pixel Monitor", version="2.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    flask_encoder = msgspec.json.Encoder()

    # ------------------------------------------------------------------ REST
    @app.get("/api/health")
    async def health():
        db_ok = await manager.db.ping() if manager.db else False
        return MsgspecResponse({
            "status": "ok" if manager.started else "booting",
            "engines": list(manager.engines.keys()),
            "postgres": db_ok,
            "session_open": _session_open(),
            "session_label": _session_label(),
        })

    @app.get("/api/auth")
    async def auth():
        from app.infra.auth import TokenStore

        ts = manager.token_store
        if ts is None or not isinstance(ts, TokenStore):
            return MsgspecResponse({"logged_in": False, "reason": "auth store unavailable"})
        return MsgspecResponse(await ts.live_status())

    @app.get("/api/pipeline")
    async def pipeline():
        monitor = manager.engines.get("monitor")
        snap = monitor.latest_snapshot() if monitor else None
        return MsgspecResponse(snap or manager._build_snapshot())

    @app.get("/api/strategies")
    async def strategies():
        se = manager.engines.get("strategy")
        return MsgspecResponse(se.strategy_list() if se else [])

    @app.get("/api/decisions")
    async def decisions():
        se = manager.engines.get("strategy")
        return MsgspecResponse(se.strategy_metrics() if se else [])

    @app.get("/api/markets")
    async def markets():
        de = manager.engines.get("data")
        out = [de.snapshot(s) for s in de.symbols] if de else []
        return MsgspecResponse([s for s in out if s is not None])

    @app.get("/api/risk")
    async def risk():
        re = manager.engines.get("risk")
        if re is None:
            return MsgspecResponse({})
        return MsgspecResponse({
            "halts": re.halt_reasons,
            "net_position": re.net_position,
            "realized_pnl_rs": round(re.realized_pnl, 2),
            "status": re.status,
            "constraints": [
                {"name": c.name, "healthy": c.healthy, "detail": c.detail}
                for c in re._constraints.values()
            ],
        })

    @app.get("/api/orders")
    async def orders():
        ee = manager.engines.get("execution")
        if ee is None:
            return MsgspecResponse([])
        return MsgspecResponse(list(ee._orders.values())[-100:])

    @app.get("/api/db/top-tables")
    async def db_top_tables():
        rows = await manager.db.fetch_all(
            "SELECT 'market_ticks' AS t, count(*) FROM market_ticks "
            "UNION ALL SELECT 'order_events', count(*) FROM order_events "
            "UNION ALL SELECT 'signals', count(*) FROM signals "
            "UNION ALL SELECT 'decisions', count(*) FROM decisions "
            "UNION ALL SELECT 'engine_heartbeats', count(*) FROM engine_heartbeats"
        ) if manager.db else []
        return JSONResponse(rows)

    @app.post("/api/commands/{command}")
    async def send_command(command: str, target: str = "*"):
        monitor = manager.engines.get("monitor")
        if monitor is None:
            return JSONResponse({"error": "monitor not running"}, status_code=503)
        cmd = schema.Command(type=command.upper(), target=target)
        result = await monitor.handle_command(cmd)
        return JSONResponse({"command": cmd.type, "target": target, "result": result})

    # ------------------------------------------------------------------ websocket
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws.accept()
        monitor = manager.engines.get("monitor")
        if monitor is None:
            await ws.close(code=1011)
            return
        while True:
            snap = monitor.latest_snapshot()
            if snap is not None:
                try:
                    await ws.send_text(flask_encoder.encode(snap).decode())
                except (WebSocketDisconnect, RuntimeError):
                    break
            await asyncio.sleep(1.0)

    @app.websocket("/ws/trades")
    async def ws_trades(ws: WebSocket):
        await ws.accept()
        bus = manager.bus

        async def forward(ch: str, raw: bytes):
            try:
                ev = schema.decode(schema.OrderEvent, raw)
                await ws.send_text(flask_encoder.encode(ev).decode())
            except Exception:
                pass

        await bus.subscribe("fyers:orders", handler=forward)
        try:
            while True:
                await asyncio.sleep(0.5)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            try:
                await bus.unsubscribe("fyers:orders", forward)
            except Exception:
                pass

    return app


def main_run(manager: EngineManager):
    import uvicorn
    uvicorn.run(create_app(manager), host=settings.api_host, port=settings.api_port)


async def boot_and_run(manager: EngineManager):
    await manager.boot()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        yield
        # uvicorn runs this on SIGINT/SIGTERM BEFORE it tears down the event
        # loop. The loop is still live here — the ONLY place we can safely
        # flatten/close the book. Once serve() returns, the loop is left in a
        # wedged state where `await` (+ timeouts) never resumes.
        await manager.emergency_flatten()

    import uvicorn
    app = create_app(manager, lifespan=_lifespan)
    config = uvicorn.Config(app, host=settings.api_host, port=settings.api_port)
    server = uvicorn.Server(config)
    try:
        await server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        # Loop is wedged after serve() — never await here. run.py force-exits.
        log.warn("[shutdown] serve returned; forced teardown by runner")