"""
app/ml/engine.py
----------------
Functional ML engine: orchestrates feature extraction, inference, and logging.

No classes — module-level state + pure functions. Called from DataEngine on
every tick (or at a configurable interval). The engine:

1. Maintains per-symbol rolling windows (mid prices, spreads, churn).
2. Computes the 27-dim feature vector via features.py.
3. Runs ONNX inference via model.py.
4. Returns a widening recommendation to SignalEngine.
5. Logs features + predictions to PostgreSQL for training.

All state lives in module-level dicts keyed by symbol. Zero heap allocation
on the hot path after the first tick (dicts pre-populated at registration).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from app.ml import features as feat
from app.ml import model
from app.infra.db import Database
import app.infra.logging as log

# ---------------------------------------------------------------------------
# Module state (no classes)
# ---------------------------------------------------------------------------
_registered: set = set()                    # symbols we track
_mid_history: Dict[str, Deque[Tuple[float, float]]] = {}   # symbol -> [(ts, mid)]
_spread_history: Dict[str, Deque[float]] = {}              # symbol -> [spread_ticks]
_depth_snapshot_ts: Dict[str, float] = {}                  # symbol -> last depth persist ts

# Config (set once at boot via configure())
_db: Optional[Database] = None
_depth_persist_interval: float = 1.0       # seconds between depth snapshots
_feature_persist_interval: float = 0.5     # seconds between feature writes
_capture_enabled: bool = True              # persist depth + features (data collection)
_enabled: bool = False                     # inference + widening recommendation
_widen_threshold: float = 0.65             # P(adverse) above this => widen
_widen_extra_ticks: int = 1                # additional ticks to widen

# Rolling window sizes
_MID_MAXLEN = 200
_SPREAD_MAXLEN = 100


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def configure(
    db: Optional[Database],
    enabled: bool = False,
    capture_enabled: bool = True,
    depth_persist_interval: float = 1.0,
    feature_persist_interval: float = 0.5,
    widen_threshold: float = 0.65,
    widen_extra_ticks: int = 1,
):
    """Configure the ML engine at boot. Called once from container.py.

    ``enabled`` controls inference + the widening recommendation (ML_ENABLED).
    ``capture_enabled`` controls data collection — persisting order-book depth
    and feature rows so the Postgres tables grow even while no model exists.
    """
    global _db, _enabled, _capture_enabled, _depth_persist_interval
    global _feature_persist_interval, _widen_threshold, _widen_extra_ticks
    _db = db
    _enabled = enabled
    _capture_enabled = capture_enabled
    _depth_persist_interval = depth_persist_interval
    _feature_persist_interval = feature_persist_interval
    _widen_threshold = widen_threshold
    _widen_extra_ticks = widen_extra_ticks
    if enabled:
        log.info(
            f"[ml] engine enabled (threshold={widen_threshold}, "
            f"extra_ticks={widen_extra_ticks}, depth_interval={depth_persist_interval}s)"
        )
    elif capture_enabled:
        log.info(
            f"[ml] data capture enabled (depth+features persisted to Postgres; "
            f"inference off — set ML_ENABLED=1 once a model is deployed)"
        )


def register_symbol(symbol: str):
    """Pre-allocate rolling buffers for a symbol. Call at universe scan time."""
    if symbol in _registered:
        return
    _registered.add(symbol)
    _mid_history[symbol] = deque(maxlen=_MID_MAXLEN)
    _spread_history[symbol] = deque(maxlen=_SPREAD_MAXLEN)
    _depth_snapshot_ts[symbol] = 0.0


def is_active(symbol: str) -> bool:
    """True when the calendar that drives the DataEngine should call on_tick().

    Uses the OR of capture (persist rows) and inference (predict+widen) so the
    data-collection tables grow independently of whether a model is loaded.
    """
    return symbol in _registered and (_capture_enabled or _enabled)


# ---------------------------------------------------------------------------
# Hot-path: called every tick
# ---------------------------------------------------------------------------
def on_tick(
    *,
    symbol: str,
    ts: float,
    ltp: float,
    bid: float,
    ask: float,
    mid: float,
    bids: List[Dict[str, Any]],
    asks: List[Dict[str, Any]],
    tick_size: float,
    churn_5s: float = 0.0,
    churn_30s: float = 0.0,
) -> Optional[Dict[str, Any]]:
    """Process one tick: update rolling state, compute features, infer.

    Runs whenever capture and/or inference is enabled for a registered symbol.
    Order-book depth and feature rows are always persisted (throttled) so the
    training tables grow regardless of whether a model is loaded.

    Returns a dict with:
      {"should_widen": bool, "extra_ticks": int, "adverse_prob": float,
       "latency_us": float, "features": dict}
    or None if inference is disabled / symbol not registered. When inference is
    off, capture still happens inside this call and the return is None.
    """
    if symbol not in _registered:
        return None
    if not (_capture_enabled or _enabled):
        return None

    t0 = time.perf_counter()

    # --- update rolling mid history ---
    hist = _mid_history[symbol]
    hist.append((ts, mid))

    # --- compute mid returns (1, 5, 10 ticks) ---
    mid_returns = _mid_returns(hist)

    # --- update spread history ---
    if bid > 0 and ask > 0 and tick_size > 0:
        spread_t = (ask - bid) / tick_size
        _spread_history[symbol].append(spread_t)

    # --- compute feature vector ---
    features = feat.compute_features(
        bids=bids,
        asks=asks,
        bid=bid,
        ask=ask,
        mid=mid,
        tick_size=tick_size,
        churn_5s=churn_5s,
        churn_30s=churn_30s,
        mid_returns=mid_returns,
        ts=ts,
        spread_history=list(_spread_history[symbol]),
    )

    # --- persist depth snapshot (throttled, synchronous — never blocks) ---
    now = time.time()
    _maybe_persist_depth(symbol, ts, ltp, bid, ask, mid, bids, asks, churn_30s, now)

    # --- persist features (throttled) ---
    _maybe_persist_features(symbol, ts, features, now)

    # --- inference (only when enabled) ---
    if not _enabled:
        return None

    arr = feat.features_to_array(features)
    raw_prob, inf_latency_us = model.predict(arr)

    should_widen = raw_prob >= _widen_threshold
    extra_ticks = _widen_extra_ticks if should_widen else 0

    _maybe_persist_prediction(symbol, ts, features, raw_prob, should_widen, now)

    total_us = (time.perf_counter() - t0) * 1_000_000

    return {
        "should_widen": should_widen,
        "extra_ticks": extra_ticks,
        "adverse_prob": raw_prob,
        "latency_us": total_us + inf_latency_us,
        "features": features,
    }


# ---------------------------------------------------------------------------
# Mid-return helper
# ---------------------------------------------------------------------------
def _mid_returns(hist: Deque[Tuple[float, float]]) -> List[float]:
    """Compute mid-price returns at 1, 5, and 10 tick intervals."""
    n = len(hist)
    returns = []
    for lookback in (1, 5, 10):
        if n > lookback:
            curr = hist[-1][1]
            prev = hist[-1 - lookback][1]
            returns.append((curr - prev) / prev if prev > 0 else 0.0)
        else:
            returns.append(0.0)
    return returns


# ---------------------------------------------------------------------------
# Persistence (throttled, never blocks hot path)
# ---------------------------------------------------------------------------
def _maybe_persist_depth(
    symbol: str, ts: float, ltp: float, bid: float, ask: float,
    mid: float, bids: List[Dict], asks: List[Dict], churn: float, now: float,
):
    if _db is None or _db.disabled:
        return
    last = _depth_snapshot_ts.get(symbol, 0.0)
    if now - last < _depth_persist_interval:
        return
    _depth_snapshot_ts[symbol] = now
    spread = (ask - bid) if (bid > 0 and ask > 0) else 0.0
    try:
        _db.buffer_write_sync("order_book_depths", {
            "ts": _db._ts(ts),
            "symbol": symbol,
            "ltp": ltp,
            "mid": mid,
            "spread": spread,
            "bids": _db._encoder.encode(bids).decode() if bids else "[]",
            "asks": _db._encoder.encode(asks).decode() if asks else "[]",
            "volume": 0,
            "churn_tps": churn,
        })
    except Exception:
        pass


_feature_persist_ts: Dict[str, float] = {}


def _maybe_persist_features(
    symbol: str, ts: float, features: Dict[str, float], now: float,
):
    if _db is None or _db.disabled:
        return
    last = _feature_persist_ts.get(symbol, 0.0)
    if now - last < _feature_persist_interval:
        return
    _feature_persist_ts[symbol] = now
    try:
        _db.buffer_write_sync("ml_features", {
            "ts": _db._ts(ts),
            "symbol": symbol,
            "features": _db._encoder.encode(features).decode(),
            "label": None,
            "label_ts": None,
        })
    except Exception:
        pass


_prediction_persist_ts: Dict[str, float] = {}


def _maybe_persist_prediction(
    symbol: str, ts: float, features: Dict[str, float],
    prediction: float, should_widen: bool, now: float,
):
    """Log every inference for offline analysis (throttled at feature cadence)."""
    if _db is None or _db.disabled:
        return
    name = model.active_model()
    if name is None:
        return
    last = _prediction_persist_ts.get(symbol, 0.0)
    if now - last < _feature_persist_interval:
        return
    _prediction_persist_ts[symbol] = now
    meta = getattr(model, "_model_meta", {}).get(name, {})
    try:
        _db.buffer_write_sync("ml_predictions", {
            "ts": _db._ts(ts),
            "symbol": symbol,
            "model_name": name,
            "model_version": meta.get("version", ""),
            "features": _db._encoder.encode(features).decode(),
            "prediction": float(prediction),
            "action": "WIDEN" if should_widen else "HOLD",
        })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
def status() -> Dict[str, Any]:
    return {
        "enabled": _enabled,
        "capture_enabled": _capture_enabled,
        "registered_symbols": len(_registered),
        "active_model": model.active_model(),
        "loaded_models": model.loaded_models(),
        "widen_threshold": _widen_threshold,
    }
