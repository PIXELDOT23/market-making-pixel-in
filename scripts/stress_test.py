"""
scripts/stress_test.py
----------------------
Destructive/latency stress test for the data + execution engines. No
PostgreSQL required (Database is stubbed in-memory); uses real Redis; FYERS
SDK is stubbed with fake sockets. Designed specifically to prove the crash
fix for `asyncio.queues.QueueFull`:

  * floods the data feed from a REAL background thread (as the FYERS SDK does)
    plus a synchronous loop-thread burst, both far exceeding the bounded
    queue capacity (DATA_FEED_BUFFER=2000)
  * floods the order socket with 10k fills (> execution queue capacity 5000)
  * asserts drop-oldest backpressure invariants hold, the NEWEST tick survives,
    queues fully drain, fan-out reaches Redis, and zero exceptions leak out of
    loop callbacks.

Usage:
    REDIS_URL=redis://localhost:6379/1 DATA_FEED_BUFFER=2000 \
    python scripts/stress_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("FYERS_CLIENT_ID", "stress")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("DATA_FEED_BUFFER", "2000")


class StubTokenStore:
    client_id = "stress"

    async def ensure_valid(self) -> str:
        return "FAKE"


class StubDB:
    def __init__(self):
        self.writes = []
        # ML buffer rows recorded by ml/engine (buffer_write_sync path)
        self.buf = {"order_book_depths": [], "ml_features": [], "ml_predictions": []}
        self.disabled = False
        import msgspec
        self._encoder = msgspec.json.Encoder()

    @staticmethod
    def _ts(value):
        from datetime import datetime, timezone
        if isinstance(value, datetime):
            return value
        return datetime.fromtimestamp(value, tz=timezone.utc)

    async def insert_market_tick(self, tick):
        self.writes.append(("market_ticks", tick.symbol, tick.ltp))

    async def insert_order_event(self, ev):
        self.writes.append(("order_events", ev.broker_order_id, ev.status))

    async def insert_heartbeat(self, hb):
        pass

    async def buffer_write(self, table, row):
        self.buf.setdefault(table, []).append(row)

    def buffer_write_sync(self, table, row):
        self.buf.setdefault(table, []).append(row)


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

    def push(self, symbol, ltp, bid, ask, bs=10, as_=10, with_depth=False):
        if self.on_message:
            msg = {
                "type": "sf", "symbol": symbol, "ltp": ltp,
                "bid_price": bid, "ask_price": ask,
                "bid_size": bs, "ask_size": as_,
            }
            if with_depth:
                msg["depth"] = {
                    "bid": [{"price": bid, "qty": bs, "orders": 1}],
                    "ask": [{"price": ask, "qty": as_, "orders": 1}],
                }
            self.on_message(msg)


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

    def _push(self, oid, status):
        if self.on_orders:
            self.on_orders({
                "orders": {"id": oid, "symbol": "MCX:NATURALGAS26SEPFUT",
                           "side": 1, "qty": 1, "status": status,
                           "filledQty": 1 if status == 2 else 0,
                           "tradedPrice": 280.0, "limitPrice": 280.0,
                           "strategy": "mm_stress"}
            })

    def push_fill(self, oid):
        self._push(oid, 2)

    def push_reject(self, oid):
        self._push(oid, 5)


class FakeFyers:
    def __init__(self, **kwargs):
        self.header = "Bearer fake"

    def get_profile(self):
        return {"s": "ok"}


import fyers_apiv3  # noqa: E402
from fyers_apiv3 import fyersModel  # noqa: E402
fyersModel.FyersModel = FakeFyers

import app.engines.data_engine as de_mod  # noqa: E402
import app.engines.execution_engine as ee_mod  # noqa: E402
de_mod.data_ws.FyersDataSocket = FakeDataSocket
ee_mod.order_ws.FyersOrderSocket = FakeOrderSocket
ee_mod.fyersModel = fyersModel

from app.engines.data_engine import DataEngine  # noqa: E402
from app.engines.execution_engine import ExecutionEngine  # noqa: E402
from app.config import settings  # noqa: E402
from app.infra.redis import RedisBus  # noqa: E402
from app import schema  # noqa: E402
import app.infra.logging as log  # noqa: E402

SYM = "MCX:NATURALGAS26SEPFUT"
THREAD_BURST = 25_000
LOOP_BURST = 25_000
FILLS_BURST = 10_000
N_SYMBOLS = 300

loop_errors: list = []
conn_gauge = {"cur": 0, "max": 0}


def _exception_handler(loop, context):
    loop_errors.append(context)
    err = context.get("exception")
    log.error(f"[stress] unhandled loop exception: {context.get('message', '?')} {err!r}")


async def wait_connected(obj, attr: str, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline and getattr(obj, attr, None) is None:
        await asyncio.sleep(0.02)
    return getattr(obj, attr, None)


async def wait_idle(data, exec_):
    deadline = time.time() + 60
    while time.time() < deadline:
        idle = (
            data._queue.qsize() == 0
            and exec_._queue.qsize() == 0
            and not data._fanout_active
            and not data._fanout_pending
        )
        if idle:
            await asyncio.sleep(0.05)
            if (
                data._queue.qsize() == 0
                and exec_._queue.qsize() == 0
                and not data._fanout_active
                and not data._fanout_pending
            ):
                return
        await asyncio.sleep(0.02)
    raise RuntimeError(f"timed out waiting for idle: data.q={data._queue.qsize()} "
                       f"exec.q={exec_._queue.qsize()} active={data._fanout_active}")


async def wait_for_ltp(ltp: float, received_list, symbol: str = SYM, timeout: float = 6.0) -> bool:
    """PubSub delivery is async: the fan-out engine marks a tick published the
    moment Redis ACKs it, but the pump still has to hand it to our handler.
    Wait (briefly) so the final-ltp assertion isn't a race."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(t.symbol == symbol and t.ltp == ltp for t in received_list):
            return True
        await asyncio.sleep(0.05)
    return False


