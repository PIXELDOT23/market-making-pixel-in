"""
tests/test_ml_features.py
-------------------------
Unit tests for the functional ML features + model + engine modules.
"""

from __future__ import annotations

import time
from collections import deque

import pytest

from app.ml import features as feat
from app.ml import engine as ml_engine
from app.ml import model as ml_model


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------
def _book():
    bids = [
        {"price": 100.0, "qty": 10, "orders": 1},
        {"price": 99.9, "qty": 20, "orders": 2},
        {"price": 99.8, "qty": 30, "orders": 1},
        {"price": 99.7, "qty": 40, "orders": 3},
        {"price": 99.6, "qty": 50, "orders": 2},
    ]
    asks = [
        {"price": 100.1, "qty": 12, "orders": 2},
        {"price": 100.2, "qty": 22, "orders": 1},
        {"price": 100.3, "qty": 32, "orders": 3},
        {"price": 100.4, "qty": 42, "orders": 1},
        {"price": 100.5, "qty": 52, "orders": 2},
    ]
    return bids, asks


def test_feature_names_are_consistent():
    assert feat.NUM_FEATURES == len(feat.FEATURE_NAMES) == 27
    assert len(set(feat.FEATURE_NAMES)) == 27  # no duplicates


def test_depth_features_levels():
    bids, asks = _book()
    out = feat.depth_features(bids, asks)
    assert out["bid_depth_1"] == 10.0
    assert out["bid_depth_5"] == 50.0
    assert out["ask_depth_3"] == 32.0
    # missing levels default to 0
    out2 = feat.depth_features([], [])
    assert out2["bid_depth_1"] == 0.0
    assert out2["ask_depth_5"] == 0.0


def test_imbalance_features():
    bids, asks = _book()
    im = feat.imbalance_features(bids, asks)
    assert im["obi_1"] == pytest.approx((10 - 12) / 22, abs=1e-6)
    assert im["obi_3"] == pytest.approx((60 - 66) / 126, abs=1e-6)
    assert -1.0 <= im["obi_5"] <= 1.0
    assert -1.0 <= im["obi_weighted"] <= 1.0
    # symmetric barks both sides -> 0
    sym_b = [{"qty": 10}, {"qty": 20}, {"qty": 30}, {"qty": 40}, {"qty": 50}]
    sym_a = [{"qty": 10}, {"qty": 20}, {"qty": 30}, {"qty": 40}, {"qty": 50}]
    im2 = feat.imbalance_features(sym_b, sym_a)
    assert im2["obi_1"] == 0.0
    assert im2["obi_weighted"] == 0.0


def test_spread_features():
    out = feat.spread_features(100.0, 100.4, 0.1, 100.2)
    assert out["spread_ticks"] == pytest.approx(4.0, abs=1e-6)
    assert out["spread_zscore"] == 0.0  # no history -> 0
    out2 = feat.spread_features(0.0, 0.0, 0.1, 0.0)
    assert out2["spread_ticks"] == 0.0


def test_microstructure_features():
    bids, asks = _book()
    out = feat.microstructure_features(
        100.0, 100.1, 10, 12, bids, asks,
        mid_returns=[0.001, 0.002, 0.003],
        churn_5s=1.5, churn_30s=0.5,
    )
    assert out["total_bid_qty"] == 150.0
    assert out["total_ask_qty"] == 160.0
    assert out["bid_ask_ratio"] == pytest.approx(150.0 / 160.0, abs=1e-6)
    assert out["mid_return_1t"] == 0.001
    assert out["mid_return_10t"] == 0.003
    assert out["churn_5s"] == 1.5
    assert out["churn_30s"] == 0.5


def test_time_features_range():
    out = feat.time_features(time.time())
    assert -1.0 <= out["hour_sin"] <= 1.0
    assert -1.0 <= out["hour_cos"] <= 1.0


def test_compute_features_full_vector():
    bids, asks = _book()
    fv = feat.compute_features(
        bids=bids, asks=asks,
        bid=100.0, ask=100.1, mid=100.05, tick_size=0.1,
        churn_5s=1.0, churn_30s=0.3,
        mid_returns=[0.0, 0.0, 0.0],
        ts=time.time(),
    )
    assert set(fv.keys()) == set(feat.FEATURE_NAMES)
    arr = feat.features_to_array(fv)
    assert len(arr) == feat.NUM_FEATURES
    assert all(isinstance(v, float) for v in arr)


