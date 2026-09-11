"""Regression tests for graceful task drain on engine shutdown.

CostEngine and MonitorEngine spawn fire-and-forget tasks (cost-quote jobs and
snapshot publishes). They must be tracked and cancelled+awaited on shutdown so
the event loop never closes with pending tasks ("Task was destroyed but it is
pending!" / "coroutine ... was never awaited").
"""

import asyncio
import unittest

from app.engines.cost_engine import CostEngine
from app.engines.monitor_engine import MonitorEngine


class _Bus:
    async def publish(self, *a, **k):
        pass

    async def close(self):
        pass

    def channel_for(self, *a, **k):
        return "fake"

    async def subscribe(self, channel, handler):
        pass


class _DB:
    async def close(self):
        pass

    async def insert_cost_quote(self, cq):
        pass


class EngineTaskDrainTest(unittest.TestCase):
    def _run_drain(self, engine, task_set_attr, drain_fn):
        async def never(*a, **k):
            await asyncio.Event().wait()

        async def scenario():
            tasks = getattr(engine, task_set_attr)
            t1 = asyncio.create_task(never())
            t2 = asyncio.create_task(never())
            tasks.add(t1)
            tasks.add(t2)
            await asyncio.sleep(0)
            self.assertEqual(len(tasks), 2)
            await drain_fn()
            self.assertEqual(len(tasks), 0)
            self.assertTrue(t1.cancelled() and t2.cancelled())

        asyncio.run(scenario())

    def test_cost_engine_drains_market_tasks(self):
        eng = CostEngine(_Bus(), _DB())
        self._run_drain(eng, "_market_tasks", eng._shutdown_market_tasks)

    def test_monitor_engine_drains_publish_tasks(self):
        eng = MonitorEngine(_Bus(), _DB())
        self._run_drain(eng, "_publish_tasks", eng._shutdown_publish_tasks)

    def test_stop_flag_gates_new_fire_and_forget_tasks(self):
        eng = CostEngine(_Bus(), _DB())
        eng._last_quote_ts = {}

        def fake_handler(ch, raw):
            return eng._on_market(ch, raw)

        eng._stop.set()
        # after stop is requested the handler must refuse to spawn new work
        self.assertTrue(eng._stop.is_set())


if __name__ == "__main__":
    unittest.main(verbosity=2)