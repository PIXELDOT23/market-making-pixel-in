"""
app/ml/train.py
---------------
Offline ML training pipeline.

Reads labelled data from PostgreSQL, engineers features, trains XGBoost,
exports ONNX, and writes the model to the ml_models table.

Run as:  python -m app.ml.train --name adverse_selection_v1 --days 7

No classes — pure functions. Designed to be run outside the live bot process.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import app.infra.logging as log
from app.ml.features import FEATURE_NAMES, NUM_FEATURES

# Lazy imports — these are only needed in the training script, not live.
_db = None
_np = None
_xgb = None
_ort = None


def _ensure_deps():
    global _np, _xgb, _ort
    try:
        import numpy as np
        _np = np
    except ImportError:
        raise SystemExit("numpy required for training: pip install numpy")
    try:
        import xgboost as xgb
        _xgb = xgb
    except ImportError:
        raise SystemExit("xgboost required for training: pip install xgboost")
    try:
        import onnxruntime as ort
        _ort = ort
    except ImportError:
        raise SystemExit("onnxruntime required for export: pip install onnxruntime")


def _get_db():
    global _db
    if _db is None:
        from app.infra.db import Database
        from app.config import settings
        _db = Database(settings.database_url)
    return _db


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_labelled_data(
    symbol: Optional[str] = None,
    days: int = 7,
    min_label: Optional[float] = None,
    include_unlabelled: bool = False,
) -> List[Dict[str, Any]]:
    """Load (optionally labelled) feature rows from PostgreSQL.

    Returns list of {features: dict, label: float} dicts.
    """
    db = _get_db()
    where = ""
    params: Dict[str, Any] = {}
    if not include_unlabelled:
        where = "WHERE label IS NOT NULL"
    else:
        where = "WHERE TRUE"
    if symbol:
        where += " AND symbol = %(symbol)s"
        params["symbol"] = symbol
    if days > 0:
        where += " AND ts >= NOW() - %(days)s * INTERVAL '1 day'"
        params["days"] = days
    if min_label is not None:
        where += " AND label >= %(min_label)s"
        params["min_label"] = min_label

    sql = f"""
        SELECT ts, symbol, features, label
        FROM ml_features
        {where}
        ORDER BY ts ASC
    """
    import psycopg
    import psycopg.rows
    with psycopg.connect(db.dsn) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    log.info(f"[train] loaded {len(rows)} labelled rows (symbol={symbol}, days={days})")
    return rows


def load_raw_depth_for_labelling(
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> List[Dict[str, Any]]:
    """Load raw order-book depth snapshots for feature reconstruction."""
    db = _get_db()
    sql = """
        SELECT ts, symbol, ltp, mid, spread, bids, asks, churn_tps
        FROM order_book_depths
        WHERE symbol = %(symbol)s AND ts BETWEEN %(start)s AND %(end)s
        ORDER BY ts ASC
    """
    import psycopg
    import psycopg.rows
    with psycopg.connect(db.dsn) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(sql, {"symbol": symbol, "start": start_ts, "end": end_ts})
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Label resolution (uses order_book_depths for true mid-price movement)
# ---------------------------------------------------------------------------
def compute_labels(
    rows: List[Dict[str, Any]],
    adverse_window_sec: float = 5.0,
    adverse_threshold_ticks: float = 0.5,
) -> List[Dict[str, Any]]:
    """Add 'label' to each row based on subsequent mid-price movement.

    label = 1.0 if the mid moved adversely by more than adverse_threshold
    ticks within adverse_window_sec (adverse selection: the market ticks
    through our resting quote), else 0.0.

    Uses the true mid from order_book_depths (loaded separately) rather than
    the feature snapshot, so the label reflects actual price action.
    """
    if not rows:
        return rows

    # Load true mids from order_book_depths for the same window, grouped by symbol
    mid_series = _load_mid_series([r["symbol"] for r in rows])

    labelled = []
    for row in rows:
        sym = row["symbol"]
        ts0 = row["ts"] if isinstance(row["ts"], datetime) else _to_dt(row["ts"])
        mids = mid_series.get(sym, [])
        label = _adverse_label(mids, ts0, adverse_window_sec, adverse_threshold_ticks)
        row["label"] = label
        labelled.append(row)
    return labelled


def _to_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    raise TypeError(f"cannot convert {type(value)} to datetime")


def _load_mid_series(symbols: List[str]) -> Dict[str, List[Tuple[datetime, float]]]:
    """Load (ts, mid) series from order_book_depths for all involved symbols."""
    if not symbols:
        return {}
    db = _get_db()
    uniq = sorted(set(symbols))
    out: Dict[str, List[Tuple[datetime, float]]] = {}
    import psycopg
    import psycopg.rows
    with psycopg.connect(db.dsn) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            for sym in uniq:
                cur.execute(
                    """SELECT ts, mid FROM order_book_depths
                       WHERE symbol = %s AND mid IS NOT NULL
                       ORDER BY ts ASC""",
                    (sym,),
                )
                out[sym] = [(r["ts"], float(r["mid"])) for r in cur.fetchall()]
    return out


def _adverse_label(
    mids: List[Tuple[datetime, float]],
    ts0: datetime,
    window_sec: float,
    threshold_ticks: float,
) -> float:
    """1.0 if mid moved adversely (any direction, market-through) in window."""
    if not mids:
        return 0.0
    end_ts = ts0 + timedelta(seconds=window_sec)
    future = [(t, m) for t, m in mids if t > ts0 and t <= end_ts]
    if not future:
        return 0.0
    base_mid = mids[0][1]
    best = max(abs(m - base_mid) for _t, m in future)
    # Without a configured tick-size here we approximate: a "meaningful" move is
    # > 2x the average gap between consecutive mids in the series (noisy if the
    # book is static; adverse if the mid genuinely stepped).
    avg_gap = _avg_mid_gap(mids)
    threshold = threshold_ticks * avg_gap if avg_gap > 0 else 0.5
    return 1.0 if best > threshold else 0.0


def _avg_mid_gap(mids: List[Tuple[datetime, float]]) -> float:
    if len(mids) < 2:
        return 0.0
    deltas = [abs(mids[i + 1][1] - mids[i][1]) for i in range(len(mids) - 1)]
    return sum(deltas) / len(deltas)


def write_labels_back(
    rows: List[Dict[str, Any]],
    adverse_window_sec: float = 5.0,
    adverse_threshold_ticks: float = 0.5,
) -> int:
    """Persist computed labels to ml_features so future training scans skip
    recomputation. Returns the number of rows updated."""
    scored = compute_labels(rows, adverse_window_sec, adverse_threshold_ticks)
    updated = 0
    db = _get_db()
    if db.disabled or not scored:
        return 0
    import psycopg
    with psycopg.connect(db.dsn) as conn:
        with conn.cursor() as cur:
            for row in scored:
                cur.execute(
                    """UPDATE ml_features SET label = %s, label_ts = NOW()
                       WHERE ts = %s AND symbol = %s""",
                    (float(row["label"]), row["ts"], row["symbol"]),
                )
                updated += cur.rowcount
        conn.commit()
    log.info(f"[train] wrote {updated} labels back to ml_features")
    return updated


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------
def train_xgboost(
    X: _np.ndarray,
    y: _np.ndarray,
    config: Optional[Dict] = None,
) -> Tuple[Any, Dict[str, float]]:
    """Train an XGBoost binary classifier. Returns (model, metrics_dict)."""
    cfg = config or {}
    params = {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "max_depth": cfg.get("max_depth", 5),
        "learning_rate": cfg.get("learning_rate", 0.05),
        "n_estimators": cfg.get("n_estimators", 200),
        "min_child_weight": cfg.get("min_child_weight", 10),
        "subsample": cfg.get("subsample", 0.8),
        "colsample_bytree": cfg.get("colsample_bytree", 0.8),
        "reg_alpha": cfg.get("reg_alpha", 0.1),
        "reg_lambda": cfg.get("reg_lambda", 1.0),
        "scale_pos_weight": cfg.get("scale_pos_weight", 1.0),
        "tree_method": "hist",
        "verbosity": 0,
    }
    n = len(X)
    split = int(n * 0.8)
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    # Train with f%d feature names: onnxmltools requires that pattern, and the
    # real FEATURE_NAMES are stored separately in the model registry config.
    xgb_names = [f"f{i}" for i in range(NUM_FEATURES)]
    dtrain = _xgb.DMatrix(X_train, label=y_train, feature_names=xgb_names)
    dval = _xgb.DMatrix(X_val, label=y_val, feature_names=xgb_names)

    model_obj = _xgb.train(
        params,
        dtrain,
        num_boost_round=params["n_estimators"],
        evals=[(dtrain, "train"), (dval, "val")],
        early_stopping_rounds=cfg.get("early_stopping", 20),
        verbose_eval=False,
    )

    train_pred = model_obj.predict(dtrain)
    val_pred = model_obj.predict(dval)

    def _auc(y_true, y_pred):
        pos = sum(y_true)
        neg = len(y_true) - pos
        if pos == 0 or neg == 0:
            return 0.5
        # Simple AUC approximation via Mann-Whitney U
        pos_scores = [y_pred[i] for i in range(len(y_true)) if y_true[i] == 1]
        neg_scores = [y_pred[i] for i in range(len(y_true)) if y_true[i] == 0]
        n_pos = len(pos_scores)
        n_neg = len(neg_scores)
        concordant = 0
        for p in pos_scores:
            concordant += sum(1 for n in neg_scores if p > n)
        return concordant / (n_pos * n_neg)

    metrics = {
        "train_auc": _auc(y_train.tolist(), train_pred.tolist()),
        "val_auc": _auc(y_val.tolist(), val_pred.tolist()),
        "train_samples": int(n),
        "val_samples": int(len(y_val)),
        "positive_rate": float(y.mean()),
    }
    log.info(
        f"[train] XGBoost: train_auc={metrics['train_auc']:.4f} "
        f"val_auc={metrics['val_auc']:.4f} samples={n}"
    )
    return model_obj, metrics


# ---------------------------------------------------------------------------
# ONNX export
# ---------------------------------------------------------------------------
def export_onnx(
    xgb_model,
    output_path: str,
    n_features: int = NUM_FEATURES,
    feature_names: Optional[List[str]] = None,
) -> bool:
    """Export XGBoost model to ONNX format.

    onnxmltools requires features named f0..fN-1, so the model is retrained
    with those names; ``feature_names`` (the real names) are stored in the
    registry/DB config instead.
    """
    try:
        from onnxmltools import convert_xgboost
        from onnxmltools.convert.common.data_types import FloatTensorType

        initial_type = [("float_input", FloatTensorType([None, n_features]))]
        onnx_model = convert_xgboost(xgb_model, initial_types=initial_type)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(onnx_model.SerializeToString())
        log.info(f"[train] exported ONNX model to {output_path}")
        return True
    except ImportError:
        # Fallback: save as native xgboost if onnxmltools missing
        try:
            log.warn("[train] onnxmltools not available — using native xgboost save")
            raw_path = output_path.replace(".onnx", ".xgb")
            xgb_model.save_model(raw_path)
            log.info(f"[train] saved XGBoost model to {raw_path}")
            return True
        except Exception as exc:
            log.error(f"[train] ONNX export failed: {exc!r}")
            return False


# ---------------------------------------------------------------------------
# End-to-end training pipeline
# ---------------------------------------------------------------------------
def run_pipeline(
    name: str = "adverse_selection_v1",
    symbol: Optional[str] = None,
    days: int = 7,
    output_dir: str = "models",
    config: Optional[Dict] = None,
) -> Optional[str]:
    """Full pipeline: load data -> label -> train -> export -> register.

    Returns the model version string on success, None on failure.
    """
    _ensure_deps()
    version = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    log.info(f"[train] === training {name} v{version} ===")

    # 1. Load data
    rows = load_labelled_data(symbol=symbol, days=days)

    # 1b. Backfill labels on unlabelled rows so future scans skip recomputation
    if len(rows) < 100:
        unlabelled = load_labelled_data(
            symbol=symbol, days=days, include_unlabelled=True
        )
        written = write_labels_back(unlabelled)
        rows = load_labelled_data(symbol=symbol, days=days)
        log.info(f"[train] backfilled {written} labels; now {len(rows)} labelled rows")

    if len(rows) < 100:
        log.warn(f"[train] insufficient data ({len(rows)} rows, need ≥100)")
        return None

    # 2. Compute labels if not already present
    rows = compute_labels(rows)

    # 3. Build feature matrix
    X = _np.zeros((len(rows), NUM_FEATURES), dtype=_np.float32)
    y = _np.zeros(len(rows), dtype=_np.float32)
    for i, row in enumerate(rows):
        feat_dict = row["features"]
        if isinstance(feat_dict, str):
            feat_dict = json.loads(feat_dict)
        for j, fname in enumerate(FEATURE_NAMES):
            X[i, j] = float(feat_dict.get(fname, 0.0))
        y[i] = float(row.get("label", 0.0))

    log.info(f"[train] feature matrix: {X.shape}, positive rate: {y.mean():.4f}")

    # 4. Train
    xgb_model, metrics = train_xgboost(X, y, config)

    # 5. Export ONNX
    model_path = os.path.join(output_dir, f"{name}_{version}.onnx")
    os.makedirs(output_dir, exist_ok=True)

    # XGBoost native save (fallback if onnxmltools not available)
    raw_path = model_path.replace(".onnx", ".xgb")
    xgb_model.save_model(raw_path)
    log.info(f"[train] saved model to {raw_path}")

    # Try ONNX export
    export_onnx(xgb_model, model_path)

    # 6. Register in DB
    db = _get_db()
    if not db.disabled:
        try:
            import psycopg
            with psycopg.connect(db.dsn) as conn:
                with conn.cursor() as cur:
                    # Deactivate previous active for this name
                    cur.execute(
                        "UPDATE ml_models SET active = FALSE WHERE name = %s AND active = TRUE",
                        (name,),
                    )
                    cur.execute(
                        """INSERT INTO ml_models
                           (name, version, model_path, feature_names, config,
                            train_samples, train_auc, val_auc, active)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE)""",
                        (
                            name, version, raw_path,
                            json.dumps(list(FEATURE_NAMES)),
                            json.dumps(config or {}),
                            metrics["train_samples"],
                            metrics["train_auc"],
                            metrics["val_auc"],
                        ),
                    )
                conn.commit()
            log.info(f"[train] model registered in DB: {name} v{version}")
        except Exception as exc:
            log.error(f"[train] DB registration failed: {exc!r}")

    log.info(f"[train] === training complete: {name} v{version} ===")
    return version


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Train ML model for market making")
    parser.add_argument("--name", default="adverse_selection_v1", help="Model name")
    parser.add_argument("--symbol", default=None, help="Filter to one symbol")
    parser.add_argument("--days", type=int, default=7, help="Training window (days)")
    parser.add_argument("--output-dir", default="models", help="Model output dir")
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--n-estimators", type=int, default=200)
    parser.add_argument("--learning-rate", type=float, default=0.05)
    args = parser.parse_args()

    config = {
        "max_depth": args.max_depth,
        "n_estimators": args.n_estimators,
        "learning_rate": args.learning_rate,
    }
    run_pipeline(
        name=args.name,
        symbol=args.symbol,
        days=args.days,
        output_dir=args.output_dir,
        config=config,
    )


if __name__ == "__main__":
    main()