async def wait_for_symbols(target_symbols, received_list, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        have = {t.symbol for t in received_list}
        if set(target_symbols) <= have:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("timed out waiting for fan-out delivery of all symbols")


def _thread_produce(ws, n, base, results):
    try:
        for i in range(n):
            px = base + (i % 7) * 0.1
            ws.push(SYM, px, px - 0.1, px + 0.1, with_depth=(i % 5 == 0))
        results["ok"] = True
    except BaseException as exc:
        results["exc"] = exc


def _reset_ml_state(ml_engine, ml_model):
    """Restore ML module globals to a clean slate (stress phases mutate them)."""
    ml_engine._registered.clear()
    ml_engine._mid_history.clear()
    ml_engine._spread_history.clear()
    ml_engine._depth_snapshot_ts.clear()
    ml_engine._feature_persist_ts.clear()
    ml_engine._prediction_persist_ts.clear()
    ml_model.unload_model("dummy")
    ml_engine.configure(db=None, enabled=False, capture_enabled=False)


async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_exception_handler)

    log.banner("STRESS TEST: data + execution engines under QueueFull overload")
    bus = RedisBus(os.environ["REDIS_URL"])
    await bus.connect()
    db = StubDB()
    tok = StubTokenStore()

    data = DataEngine(bus, db, tok)
    exec_ = ExecutionEngine(bus, db, tok)

    received: list = []

    async def market_handler(ch: str, raw: bytes):
        received.append(schema.decode(schema.MarketTick, raw))

    await bus.subscribe("fyers:market:*", handler=market_handler)

    # gauge how many Redis commands are in flight at any instant — proves the
    # fan-out semaphore keeps pool usage well under redis-py's max_connections
    # cap (default 100) even when a whole market is churning.
    _orig_publish = bus.publish

    async def _gauged_publish(channel, msg, ttl_sec=None):
        conn_gauge["cur"] += 1
        conn_gauge["max"] = max(conn_gauge["max"], conn_gauge["cur"])
        try:
            return await _orig_publish(channel, msg, ttl_sec)
        finally:
            conn_gauge["cur"] -= 1
    bus.publish = _gauged_publish

    data.register_symbols([SYM])
    data.set_tick_sizes({SYM: 0.1})

    try:
        await data.start()
        await exec_.start()

        ds: FakeDataSocket = await wait_connected(data, "_ws")
        os_: FakeOrderSocket = await wait_connected(exec_, "_ws")
        if ds is None or os_ is None:
            raise RuntimeError("engines never reached socket connect")

        # give the exec engine's bus subscription time to settle
        await asyncio.sleep(0.3)

        final_ltp = 0.0

        log.info(f"Phase A: real-thread burst of {THREAD_BURST} ticks "
                 f"(data cap {settings.data_feed_buffer}) ...")
        base = 280.0
        results: dict = {}
        t = threading.Thread(target=_thread_produce, args=(ds, THREAD_BURST, base, results))
        t.start()
        t.join()
        if results.get("exc"):
            raise results["exc"]
        final_ltp = base + (THREAD_BURST - 1) % 7 * 0.1
        log.info(f"Phase A done: enqueued={data._enqueued_total} drops={data.dropped_ticks} "
                 f"ingested={data.ingested_total}")
        await wait_idle(data, exec_)

        log.info(f"Phase B: loop-thread burst of {LOOP_BURST} ticks (no interleaved awaits) ...")
        base = 500.0
        for i in range(LOOP_BURST):
            px = base + (i % 7) * 0.1
            ds.push(SYM, px, px - 0.1, px + 0.1, with_depth=True)
        final_ltp = base + (LOOP_BURST - 1) % 7 * 0.1
        log.info(f"Phase B done: enqueued={data._enqueued_total} drops={data.dropped_ticks} "
                 f"ingested={data.ingested_total}")
        await wait_idle(data, exec_)

        total_pushed = THREAD_BURST + LOOP_BURST
        snap = data.snapshot(SYM)

        data_checks = {
            "enqueued == total pushed": data._enqueued_total == total_pushed,
            "enqueued == dropped + ingested (+remaining q)":
                data._enqueued_total == data.dropped_ticks + data.ingested_total + data._queue.qsize(),
            "queue drained": data._queue.qsize() == 0,
            "drops were taken": data.dropped_ticks > 0,
            "newest tick survived": snap is not None and snap.ltp == final_ltp,
            "ingest consumed accepted ticks": 0 < data.ingested_total <= total_pushed,
            "fan-out actively published": data.fanout_published > 0,
            "engine healthy": data.status not in ("halted",),
        }
        market_received = [t for t in received if t.symbol == SYM]
        # The overload loop MUST drop ticks by design ("drops were taken"), so
        # the newest push is not guaranteed to survive ingest. What fan-out
        # coalescing (latest pending tick per symbol) does guarantee is that the
        # final price reaches the bus at least once — check that, not the
        # newest-tick equality, which is a race when drops are deliberately forced.
        data_checks["Redis fan-out carried the final tick price"] = (
            bool(market_received) and await wait_for_ltp(final_ltp, received)
        )

        log.info("DATA ENGINE OVERLOAD RESULTS:")
        for k, v in data_checks.items():
            log.info(f"  [{'PASS' if v else 'FAIL'}] {k}")
        for k, v in data_checks.items():
            if not v:
                raise AssertionError(f"data check failed: {k}")

        log.info(f"Phase C: order-event flood of {FILLS_BURST} fills "
                 f"(exec cap 5000) ...")
        for i in range(FILLS_BURST):
            os_.push_fill(f"STRESS{i}")
        await wait_idle(data, exec_)

        exec_checks = {
            "enqueued == total fills": exec_._enqueued_events == FILLS_BURST,
            "enqueued == dropped + handled":
                exec_._enqueued_events == exec_.dropped_events + exec_.fills_total + exec_.rejects_total,
            "queue drained": exec_._queue.qsize() == 0,
            "drops were taken": exec_.dropped_events > 0,
            "fills actually handled": exec_.fills_total > 0,
            "engine healthy": exec_.status not in ("halted",),
        }
        log.info("EXECUTION ENGINE OVERLOAD RESULTS:")
        for k, v in exec_checks.items():
            log.info(f"  [{'PASS' if v else 'FAIL'}] {k}")
        for k, v in exec_checks.items():
            if not v:
                raise AssertionError(f"exec check failed: {k}")

        await asyncio.sleep(1.5)

        log.info(f"Phase D: whole-market fan-out across {N_SYMBOLS} symbols ...")
        symbols = [f"MCX:SYM{i:04d}FUT" for i in range(N_SYMBOLS)]
        data.register_symbols(symbols)
        for s in symbols:
            ds.push(s, 100.0, 99.9, 100.1, with_depth=True)
        await wait_idle(data, exec_)
        log.info(f"Phase D done: fanout={data.fanout_published} "
                 f"ingested={data.ingested_total}")

        symbols_reached = {t.symbol for t in received}
        await wait_for_symbols(symbols, received)
        missing = [s for s in symbols if s not in symbols_reached]
        market_checks = {
            "fan-out carried a tick for every symbol": not missing,
            "concurrent in-flight publishes bounded (< pool cap)": conn_gauge["max"] <= 30,
        }
        log.info("WHOLE-MARKET FAN-OUT RESULTS:")
        for k, v in market_checks.items():
            log.info(f"  [{'PASS' if v else 'FAIL'}] {k}")
        for k, v in market_checks.items():
            if not v:
                raise AssertionError(f"market check failed: {k}")

        # ====================================================================
        # Phase E: ML capture — order_book_depths + ml_features MUST grow.
        # Regression for the bug where the ML engine called the async
        # buffer_write() from a sync context (never-awaited coroutine) so the
        # tables stayed at zero rows forever.
        # ====================================================================
        log.info("Phase E: ML capture — depth + feature tables must GROW ...")
        import app.ml.engine as ml_engine
        import app.ml.model as ml_model
        import numpy as np

        _reset_ml_state(ml_engine, ml_model)
        ml_engine.configure(
            db=db, enabled=False, capture_enabled=True,
            depth_persist_interval=0.0, feature_persist_interval=0.0,
        )
        ml_engine.register_symbol(SYM)
        e0_d = len(db.buf["order_book_depths"])
        e0_f = len(db.buf["ml_features"])
        e0_p = len(db.buf["ml_predictions"])
        base = 700.0
        for i in range(200):
            px = base + (i % 5) * 0.1
            ds.push(SYM, px, px - 0.1, px + 0.1, with_depth=True)
        await wait_idle(data, exec_)

        e_grows = {
            "order_book_depths rows grew (capture works, no model needed)":
                len(db.buf["order_book_depths"]) > e0_d,
            "ml_features rows grew":
                len(db.buf["ml_features"]) > e0_f,
            "no predictions while inference off":
                len(db.buf["ml_predictions"]) == e0_p,
            "no ML result on snapshot while capture-only":
                data.snapshot(SYM).ml_adverse_prob == 0.0
                and data.snapshot(SYM).ml_should_widen is False,
        }
        log.info("ML CAPTURE RESULTS:")
        for k, v in e_grows.items():
            log.info(f"  [{'PASS' if v else 'FAIL'}] {k}")
        for k, v in e_grows.items():
            if not v:
                raise AssertionError(f"ml capture check failed: {k}")

        # ====================================================================
        # Phase F: ML inference — prediction log grows + snapshot carries result.
        # ====================================================================
        log.info("Phase F: ML inference — predictions + widen recommendation ...")
        ml_model._models["dummy"] = SimpleNamespace(
            get_inputs=lambda: [SimpleNamespace(name="float_input")],
            run=lambda *a, **k: [np.array([[0.9]], dtype=np.float32)],
        )
        ml_model._model_meta["dummy"] = {
            "version": "v1", "feature_names": [f"f{i}" for i in range(27)],
            "path": "dummy",
        }
        ml_model.set_active_model("dummy")
        ml_engine.configure(
            db=db, enabled=True, capture_enabled=True,
            depth_persist_interval=0.0, feature_persist_interval=0.0,
            widen_threshold=0.7, widen_extra_ticks=2,
        )
        base = 800.0
        for i in range(150):
            px = base + (i % 5) * 0.1
            ds.push(SYM, px, px - 0.1, px + 0.1, with_depth=True)
        await wait_idle(data, exec_)

        preds = db.buf["ml_predictions"]
        snap = data.snapshot(SYM)
        f_grows = {
            "ml_predictions rows grew":
                len(preds) > e0_p,
            "every prediction gets a WIDEN action (p=0.9 > t=0.7)":
                bool(preds) and all(r.get("action") == "WIDEN" for r in preds),
            "snapshot carries ML widening output":
                snap.ml_should_widen is True and snap.ml_extra_ticks == 2
                and snap.ml_adverse_prob is not None,
        }
        log.info("ML INFERENCE RESULTS:")
        for k, v in f_grows.items():
            log.info(f"  [{'PASS' if v else 'FAIL'}] {k}")
        for k, v in f_grows.items():
            if not v:
                raise AssertionError(f"ml inference check failed: {k}")
        log.info(f"  ML rows buffered: depth={len(db.buf['order_book_depths'])} "
                 f"features={len(db.buf['ml_features'])} predictions={len(preds)}")
        _reset_ml_state(ml_engine, ml_model)

        if loop_errors:
            log.error(f"{len(loop_errors)} unhandled loop exceptions leaked:")
            for ctx in loop_errors[:10]:
                log.error(f"  {ctx.get('message')} {ctx.get('exception')!r}")
            raise AssertionError("unhandled loop exceptions leaked (this is the OLD QueueFull bug)")

        log.info(f"final counts: data in={data._enqueued_total} drops={data.dropped_ticks} "
                 f"ingested={data.ingested_total} fanout={data.fanout_published} | "
                 f"exec in={exec_._enqueued_events} drops={exec_.dropped_events} "
                 f"fills={exec_.fills_total} rejects={exec_.rejects_total}")
        assert data.status == "healthy", data.detail
        assert exec_.status == "healthy", exec_.detail

        await exec_.stop()
        await data.stop()
        # every fan-out task must be cancelled + awaited before the loop closes —
        # otherwise Python reports "Task was destroyed but it is pending!" at exit
        assert not data._fanout_tasks, f"pending fanout tasks left after stop: {data._fanout_tasks}"
        assert not data._fanout_active, f"fanout active set not cleared: {data._fanout_active}"
        assert not data._fanout_pending, f"fanout pending not drained: {data._fanout_pending}"
        await asyncio.sleep(0.2)
        await bus.close()
        log.success("STRESS TEST PASSED")
    except BaseException as exc:
        log.error(f"STRESS TEST FAILED: {exc!r}")
        try:
            await exec_.stop()
            await data.stop()
            await bus.close()
        except BaseException:
            pass
        raise


if __name__ == "__main__":
    asyncio.run(main())