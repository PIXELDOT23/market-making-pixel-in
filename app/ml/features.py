"""
app/ml/features.py
------------------
Pure-function feature engineering from order-book depth + market state.

All functions take primitive inputs (dicts, floats, ints) and return floats.
No side effects, no class state — O(1) per feature, <100us total.

Feature vector (27 dims):
  depth:     bid_depth_1..5, ask_depth_1..5  (10)
  imbalance: obi_1, obi_3, obi_5, obi_weighted (4)
  spread:    spread_ticks, spread_pct, spread_zscore (3)
  micro:     bid_ask_ratio, total_bid_qty, total_ask_qty, mid_return_1t,
             mid_return_5t, mid_return_10t, churn_5s, churn_30s (8)
  time:      hour_sin, hour_cos (2)
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

# Canonical feature order (matches ONNX model input)
FEATURE_NAMES: Tuple[str, ...] = (
    # depth levels
    "bid_depth_1", "bid_depth_2", "bid_depth_3", "bid_depth_4", "bid_depth_5",
    "ask_depth_1", "ask_depth_2", "ask_depth_3", "ask_depth_4", "ask_depth_5",
    # imbalance
    "obi_1", "obi_3", "obi_5", "obi_weighted",
    # spread
    "spread_ticks", "spread_pct", "spread_zscore",
    # microstructure
    "bid_ask_ratio", "total_bid_qty", "total_ask_qty",
    "mid_return_1t", "mid_return_5t", "mid_return_10t",
    "churn_5s", "churn_30s",
    # time encoding
    "hour_sin", "hour_cos",
)

NUM_FEATURES = len(FEATURE_NAMES)


def depth_features(
    bids: List[Dict[str, Any]],
    asks: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Extract raw depth quantities for levels 1-5. Missing levels default to 0."""
    out: Dict[str, float] = {}
    for i in range(5):
        out[f"bid_depth_{i + 1}"] = float(bids[i]["qty"]) if i < len(bids) else 0.0
        out[f"ask_depth_{i + 1}"] = float(asks[i]["qty"]) if i < len(asks) else 0.0
    return out


def imbalance_features(
    bids: List[Dict[str, Any]],
    asks: List[Dict[str, Any]],
) -> Dict[str, float]:
    """Order-book imbalance at top-1, top-3, top-5, and depth-weighted."""
    def _obi(n: int) -> float:
        bq = sum(b["qty"] for b in bids[:n])
        aq = sum(a["qty"] for a in asks[:n])
        total = bq + aq
        return (bq - aq) / total if total > 0 else 0.0

    def _weighted_obi() -> float:
        bq = sum(b["qty"] / (i + 1) for i, b in enumerate(bids[:5]))
        aq = sum(a["qty"] / (i + 1) for i, a in enumerate(asks[:5]))
        total = bq + aq
        return (bq - aq) / total if total > 0 else 0.0

    return {
        "obi_1": _obi(1),
        "obi_3": _obi(3),
        "obi_5": _obi(5),
        "obi_weighted": _weighted_obi(),
    }


def spread_features(
    bid: float,
    ask: float,
    tick_size: float,
    mid: float,
    spread_history: Optional[List[float]] = None,
) -> Dict[str, float]:
    """Spread in ticks, percentage, and z-score vs recent history."""
    if not bid or not ask or tick_size <= 0:
        return {"spread_ticks": 0.0, "spread_pct": 0.0, "spread_zscore": 0.0}
    raw_spread = ask - bid
    spread_ticks = raw_spread / tick_size
    spread_pct = (raw_spread / mid * 100.0) if mid > 0 else 0.0

    zscore = 0.0
    if spread_history and len(spread_history) >= 5:
        mean_s = sum(spread_history) / len(spread_history)
        var_s = sum((s - mean_s) ** 2 for s in spread_history) / len(spread_history)
        std_s = var_s ** 0.5
        if std_s > 1e-9:
            zscore = (spread_ticks - mean_s) / std_s

    return {
        "spread_ticks": spread_ticks,
        "spread_pct": spread_pct,
        "spread_zscore": zscore,
    }


def microstructure_features(
    bid: float,
    ask: float,
    bid_qty: int,
    ask_qty: int,
    bids: List[Dict[str, Any]],
    asks: List[Dict[str, Any]],
    mid_returns: List[float],
    churn_5s: float,
    churn_30s: float,
) -> Dict[str, float]:
    """Microstructure: ratios, total qty, mid returns, churn."""
    total_bid = sum(b["qty"] for b in bids) if bids else float(bid_qty)
    total_ask = sum(a["qty"] for a in asks) if asks else float(ask_qty)
    ba_ratio = total_bid / total_ask if total_ask > 0 else 1.0

    return {
        "bid_ask_ratio": ba_ratio,
        "total_bid_qty": total_bid,
        "total_ask_qty": total_ask,
        "mid_return_1t": mid_returns[0] if len(mid_returns) > 0 else 0.0,
        "mid_return_5t": mid_returns[1] if len(mid_returns) > 1 else 0.0,
        "mid_return_10t": mid_returns[2] if len(mid_returns) > 2 else 0.0,
        "churn_5s": churn_5s,
        "churn_30s": churn_30s,
    }


def time_features(ts: float) -> Dict[str, float]:
    """Sin/cos encoding of hour-of-day (UTC epoch seconds)."""
    hour = (ts / 3600.0) % 24.0
    return {
        "hour_sin": math.sin(2.0 * math.pi * hour / 24.0),
        "hour_cos": math.cos(2.0 * math.pi * hour / 24.0),
    }


def compute_features(
    *,
    bids: List[Dict[str, Any]],
    asks: List[Dict[str, Any]],
    bid: float,
    ask: float,
    mid: float,
    tick_size: float,
    churn_5s: float,
    churn_30s: float,
    mid_returns: List[float],
    ts: float,
    spread_history: Optional[List[float]] = None,
) -> Dict[str, float]:
    """Compute the full 27-dim feature vector from raw market state.

    Returns a dict keyed by FEATURE_NAMES. All values are float64.
    """
    feat: Dict[str, float] = {}
    feat.update(depth_features(bids, asks))
    feat.update(imbalance_features(bids, asks))
    feat.update(spread_features(bid, ask, tick_size, mid, spread_history))
    feat.update(microstructure_features(
        bid, ask, 0, 0, bids, asks, mid_returns, churn_5s, churn_30s,
    ))
    feat.update(time_features(ts))
    return feat


def features_to_array(feat: Dict[str, float]) -> List[float]:
    """Convert feature dict to ordered float list matching FEATURE_NAMES."""
    return [float(feat.get(name, 0.0)) for name in FEATURE_NAMES]
