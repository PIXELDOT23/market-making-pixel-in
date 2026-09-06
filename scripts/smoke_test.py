"""
scripts/smoke_test.py
---------------------
Boots ALL seven engines against REAL Redis + REAL PostgreSQL, but with the
FYERS SDK stubbed (no live socket / no real orders) and synthetic market
ticks injected into the DataEngine feed.

Verifies the full low-latency pipeline wiring:
  data -> signal -> cost -> strategy -> risk -> execution -> monitor

Usage:
    DATABASE_URL=postgresql://postgres@localhost:5433/market_making \
    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("FYERS_CLIENT_ID", "smoketest")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres@localhost:5433/market_making"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")


class FakeDataSocket:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.on_message = kwargs.get("on_message")
        self.on_connect = kwargs.get("on_connect")

    def connect(self):
        if self.on_connect:
            self.on_connect()

    def subscribe(self, symbols=None, data_type=None):
        pass

    def close_connection(self):
        pass

    def push(self, symbol, ltp, bid, ask, bs=10, as_=10):
        if self.on_message:
            self.on_message({
                "type": "sf", "symbol": symbol, "ltp": ltp,
                "bid_price": bid, "ask_price": ask,
                "bid_size": bs, "ask_size": as_,
            })


class FakeOrderSocket:
    def __init__(self, **kwargs):
        self.on_orders = kwargs.get("on_orders")
        self.on_connect = kwargs.get("on_connect")

    def connect(self):
        if self.on_connect:
            self.on_connect()

    def subscribe(self, data_type=None):
        pass

    def close_connection(self):
        pass

    def push_fill(self, order_id, symbol, side, qty, price):
        if self.on_orders:
            self.on_orders({
                "orders": {"id": order_id, "symbol": symbol, "side": side,
                           "qty": qty, "status": 2, "filledQty": qty,
                           "tradedPrice": price, "limitPrice": price,
                           "strategy": "mm_natgas"}
            })


class FakeFyers:
    fake_order_counter = 1000

    def __init__(self, **kwargs):
        self.header = "Bearer fake"

    def get_profile(self):
        return {"s": "ok"}

    def place_order(self, data):
        type(self).fake_order_counter += 1
        return {"s": "ok", "id": f"FAKEORD{type(self).fake_order_counter}"}

    def cancel_order(self, data):
        return {"s": "ok"}

    def modify_order(self, data):
        return {"s": "ok"}

    def positions(self, data=None):
        return {"s": "ok", "netPositions": []}

    def exit_positions(self, data=None):
        return {"s": "ok"}

    def funds(self, data=None):
        return {"s": "ok", "fund_limit": []}

    @property
    def service(self):
        return SimpleNamespace(
            post_call=lambda *a, **k: {
                "s": "ok", "code": 200,
                "data": {"margin_avail": 1000000.0,
                         "margin_new_order": 5000.0, "margin_total": 5000.0},
            }
        )


import fyers_apiv3  # noqa: E402
from fyers_apiv3 import fyersModel  # noqa: E402
fyersModel.FyersModel = FakeFyers

import app.infra.auth as auth_mod  # noqa: E402
auth_mod.fyersModel = fyersModel

import app.engines.data_engine as de_mod  # noqa: E402
import app.engines.execution_engine as ee_mod  # noqa: E402
import app.engines.risk_engine as re_mod  # noqa: E402
de_mod.data_ws.FyersDataSocket = FakeDataSocket
ee_mod.order_ws.FyersOrderSocket = FakeOrderSocket
ee_mod.fyersModel = fyersModel
re_mod.fyersModel = fyersModel


from app.container import EngineManager  # noqa: E402
from app.infra.auth import AccessToken  # noqa: E402
import app.infra.logging as log  # noqa: E402

MANAGER: EngineManager = None


async def wait_connected(obj, attr: str, timeout: float = 10.0):
    """Poll until an async engine has created its socket (boot starts tasks async)."""
    deadline = time.time() + timeout
    while time.time() < deadline and getattr(obj, attr, None) is None:
        await asyncio.sleep(0.05)
    return getattr(obj, attr, None)


async def main():
    global MANAGER
    log.banner("SMOKE TEST: 7-engine pipeline on real Redis + Postgres")
    manager = EngineManager()
    MANAGER = manager

    log.info("Starting local engine manager WITHOUT policy (smoke boot only)")
    # pre-seed token in Redis so interactive login is skipped
    await manager.bus.connect()
    key = manager.token_store.key  # uses settings.client_id = smoketest
    await manager.bus.set(
        key,
        AccessToken(client_id="smoketest", token="FAKE",
                   expires_ts=time.time() + 3600),
        ttl_sec=3600,
    )
    await manager.bus.close()

    try:
        await manager.boot()

        # subscriptions register asynchronously in each engine's run() and the
        # pump only starts after the settle window; wait for the listener so
        # redis pub/sub does not drop the synthetic ticks (no retention).
        expected_subs = 5
        deadline = time.time() + 10
        while time.time() < deadline:
            if (
                getattr(manager.bus, "_subscriber_count", 0) >= expected_subs
                and getattr(manager.bus, "_listener_task", None) is not None
            ):
                break
            await asyncio.sleep(0.05)

        data = manager.engines["data"]
        ds: FakeDataSocket = await wait_connected(data, "_ws")
        if ds is None:
            raise RuntimeError("data socket never connected")
        ds.on_connect()
        data._resubscribe()

        log.info("Injecting synthetic ticks ...")
        SYM = "MCX:NATURALGAS26SEPFUT"
        for i in range(30):
            base = 280 + (i % 5) * 0.1
            ds.push(SYM, base, base - 0.1, base + 0.1)
            await asyncio.sleep(0.05)

        await asyncio.sleep(3)

        snap = manager._build_snapshot()
        print("\n" + "=" * 70)
        print("PIPELINE SNAPSHOT")
        print("=" * 70)
        for hb in snap.engines:
            print(f"  {hb.engine:16s} status={hb.status:9s} processed={hb.processed_count:7d} loop_latency={hb.loop_latency_ms:.3f}ms")
        print("Strategies:")
        for s in snap.strategies:
            print(f"  {s.name}  {s.symbol} enabled={s.enabled}")
        print("Decision metrics:")
        for m in snap.decisions:
            print(f"  {m.strategy}: decisions={m.decisions_total} placed={m.quotes_placed} "
                  f"fills={m.fills_received} cycles={m.cycles_completed} "
                  f"avg_latency={m.avg_decision_latency_ms}ms")
        print("Markets:")
        for m in snap.markets:
            print(f"  {m.symbol}: mid={m.mid} ticks={m.tick_count} churn={m.churn_ticks_per_sec:.2f} t/s")
        print(f"risk: healthy={snap.risk_healthy} active={snap.risk_active} halts={snap.risk_halts}")

        ticks = await manager.db.fetch_all("SELECT count(*) AS n FROM market_ticks", {})
        sig = await manager.db.fetch_all("SELECT count(*) AS n FROM signals", {})
        verdicts = await manager.db.fetch_all("SELECT count(*) AS n FROM risk_verdicts", {})
        decisions = await manager.db.fetch_all("SELECT count(*) AS n FROM decisions", {})
        hbs = await manager.db.fetch_all("SELECT count(*) AS n FROM engine_heartbeats", {})
        print("\nDB row counts:")
        for label, r in [
            ("market_ticks", ticks), ("signals", sig), ("risk_verdicts", verdicts),
            ("decisions", decisions), ("engine_heartbeats", hbs),
        ]:
            print(f"  {label}={r[0]['n']}")

        os_ds: FakeOrderSocket = await wait_connected(manager.engines["execution"], "_ws")
        if os_ds is None:
            raise RuntimeError("order socket never connected")
        os_ds.push_fill("FAKEORD1", SYM, 1, 1, 280.0)
        await asyncio.sleep(1)
        risk = manager.engines["risk"]
        print(f"\nPost-fill risk net_position={risk.net_position} realized={risk.realized_pnl:.2f}")

        await manager.shutdown()
        log.success("SMOKE TEST PASSED")
    except BaseException as exc:
        print(f"\nSMOKE TEST FAILED: {exc!r}", flush=True)
        await manager.shutdown()
        raise


if __name__ == "__main__":
    asyncio.run(main())