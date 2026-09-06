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
  is_connected: boolean
}

export interface PipelineSnapshot {
  ts: number
  engines: EngineHeartbeat[]
  strategies: StrategyInfo[]
  decisions: DecisionMetrics[]
  markets: MarketSnapshot[]
  risk_active: boolean
  risk_healthy: boolean
  risk_halts: string[]
  session_open: boolean
  session_close_in_sec: number
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