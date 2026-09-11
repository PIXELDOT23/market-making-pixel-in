# ML for Adverse-Selection & Spread Widening

## Why ML at all?

The bot currently guards adverse selection with two **linear** rules:

1. **OBI gate** — refuse to quote a side when the book is more than `obi_gate_ratio`
   (3.0x) lopsided.
2. **Churn widening** — widen the spread by `churn × volatility_widen_factor`.

The interaction between **order-book depth**, **churn**, **spread state** and
**time-of-day** is non-linear. A fixed 3x ratio treats a quiet, deep book the
same as a thin, hot one. ML replaces those two fixed points with a learned
classifier over the full 5-level book.

## What we implement (and what we deliberately skip)

| Area                | Decision    | Why                                                          |
|---------------------|-------------|--------------------------------------------------------------|
| Adverse selection prediction | **Yes** | The single highest-value, naturally labelled problem. |
| Dynamic spread widening      | **Yes** | Replaces the linear `churn * 2.0`, model output feeds extra ticks. |
| Scanner ranking      | Skip        | Already adaptive via RoM weighting (realized PnL per margin). |
| Position sizing      | Skip        | Governance decisions (95% breadth, qty=1) should NOT be learned. |
| Directional prediction | Skip      | Market can't be called; we only need risk timing. |
| Deep RL             | Skip        | qty=1 + breadth strategy doesn't need reinforcement. |

## Architecture

```
Fyers WS ──► DataEngine (5-level depth in memory)
                 │  every tick
                 ▼
        app/ml/engine.py  (functional, module state only)
                 │  on_tick(...)
                 ├─► features.py  → 27-dim vector (<100us)
                 ├─► model.py     → ONNX session, ~20us inference
                 └─► db.buffer_write (batch)  → order_book_depths,
                                                     ml_features, ml_predictions
                                                          │
                                                          ▼ (offline)
        app/ml/train.py  → XGBoost → ONNX → ml_models table → models/*.onnx
```

## Feature vector (27 dims, see `app/ml/features.py`)

| Group            | Features                                                |
|------------------|---------------------------------------------------------|
| Depth levels     | bid_depth_1..5, ask_depth_1..5 (10)                     |
| Imbalance        | obi_1, obi_3, obi_5, obi_weighted (4)                  |
| Spread           | spread_ticks, spread_pct, spread_zscore (3)            |
| Microstructure   | bid_ask_ratio, total_bid_qty, total_ask_qty, mid_return_1t/5t/10t, churn_5s, churn_30s (8) |
| Time             | hour_sin, hour_cos (2)                                 |

All functions are **pure** (`depth_features`, `imbalance_features`,
`spread_features`, `microstructure_features`, `time_features`,
`compute_features`, `features_to_array`). No classes → no vtable/jit overhead,
flat module-level dicts.

## Label definition (adverse selection)

A feature row is **positive** (`label = 1`) if, within the next 5 seconds, the
true mid (read from `order_book_depths`) stepped by more than
`adverse_threshold_ticks × avg_mid_gap` — i.e. the market traded through our
resting quote. Otherwise 0. Labels are resolved offline by
`train.compute_labels` and written back into `ml_features` so repeated training
runs don't recompute.

## Data capture (live)

Capture runs **independently of inference** — controlled by
`ML_CAPTURE_ENABLED` (default `1`), so `order_book_depths` / `ml_features`
accumulate training data from day one even before a model exists.

- **Every tick** → `engine.on_tick()` keeps rolling mid/spread history; runs
  inference only when `ML_ENABLED=1`. `ml_predictions` row per optimistic batch.
- **Throttled**: `order_book_depths` snapshot every `ml_depth_persist_interval_sec`
  (default 1s) per symbol; `ml_features` row every `ml_feature_persist_interval_sec`
  (default 0.5s).
- All writes go through the existing **batched** `Database.buffer_write_sync()` +
  `Database.flush()` — synchronous enqueue (no coroutine left un-awaited), never
  a network round-trip on the hot path.

## Inference output → spread decision

```
P(adverse) >= ml_widen_threshold (0.65)  →  add ml_widen_extra_ticks (1) to widening
CAM (cap at max_spread_widen_ticks)     →  final vol_widening_ticks
```

Consumed in `StrategyEngine._on_tick` on top of the churn widening. If ML is
disabled (`ML_ENABLED=0`) the bot behaves exactly as before.

## Training (offline, outside the bot)

```bash
.venv/bin/pip install xgboost onnxmltools onnxconverter-common
DATABASE_URL=postgresql://postgres:PixelIn@localhost:5432/pixel_in \
  .venv/bin/python -m app.ml.train --name adverse_selection_v1 --days 7 \
      --max-depth 5 --n-estimators 300
```

Pipeline: load raw `ml_features` → backfill labels from `order_book_depths` →
train XGBoost (binary:logistic, early stopping, AUC) → export ONNX →
insert `ml_models` (deactivating any previously-active version).

## Deployment (live)

- `ML_CAPTURE_ENABLED=1` (default) — depth + feature collection always on.
- `ML_ENABLED=1` — additionally run the model and widen the book.
- `ML_MODEL_PATH=models/adverse_selection_v1_*.onnx` — or leave unset to
  auto-load the **active** model from the `ml_models` registry at boot
  (`model.load_active_from_db`).

## Performance

Measured (smoke test, 27 dims, CPU):

- ONNX inference: **~20μs** (target <1ms — 50x headroom)
- Feature compute: **<100μs**
- Per-tick total added latency: **~250μs** (includes persistence throttle logic)

## Operational notes

- `onnxruntime` is optional at runtime. If absent, `model.predict` returns 0.5
  (no widening) and the bot runs unchanged — the whole ML layer degrades
  gracefully.
- Feature/label schema lives in `db/ml_schema.sql` (standalone) and is also
  appended to `db/schema.sql`. Tables: `order_book_depths`, `ml_features`,
  `ml_models`, `ml_predictions`, `ml_training_runs`.
- Monitor endpoint: `GET /api/ml` returns enabled state, loaded models, config.
- Data volume: 27 floats/row ≈ 220 bytes JSON, ~1 row/s per symbol at the
  default throttle — negligible vs `market_ticks`.