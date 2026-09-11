export type Status = "healthy" | "degraded" | "halted" | "booting" | "stopped"

export interface EngineHeartbeat {
  engine: string
  ts: number
  status: Status
  process_uptime_sec: number
  loop_latency_ms: number
  processed_count: number
  detail: string
}

export interface StrategyInfo {
  name: string
  symbol: string
  segment: string
  enabled: boolean
  started_ts: number
  params: Record<string, unknown>
}

export interface DecisionMetrics {
  strategy: string
  ts: number
  decisions_total: number
  decisions_per_min: number
  quotes_placed: number
  quotes_cancelled: number
  fills_received: number
  cycles_completed: number
  realized_pnl_rs: number
  unrealized_pnl_rs: number
  inventory: number
  avg_decision_latency_ms: number
  p99_decision_latency_ms: number
  last_decision: string
}

export interface MarketSnapshot {
  symbol: string
  ltp: number | null
  bid: number | null
  ask: number | null
  mid: number | null
  bid_size: number
  ask_size: number
  tick_count: number
  churn_ticks_per_sec: number
  last_tick_ts: number
  volume: number
  is_connected: boolean
  bids?: DepthLevel[]
  asks?: DepthLevel[]
}

export interface DepthLevel {
  price: number
  qty: number
  orders: number
}

export interface ScannerRow {
  symbol: string
  rank: number
  segment: string
  asset_type: string
  ltp: number | null
  bid: number | null
  ask: number | null
  mid: number | null
  bid_size: number
  ask_size: number
  spread_ticks: number
  liquidity_grade: number
  churn_ticks_per_sec: number
  vol_widening_ticks: number
  volume: number
  quoteable: boolean
  margin_avail: number
  margin_per_lot: number
  quote_qty: number
  lot_size: number
  margin_req_rs: number
  score: number
  net_profit_rs: number
  round_trip_charges_rs: number
  breakeven_spread_ticks: number
  required_spread_ticks: number
  profitable: boolean
  weight: number
  size_mult: number
  reasons: string[]
  ts: number
}

export interface AssetPnl {
  symbol: string
  position: number
  entry: number
  realized_pnl_rs: number
  unrealized_pnl_rs: number
  total_pnl_rs: number
  open_age_sec: number
  lot_size: number
  last_fill_price: number
}

export interface AssetDetail {
  symbol: string
  ts: number | null
  session_open: boolean
  session_label: string
  snapshot: MarketSnapshot | null
  row: ScannerRow | null
  pnl: AssetPnl | null
  strategy: DecisionMetrics | null
}

export interface SegmentStatus {
  segment: string
  label: string
  open: boolean
  close_in_sec: number
}

export interface PipelineSnapshot {
  ts: number
  engines: EngineHeartbeat[]
  strategies: StrategyInfo[]
  decisions: DecisionMetrics[]
  markets: MarketSnapshot[]
  scanner: ScannerRow[]
  risk_active: boolean
  risk_healthy: boolean
  risk_halts: string[]
  session_open: boolean
  session_close_in_sec: number
  segments: SegmentStatus[]
}

export interface AuthInfo {
  logged_in: boolean
  token_cached?: boolean
  expires_in_sec: number
  client_id: string
  has_refresh: boolean
  reason?: string
}

export interface RiskConstraint {
  name: string
  healthy: boolean
  detail: string
}

export interface RiskStatus {
  halts: string[]
  net_position: number
  realized_pnl_rs: number
  status: Status
  constraints: RiskConstraint[]
}

export interface OrderEvent {
  ts: number
  broker_order_id: string
  strategy: string
  symbol: string
  side: number
  qty: number
  status: number
  status_label: string
  limit_price: number
  filled_qty: number
  traded_price: number
  raw: Record<string, unknown>
}

export interface TableCount {
  t: string
  count: number
}

export type CommandName =
  | "PAUSE_STRATEGY"
  | "RESUME_STRATEGY"
  | "FLATTEN"
  | "HALT_ALL"
  | "RESET"