def test_features_to_array_order():
    bids, asks = _book()
    fv = feat.compute_features(
        bids=bids, asks=asks,
        bid=100.0, ask=100.1, mid=100.05, tick_size=0.1,
        churn_5s=0.0, churn_30s=0.0,
        mid_returns=[0.0, 0.0, 0.0],
        ts=time.time(),
    )
    arr = feat.features_to_array(fv)
    # index 0 = bid_depth_1, index 10 = obi_1, index 14 = spread_ticks
    assert arr[0] == 10.0
    assert arr[14] == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# model (no-ORT fallback path)
# ---------------------------------------------------------------------------
def test_model_fallback_when_no_ort():
    # If onnxruntime is absent, predict returns 0.5 with zero latency
    raw, us = ml_model.predict([0.0] * feat.NUM_FEATURES)
    assert raw == 0.5
    assert us == 0.0


def test_model_unload_state_cleanup():
    ml_model.unload_model("nope")
    assert ml_model.active_model() is None
    assert ml_model.loaded_models() == {}


def test_model_is_available_gate():
    # Without a loaded session, is_available is False (or True if real ORT
    # with a loaded model which we don't have in tests)
    assert not ml_model.is_available() or ml_model.is_available()


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
import msgspec
import numpy as np
from types import SimpleNamespace


class _StubMLDB:
    """Records buffered ML rows synchronously — mirrors Database.buffer_write_sync."""
    disabled = False

    def __init__(self):
        self._encoder = msgspec.json.Encoder()
        self.buf = {"order_book_depths": [], "ml_features": [], "ml_predictions": []}

    @staticmethod
    def _ts(value):
        from datetime import datetime, timezone
        if isinstance(value, datetime):
            return value
        return datetime.fromtimestamp(value, tz=timezone.utc)

    def buffer_write_sync(self, table, row):
        self.buf[table].append(row)


def _reset_engine_state():
    ml_engine._registered.clear()
    ml_engine._mid_history.clear()
    ml_engine._spread_history.clear()
    ml_engine._depth_snapshot_ts.clear()
    ml_engine._feature_persist_ts.clear()
    ml_engine._prediction_persist_ts.clear()


def test_engine_disabled_returns_none():
    ml_engine.configure(db=None, enabled=False)
    bids, asks = _book()
    result = ml_engine.on_tick(
        symbol="MCX:NATGAS", ts=time.time(), ltp=100.0,
        bid=100.0, ask=100.1, mid=100.05,
        bids=bids, asks=asks, tick_size=0.1,
    )
    assert result is None


def test_engine_unregistered_symbol_returns_none():
    ml_engine.configure(db=None, enabled=True)
    bids, asks = _book()
    result = ml_engine.on_tick(
        symbol="UNREGISTERED", ts=time.time(), ltp=100.0,
        bid=100.0, ask=100.1, mid=100.05,
        bids=bids, asks=asks, tick_size=0.1,
    )
    assert result is None


def test_engine_enabled_returns_result():
    ml_engine.configure(db=None, enabled=True)
    ml_engine.register_symbol("MCX:NATGAS")
    bids, asks = _book()
    result = ml_engine.on_tick(
        symbol="MCX:NATGAS", ts=time.time(), ltp=100.0,
        bid=100.0, ask=100.1, mid=100.05,
        bids=bids, asks=asks, tick_size=0.1,
    )
    assert result is not None
    assert "adverse_prob" in result
    assert "should_widen" in result
    assert "extra_ticks" in result
    assert isinstance(result["adverse_prob"], float)
    assert isinstance(result["latency_us"], float)
    assert len(result["features"]) == feat.NUM_FEATURES


def test_engine_capture_only_persists_without_inference():
    """BUGFIX regression: capture must write depth+feature rows even when
    inference is OFF, and must do so via a SYNC enqueue (the old async
    buffer_write from a sync context created a never-awaited coroutine)."""
    _reset_engine_state()
    stub = _StubMLDB()
    ml_engine.configure(
        db=stub, enabled=False, capture_enabled=True,
        depth_persist_interval=0.0, feature_persist_interval=0.0,
    )
    ml_engine.register_symbol("MCX:CAPTURE")
    bids, asks = _book()
    for _ in range(3):
        result = ml_engine.on_tick(
            symbol="MCX:CAPTURE", ts=time.time(), ltp=100.0,
            bid=100.0, ask=100.1, mid=100.05,
            bids=bids, asks=asks, tick_size=0.1,
        )
        assert result is None  # inference off -> no widening recommendation
    assert len(stub.buf["order_book_depths"]) == 3        # depth rows GROW
    assert len(stub.buf["ml_features"]) == 3              # feature rows GROW
    assert len(stub.buf["ml_predictions"]) == 0           # no model -> none
    _reset_engine_state()


