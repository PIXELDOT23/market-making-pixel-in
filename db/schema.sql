-- ============================================================================
-- Market-Making Pixel  — PostgreSQL schema (low-latency engine persistence)
-- Mirrors the msgspec structs in app/schema.py (structs are stored as JSONB).
-- ============================================================================

-- 1. Market ticks (batched writes from DataEngine)
CREATE TABLE IF NOT EXISTS market_ticks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    ltp         DOUBLE PRECISION NOT NULL,
    bid         DOUBLE PRECISION,
    ask         DOUBLE PRECISION,
    bid_size    BIGINT      NOT NULL DEFAULT 0,
    ask_size    BIGINT      NOT NULL DEFAULT 0,
    source      TEXT        NOT NULL DEFAULT 'fyers_ws'
);
CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_ts ON market_ticks (symbol, ts DESC);

-- 2. Signal metrics (SignalEngine)
CREATE TABLE IF NOT EXISTS signals (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    metrics     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals (symbol, ts DESC);

-- 3. Cost quotes (CostEngine)
CREATE TABLE IF NOT EXISTS cost_quotes (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    quote       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_quotes_symbol_ts ON cost_quotes (symbol, ts DESC);

-- 4. Risk verdicts (RiskEngine pre-trade gate)
CREATE TABLE IF NOT EXISTS risk_verdicts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      TEXT NOT NULL,
    strategy    TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    verdict     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_verdicts_strategy_ts ON risk_verdicts (strategy, ts DESC);

-- 5. Order events (ExecutionEngine real-time WS)
CREATE TABLE IF NOT EXISTS order_events (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    broker_order_id TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            INT NOT NULL,
    status          INT NOT NULL,
    event           JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_events_symbol_ts ON order_events (symbol, ts DESC);

-- 6. Decision metrics (StrategyEngine)
CREATE TABLE IF NOT EXISTS decisions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy    TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    metrics     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_strategy_ts ON decisions (strategy, ts DESC);

-- 7. Engine heartbeats (MonitorEngine / every engine)
CREATE TABLE IF NOT EXISTS engine_heartbeats (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    engine          TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL,
    latency_ms      DOUBLE PRECISION,
    processed_count BIGINT NOT NULL DEFAULT 0,
    heartbeat       JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_engine_heartbeats_ts ON engine_heartbeats (engine, ts DESC);

-- 8. Live strategy registry
CREATE TABLE IF NOT EXISTS strategies (
    name        TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    segment     TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    started_ts  TIMESTAMPTZ NOT NULL,
    params      JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 9. Retained market snapshot (latest per symbol, for dashboard)
CREATE TABLE IF NOT EXISTS market_snapshots (
    symbol      TEXT PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    ltp         DOUBLE PRECISION,
    bid         DOUBLE PRECISION,
    ask         DOUBLE PRECISION,
    bid_size    BIGINT DEFAULT 0,
    ask_size    BIGINT DEFAULT 0
);

-- ============================================================================
-- ML tables (see also db/ml_schema.sql for standalone migration)
-- ============================================================================

-- 10. Full order-book depth snapshots (5 levels, persisted every N sec)
CREATE TABLE IF NOT EXISTS order_book_depths (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    ltp         DOUBLE PRECISION NOT NULL,
    mid         DOUBLE PRECISION,
    spread      DOUBLE PRECISION,
    bids        JSONB NOT NULL DEFAULT '[]'::jsonb,
    asks        JSONB NOT NULL DEFAULT '[]'::jsonb,
    volume      BIGINT NOT NULL DEFAULT 0,
    churn_tps   DOUBLE PRECISION NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_obd_symbol_ts ON order_book_depths (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_obd_ts_day ON order_book_depths ((ts AT TIME ZONE 'UTC')::date);

-- 11. ML feature vectors
CREATE TABLE IF NOT EXISTS ml_features (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    features    JSONB NOT NULL,
    label       DOUBLE PRECISION,
    label_ts    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_mlf_symbol_ts ON ml_features (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_mlf_labelled ON ml_features (label) WHERE label IS NOT NULL;

-- 12. ML model registry
CREATE TABLE IF NOT EXISTS ml_models (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    model_path      TEXT NOT NULL,
    feature_names   JSONB NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    train_samples   INTEGER,
    train_auc       DOUBLE PRECISION,
    val_auc         DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    active          BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_mlm_name_active ON ml_models (name, active) WHERE active = TRUE;

-- 13. ML prediction log
CREATE TABLE IF NOT EXISTS ml_predictions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    model_name  TEXT NOT NULL,
    model_version TEXT NOT NULL,
    features    JSONB NOT NULL,
    prediction  DOUBLE PRECISION NOT NULL,
    action      TEXT NOT NULL,
    actual      DOUBLE PRECISION,
    actual_ts   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_mlp_symbol_ts ON ml_predictions (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_mlp_model ON ml_predictions (model_name, model_version);

-- 14. ML training runs
CREATE TABLE IF NOT EXISTS ml_training_runs (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'running',
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics         JSONB NOT NULL DEFAULT '{}'::jsonb,
    model_version   TEXT,
    error           TEXT
);