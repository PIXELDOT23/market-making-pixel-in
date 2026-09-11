-- ============================================================================
-- Market-Making Pixel  — ML persistence (PostgreSQL)
-- Full order-book depth, feature store, model registry, prediction log.
-- ============================================================================

-- 10. Full order-book depth snapshots (5 levels, persisted every N sec per symbol)
--     One row per symbol per snapshot interval. Bids/asks stored as JSONB arrays
--     of {price, qty, orders} so the training pipeline can reconstruct the full
--     book without touching the live feed.
CREATE TABLE IF NOT EXISTS order_book_depths (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    ltp         DOUBLE PRECISION NOT NULL,
    mid         DOUBLE PRECISION,
    spread      DOUBLE PRECISION,
    bids        JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{price,qty,orders}, ...] bid-side L1..L5
    asks        JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{price,qty,orders}, ...] ask-side L1..L5
    volume      BIGINT NOT NULL DEFAULT 0,
    churn_tps   DOUBLE PRECISION NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_obd_symbol_ts ON order_book_depths (symbol, ts DESC);
-- Partition by day for fast training-window scans (append-only bulk reads)
CREATE INDEX IF NOT EXISTS idx_obd_ts_day ON order_book_depths ((ts AT TIME ZONE 'UTC')::date);

-- 11. ML feature vectors (computed every tick interval, one row per symbol)
--     Pre-computed numeric features so the training pipeline avoids
--     re-computing from raw depth on every epoch.
CREATE TABLE IF NOT EXISTS ml_features (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    features    JSONB NOT NULL,          -- dict of named floats
    label       DOUBLE PRECISION,        -- target variable (NULL until labelled)
    label_ts    TIMESTAMPTZ              -- when the label was resolved
);
CREATE INDEX IF NOT EXISTS idx_mlf_symbol_ts ON ml_features (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_mlf_labelled ON ml_features (label) WHERE label IS NOT NULL;

-- 12. ML model registry (one row per trained model version)
CREATE TABLE IF NOT EXISTS ml_models (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,            -- e.g. "adverse_selection_v1"
    version         TEXT NOT NULL,            -- e.g. "20260910_143000"
    model_path      TEXT NOT NULL,            -- local file path to .onnx
    feature_names   JSONB NOT NULL,           -- ordered list of feature keys
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    train_samples   INTEGER,
    train_auc       DOUBLE PRECISION,
    val_auc         DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active          BOOLEAN NOT NULL DEFAULT FALSE,  -- only one active per name
    UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_mlm_name_active ON ml_models (name, active) WHERE active = TRUE;

-- 13. ML prediction log (every inference is logged for offline analysis)
CREATE TABLE IF NOT EXISTS ml_predictions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    model_name  TEXT NOT NULL,
    model_version TEXT NOT NULL,
    features    JSONB NOT NULL,              -- snapshot of input features
    prediction  DOUBLE PRECISION NOT NULL,   -- raw model output (0..1 probability)
    action      TEXT NOT NULL,               -- "WIDEN" | "HOLD" | "SKIP"
    actual      DOUBLE PRECISION,            -- filled later from execution log
    actual_ts   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_mlp_symbol_ts ON ml_predictions (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_mlp_model ON ml_predictions (model_name, model_version);

-- 14. ML training runs (audit log for the offline pipeline)
CREATE TABLE IF NOT EXISTS ml_training_runs (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | success | failed
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    model_version   TEXT,
    error           TEXT
);
