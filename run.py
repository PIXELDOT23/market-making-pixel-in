"""
run.py
------
Entry point for the multi-engine low-latency architecture.

Usage:
    python3 run.py          # boot all seven engines + FastAPI monitor gateway

Ctrl+C / SIGTERM triggers an orderly teardown that FIRST flattens every position
and cancels all resting orders through the risk->execution chain (so you are
never left holding a position when the process goes away), then stops engines.
"""

from __future__ import annotations

import asyncio
import os

import app.infra.logging as log
from app.container import EngineManager
from app.api import boot_and_run


async def _main():
    manager = EngineManager()
    await boot_and_run(manager)


def _force_exit(code: int = 0):
    # Flatten + teardown already completed, but the broker socket / executor
    # threads are non-daemon and would keep the process alive forever. For a
    # kill-switch system "Ctrl+C must die" is a hard requirement, so exit now.
    os._exit(code)

def _run():
    # Drive the app on a raw loop and never await asyncio.run()'s cleanup:
    # its `shutdown_default_executor()` has no cancellation and blocks forever
    # on any stuck broker REST thread — yet we _force_exit anyway.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_main())
    except KeyboardInterrupt:
        log.warn("Interrupted by keyboard (Ctrl+C) — shutdown already handled.")
    except SystemExit as exc:
        # Intentional abort (e.g. API port already held by another instance) —
        # not a crash. Carry its exit code without a scary traceback.
        code = exc.code if isinstance(exc.code, int) else 1
        log.warn(f"Aborted (exit {code}) — no engines were booted.")
        _force_exit(code)
    except BaseException as exc:
        # Never swallow a real failure as a clean exit: forward the traceback
        # and exit non-zero so the operator/kill-switch sees the error.
        log.error(f"Unhandled {type(exc).__name__}: {exc}")
        try:
            loop.stop()
        except Exception:
            pass
        _force_exit(1)
    finally:
        try:
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass
        _force_exit(0)


if __name__ == "__main__":
    _run()