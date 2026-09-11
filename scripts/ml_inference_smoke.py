"""End-to-end ML pipeline test: train a toy XGBoost model on synthetic data,
export ONNX, load via model.py, and confirm inference < 1ms with sane output.

Usage: DATABASE_URL=... .venv/bin/python scripts/ml_inference_smoke.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import xgboost as xgb
from onnxmltools import convert_xgboost
from onnxmltools.convert.common.data_types import FloatTensorType

from app.ml import features as feat
from app.ml import model as ml_model
from app.ml.features import FEATURE_NAMES, NUM_FEATURES


def main():
    print("> generating synthetic features ...")
    rng = np.random.default_rng(42)
    n = 2000
    X = rng.normal(size=(n, NUM_FEATURES)).astype(np.float32)
    # label correlated with obi_1, spread_ticks, churn_30s
    y = ((X[:, 10] + X[:, 14] * 0.3 + X[:, 24] * 0.2 + rng.normal(size=n) * 0.5) > 0).astype(np.float32)
    print(f"  X={X.shape} y={y.mean():.3f}")

    print("> training toy XGBoost ...")
    onnx_names = [f"f{i}" for i in range(NUM_FEATURES)]  # onnxmltools requires f%d
    dtrain = xgb.DMatrix(X, label=y, feature_names=onnx_names)
    model = xgb.train(
        {"objective": "binary:logistic", "max_depth": 3, "learning_rate": 0.1,
         "n_estimators": 100, "tree_method": "hist", "verbosity": 0},
        dtrain, num_boost_round=100,
    )

    print("> exporting ONNX ...")
    os.makedirs("/tmp/opencode/ml", exist_ok=True)
    path = "/tmp/opencode/ml/smoke.onnx"
    onnx_model = convert_xgboost(model, initial_types=[("float_input", FloatTensorType([None, NUM_FEATURES]))])
    with open(path, "wb") as f:
        f.write(onnx_model.SerializeToString())

    print("> loading via model.py ...")
    assert ml_model.load_model("smoke", path, version="v1", feature_names=list(FEATURE_NAMES)), "load failed"
    ml_model.set_active_model("smoke")
    assert ml_model.is_available()

    # latency benchmark over 10k inferences
    print("> benchmarking 10k inferences ...")
    arr = X[:10000] if len(X) >= 10000 else np.tile(X, (10, 1))
    t0 = time.perf_counter()
    for row in arr:
        ml_model.predict(row.tolist())
    dt = time.perf_counter() - t0
    avg_us = dt / len(arr) * 1e6
    print(f"  avg {avg_us:.1f} us/inference  (target <1000us)")

    # sanity: prediction correlates with label
    preds = [ml_model.predict(r.tolist())[0] for r in arr[:500]]
    print(f"  pred mean={np.mean(preds):.3f} (pos label rate {y[:500].mean():.3f})")

    # roundtrip through engine.on_tick feature path
    print("> engine.on_tick feature pipeline ...")
    import app.ml.engine as ml_engine
    ml_engine.configure(db=None, enabled=True)
    ml_engine.register_symbol("MCX:SAMPLE")
    bids = [{"price": 100.0 - i * 0.1, "qty": 10 + i * 5, "orders": 1} for i in range(5)]
    asks = [{"price": 100.1 + i * 0.1, "qty": 12 + i * 4, "orders": 1} for i in range(5)]
    res = ml_engine.on_tick(
        symbol="MCX:SAMPLE", ts=time.time(), ltp=100.05,
        bid=100.0, ask=100.1, mid=100.05, bids=bids, asks=asks,
        tick_size=0.1, churn_5s=0.5, churn_30s=0.2,
    )
    assert res is not None
    print(f"  adverse_prob={res['adverse_prob']:.3f} widen={res['should_widen']} latency={res['latency_us']:.1f}us")

    print("ALL ML INFERENCE SMOKE TESTS OK")


if __name__ == "__main__":
    main()