"""
app/ml/model.py
---------------
ONNX model loading + inference.

Functional interface: load_model() returns a callable predict(features) -> float.
No classes. Model is stored in module-level dict. Inference is <1ms via
onnxruntime with a single CPU execution provider.

If onnxruntime is not installed, falls back to a no-op (returns 0.5 always)
so the bot still boots without ML.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import app.infra.logging as log

# ---------------------------------------------------------------------------
# Module state (no classes)
# ---------------------------------------------------------------------------
_models: Dict[str, Any] = {}            # model_name -> onnxruntime.InferenceSession
_model_meta: Dict[str, Dict] = {}       # model_name -> {version, feature_names, path}
_default_model: Optional[str] = None    # name of the active model for inference

# Try importing onnxruntime; gracefully degrade if absent.
try:
    import onnxruntime as ort
    _HAS_ORT = True
except ImportError:
    ort = None  # type: ignore
    _HAS_ORT = False
    log.warn("[ml] onnxruntime not installed — ML inference disabled (returns 0.5)")


# ---------------------------------------------------------------------------
# Model lifecycle
# ---------------------------------------------------------------------------
def load_model(
    name: str,
    path: str,
    version: str = "",
    feature_names: Optional[List[str]] = None,
) -> bool:
    """Load an ONNX model from disk into module memory.

    Returns True on success. If the file is missing or onnxruntime is absent
    the model is not loaded and False is returned (bot keeps running).
    """
    if not _HAS_ORT:
        log.warn(f"[ml] cannot load {name}: onnxruntime missing")
        return False

    if not os.path.isfile(path):
        log.warn(f"[ml] model file not found: {path}")
        return False

    try:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])
        _models[name] = sess
        _model_meta[name] = {
            "version": version,
            "feature_names": feature_names or [],
            "path": path,
        }
        log.info(f"[ml] loaded model {name!r} v{version} from {path}")
        return True
    except Exception as exc:
        log.error(f"[ml] failed to load model {name!r}: {exc!r}")
        return False


def set_active_model(name: str):
    """Set the default model used by predict()."""
    global _default_model
    if name not in _models:
        log.warn(f"[ml] cannot activate {name!r}: not loaded")
        return
    _default_model = name
    log.info(f"[ml] active model set to {name!r}")


def unload_model(name: str):
    """Remove a model from memory."""
    _models.pop(name, None)
    _model_meta.pop(name, None)
    global _default_model
    if _default_model == name:
        _default_model = None


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
def predict(
    features: List[float],
    model_name: Optional[str] = None,
) -> Tuple[float, float]:
    """Run inference on a single feature vector.

    Returns (raw_output, latency_us).
    raw_output is typically a probability in [0, 1] for binary classifiers.
    Falls back to 0.5 if no model is loaded.
    """
    name = model_name or _default_model
    if name is None or name not in _models:
        return 0.5, 0.0

    sess = _models[name]
    meta = _model_meta[name]
    feat_names = meta.get("feature_names", [])

    try:
        t0 = time.perf_counter()
        # ONNX expects (1, N) float32 tensor
        import numpy as np
        arr = np.array([features], dtype=np.float32)
        input_name = sess.get_inputs()[0].name
        result = sess.run(None, {input_name: arr})
        raw = float(result[0].flatten()[0]) if result else 0.5
        latency_us = (time.perf_counter() - t0) * 1_000_000
        return raw, latency_us
    except Exception as exc:
        log.warn(f"[ml] inference error ({name}): {exc!r}")
        return 0.5, 0.0


def predict_from_dict(
    feat_dict: Dict[str, float],
    ordered_names: List[str],
    model_name: Optional[str] = None,
) -> Tuple[float, float]:
    """Convenience: predict from a feature dict (features.py output)."""
    arr = [float(feat_dict.get(n, 0.0)) for n in ordered_names]
    return predict(arr, model_name)


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------
def loaded_models() -> Dict[str, Dict]:
    """Return metadata for all loaded models."""
    return {
        name: {
            "version": meta["version"],
            "path": meta["path"],
            "n_features": len(meta.get("feature_names", [])),
        }
        for name, meta in _model_meta.items()
    }


def active_model() -> Optional[str]:
    return _default_model


def is_available() -> bool:
    return _HAS_ORT and bool(_models)


def load_active_from_db(database) -> bool:
    """Look up the active model in the ml_models registry and load it.

    Convenience used at boot when ML_MODEL_PATH is not configured: the bot
    picks up whichever model the training pipeline last marked active.
    Returns True if a model was loaded.
    """
    if database is None or getattr(database, "disabled", True):
        return False
    try:
        rows = database.fetch_all(
            "SELECT name, version, model_path FROM ml_models "
            "WHERE active = TRUE ORDER BY created_at DESC LIMIT 1"
        )
    except Exception:
        return False
    if not rows:
        return False
    row = rows[0]
    loaded = load_model(
        name=row["name"],
        path=row["model_path"],
        version=row["version"],
    )
    if loaded:
        set_active_model(row["name"])
    return loaded
