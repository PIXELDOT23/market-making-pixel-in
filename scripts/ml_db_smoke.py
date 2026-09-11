"""
Quick integration check: writes a fake depth snapshot + ml_features + prediction
through the Database buffer and flushes, then reads back. Run with the real
pixel_in DB (DATABASE_URL=postgresql://postgres:PixelIn@localhost:5432/pixel_in).

Usage:  DATABASE_URL=postgresql://postgres:PixelIn@localhost:5432/pixel_in \
        .venv/bin/python scripts/ml_db_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import schema
from app.infra.db import Database


async def main():
    db = Database(os.environ["DATABASE_URL"])
    await db.connect()
    assert not db.disabled, "DB should be reachable"

    print("> writing order_book_depths row ...")
    await db.insert_order_book_depth(schema.OrderBookDepth(
        ts=__import__("time").time(),
        symbol="MCX:SMOKETEST",
        ltp=100.0,
        mid=100.05,
        spread=0.1,
        bids=[{"price": 100.0, "qty": 10, "orders": 1}],
        asks=[{"price": 100.1, "qty": 11, "orders": 2}],
        volume=999,
        churn_tps=1.5,
    ))
    await db.flush()

    rows = await db.fetch_all(
        "SELECT symbol, ltp, spread, bids, churn_tps FROM order_book_depths "
        "WHERE symbol = 'MCX:SMOKETEST' ORDER BY id DESC LIMIT 1"
    )
    assert rows, "no depth row found"
    assert rows[0]["symbol"] == "MCX:SMOKETEST"
    assert rows[0]["bids"][0]["qty"] == 10, f"bids decode fail: {rows[0]['bids']}"
    print("OK depth:", rows[0])

    print("> writing ml_features row (async buffer_write) ...")
    key = __import__("time").time()
    await db.buffer_write("ml_features", {
        "ts": db._ts(key), "symbol": "MCX:SMOKETEST",
        "features": db._encoder.encode({"obi_1": 0.5, "spread_ticks": 2.0}).decode(),
        "label": None, "label_ts": None,
    })
    await db.flush()
    frows = await db.fetch_all(
        "SELECT symbol, features FROM ml_features WHERE symbol='MCX:SMOKETEST' "
        "ORDER BY id DESC LIMIT 1"
    )
    assert frows, "no feature row"
    assert frows[0]["features"]["obi_1"] == 0.5, f"feature decode fail: {frows[0]}"
    print("  OK feature:", frows[0])

    print("> writing order_book_depths row via buffer_write_sync (engine hot path) ...")
    db.buffer_write_sync("order_book_depths", {
        "ts": db._ts(key), "symbol": "MCX:SMOKETEST",
        "ltp": 101.0, "mid": 101.05, "spread": 0.1,
        "bids": db._encoder.encode([{"price": 101.0, "qty": 21, "orders": 1}]).decode(),
        "asks": db._encoder.encode([{"price": 101.1, "qty": 22, "orders": 1}]).decode(),
        "volume": 0, "churn_tps": 2.5,
    })
    await db.flush()
    srows = await db.fetch_all(
        "SELECT symbol, ltp, bids FROM order_book_depths "
        "WHERE symbol='MCX:SMOKETEST' AND ltp=101.0 ORDER BY id DESC LIMIT 1"
    )
    assert srows and srows[0]["bids"][0]["qty"] == 21, f"sync depth fail: {srows}"
    print("  OK sync depth:", srows[0])

    print("> writing ml_prediction row ...")
    await db.insert_ml_prediction(schema.MLPrediction(
        ts=key, symbol="MCX:SMOKETEST",
        model_name="smoke", model_version="v0",
        prediction=0.71, action="WIDEN",
    ))
    await db.flush()
    prows = await db.fetch_all(
        "SELECT model_name, prediction, action FROM ml_predictions "
        "WHERE symbol='MCX:SMOKETEST' ORDER BY id DESC LIMIT 1"
    )
    assert prows and prows[0]["prediction"] == 0.71, f"pred fail: {prows}"
    print("  OK prediction:", prows[0])

    await db.close()
    print("  ALL ML DB ROUND-TRIPS OK")


if __name__ == "__main__":
    asyncio.run(main())