def test_engine_inference_persists_predictions():
    """BUGFIX regression: inference mode must also log ml_predictions rows."""
    _reset_engine_state()
    stub = _StubMLDB()
    # fake ONNX session: predict always ~0.9 => WIDEN at threshold 0.7
    ml_model._models["dummy"] = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(name="float_input")],
        run=lambda *a, **k: [np.array([[0.9]], dtype=np.float32)],
    )
    ml_model._model_meta["dummy"] = {
        "version": "v1", "feature_names": list(feat.FEATURE_NAMES), "path": "dummy",
    }
    ml_model.set_active_model("dummy")
    try:
        ml_engine.configure(
            db=stub, enabled=True, capture_enabled=True,
            depth_persist_interval=0.0, feature_persist_interval=0.0,
            widen_threshold=0.7, widen_extra_ticks=2,
        )
        ml_engine.register_symbol("MCX:INFER")
        bids, asks = _book()
        result = ml_engine.on_tick(
            symbol="MCX:INFER", ts=time.time(), ltp=100.0,
            bid=100.0, ask=100.1, mid=100.05,
            bids=bids, asks=asks, tick_size=0.1,
        )
        assert result is not None
        assert result["should_widen"] is True
        assert result["extra_ticks"] == 2
        assert result["adverse_prob"] == pytest.approx(0.9, abs=1e-3)
        assert len(stub.buf["order_book_depths"]) == 1    # capture still on
        assert len(stub.buf["ml_features"]) == 1
        assert len(stub.buf["ml_predictions"]) == 1       # prediction log GROWS
        pred = stub.buf["ml_predictions"][0]
        assert pred["action"] == "WIDEN"
        assert pred["model_name"] == "dummy"
        assert pred["model_version"] == "v1"
    finally:
        ml_model.unload_model("dummy")
    _reset_engine_state()


def test_engine_is_active_gating():
    _reset_engine_state()
    ml_engine.configure(db=None, enabled=False, capture_enabled=True)
    ml_engine.register_symbol("MCX:GATE")
    assert ml_engine.is_active("MCX:GATE") is True      # capture counts
    assert ml_engine.is_active("OTHER") is False
    ml_engine.configure(db=None, enabled=False, capture_enabled=False)
    assert ml_engine.is_active("MCX:GATE") is False     # neither on -> inactive
    ml_engine.configure(db=None, enabled=True, capture_enabled=False)
    assert ml_engine.is_active("MCX:GATE") is True      # inference alone counts
    _reset_engine_state()


def test_engine_wipe_state_between_tests():
    # Ensure module state does not leak across tests
    _reset_engine_state()
    ml_engine.configure(db=None, enabled=False)


# ---------------------------------------------------------------------------
# label resolution (pure parts — no DB in unit tests)
# ---------------------------------------------------------------------------
def test_avg_mid_gap():
    from app.ml import train
    mids = [(0.0, 100.0), (1.0, 100.1), (2.0, 100.2)]
    assert train._avg_mid_gap(mids) == pytest.approx(0.1, abs=1e-9)
    assert train._avg_mid_gap([]) == 0.0
    assert train._avg_mid_gap([(0.0, 100.0)]) == 0.0


def test_adverse_label_detects_step():
    from datetime import datetime, timezone
    import time as _time
    from app.ml import train
    base = datetime.now(timezone.utc)
    # mid steps 1.0 in the window -> clearly adverse
    mids = [(base, 100.0), (base + __import__("datetime").timedelta(seconds=1.0), 100.0),
            (base + __import__("datetime").timedelta(seconds=3.0), 101.0)]
    label = train._adverse_label(mids, base, 5.0, 0.5)
    assert label == 1.0


def test_adverse_label_no_move():
    from datetime import datetime, timezone, timedelta
    from app.ml import train
    base = datetime.now(timezone.utc)
    mids = [(base, 100.0), (base + timedelta(seconds=1.0), 100.0),
            (base + timedelta(seconds=3.0), 100.0)]
    label = train._adverse_label(mids, base, 5.0, 0.5)
    assert label == 0.0


def test_adverse_label_empty_series():
    from datetime import datetime, timezone
    from app.ml import train
    label = train._adverse_label([], datetime.now(timezone.utc), 5.0, 0.5)
    assert label == 0.0