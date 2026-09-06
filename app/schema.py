"""
app/schema.py
-------------
msgspec.Struct definitions shared across every engine.

All inter-engine messages (market ticks, quote decisions, order events,
signal metrics, risk verdicts, engine heartbeats) are serialized with
msgspec.json so the whole pipeline shares one ultra-fast codec.

Every struct here rounds-trips losslessly through Redis pub/sub and is
persisted into PostgreSQL as JSONB in the matching table.
"""

from __future__ import annotations

import time
import msgspec
from typing import Any, Dict, List, Optional

_now = time.time


# =============================================================================
# Market Data
# =============================================================================
class MarketTick(msgspec.Struct, frozen=True):
    ts: float                # unix epoch seconds (wall clock)
    symbol: str
    ltp: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: int = 0
    ask_size: int = 0
    source: str = "fyers_ws"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts, "symbol": self.symbol, "ltp": self.ltp,
            "bid": self.bid, "ask": self.ask, "bid_size": self.bid_size,
            "ask_size": self.ask_size, "source": self.source,
        }


class MarketSnapshot(msgspec.Struct):
    symbol: str
    ltp: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]
    bid_size: int
    ask_size: int
    tick_count: int
    churn_ticks_per_sec: float
    last_tick_ts: float
    is_connected: bool


# =============================================================================
# Transaction Cost
# =============================================================================
class ChargeBreakdown(msgspec.Struct):
    turnover: float
    brokerage: float
    txn: float
    stt_or_ctt: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.txn + self.stt_or_ctt + self.sebi
            + self.stamp + self.gst, 2
        )


class CostQuote(msgspec.Struct):
    """Cost projection for one quote pair / round-trip cycle on an asset."""
    symbol: str
    strategy: str
    qty: int
    segment: str
    lot_size: int
    round_trip_charges_rs: float
    breakeven_spread_ticks: int
    required_spread_ticks: int
    net_profit_per_cycle_rs: float
    each_leg: ChargeBreakdown
    computed_at: float = msgspec.field(default_factory=_now)


# =============================================================================
# Signal Generation
# =============================================================================
class SignalMetrics(msgspec.Struct):
    symbol: str
    strategy: str
    ts: float
    mid: Optional[float]
    churn_ticks_per_sec: float
    vol_widening_ticks: int
    liquidity_grade: float            # 0..1 based on best bid/ask depth
    spread_ticks_now: int             # observed bid-ask width in ticks
    quoteable: bool                   # safe to quote right now
    reasons: List[str] = msgspec.field(default_factory=list)


class TradeSignal(msgspec.Struct):
    symbol: str
    strategy: str
    ts: float
    side: int                         # 1 buy, -1 sell, 0 = no trade
    action: str                       # "OPEN", "EXIT", "HOLD", "SKIP"
    qty: int
    price: float
    signal_sources: List[str] = msgspec.field(default_factory=list)


# =============================================================================
# Risk
# =============================================================================
class MarginCheck(msgspec.Struct):
    symbol: str
    qty: int
    side: int
    limit_price: float
    margin_avail: float
    margin_required: float
    margin_total: float
    buffer_rs: float
    is_sufficient: bool
    code: int = 0
    message: str = ""


class RiskConstraint(msgspec.Struct):
    name: str
    healthy: bool
    detail: str
    ts: float = msgspec.field(default_factory=_now)


class RiskVerdict(msgspec.Struct):
    symbol: str
    strategy: str
    ts: float
    side: int
    qty: int
    price: float
    allowed: bool
    reason: str
    checks: List[RiskConstraint] = msgspec.field(default_factory=list)


# =============================================================================
# Execution / Orders
# =============================================================================
class OrderRequest(msgspec.Struct):
    strategy: str
    symbol: str
    qty: int
    side: int                          # 1 buy, -1 sell
    price: float
    product_type: str = "INTRADAY"
    order_type: int = 1                # 1 = limit
    validity: str = "DAY"


class OrderEvent(msgspec.Struct):
    ts: float
    broker_order_id: str
    strategy: str
    symbol: str
    side: int
    qty: int
    status: int                        # fyers status codes (2 = filled, 6 = open)
    status_label: str
    limit_price: float
    filled_qty: int
    traded_price: float
    raw: Dict[str, Any] = msgspec.field(default_factory=dict)


class QuoteState(msgspec.Struct):
    strategy: str
    symbol: str
    ts: float
    bid_id: Optional[str]
    ask_id: Optional[str]
    bid_price: float
    ask_price: float
    as_qty: int
    status: str


# =============================================================================
# Strategy Engine / Decisions
# =============================================================================
class StrategyInfo(msgspec.Struct):
    name: str
    symbol: str
    segment: str
    enabled: bool
    started_ts: float
    params: Dict[str, Any] = msgspec.field(default_factory=dict)


class DecisionMetrics(msgspec.Struct):
    strategy: str
    ts: float
    decisions_total: int
    decisions_per_min: float
    quotes_placed: int
    quotes_cancelled: int
    fills_received: int
    cycles_completed: int
    realized_pnl_rs: float
    unrealized_pnl_rs: float
    inventory: int
    avg_decision_latency_ms: float
    p99_decision_latency_ms: float
    last_decision: str


# =============================================================================
# Monitor / Heartbeat
# =============================================================================
class EngineHeartbeat(msgspec.Struct):
    engine: str
    ts: float
    status: str                        # healthy | degraded | halted
    process_uptime_sec: float
    loop_latency_ms: float
    processed_count: int
    detail: str = ""


class PipelineSnapshot(msgspec.Struct, kw_only=True):
    ts: float
    engines: List[EngineHeartbeat] = msgspec.field(default_factory=list)
    strategies: List[StrategyInfo] = msgspec.field(default_factory=list)
    decisions: List[DecisionMetrics] = msgspec.field(default_factory=list)
    markets: List[MarketSnapshot] = msgspec.field(default_factory=list)
    risk_active: bool
    risk_healthy: bool
    risk_halts: List[str] = msgspec.field(default_factory=list)
    session_open: bool = True
    session_close_in_sec: float = 0.0


class Command(msgspec.Struct):
    """Control-plane command issued from the Monitor/control UI."""
    type: str                          # PAUSE_STRATEGY, RESUME_STRATEGY, FLATTEN, HALT_ALL, RESET
    target: str = "*"
    ts: float = msgspec.field(default_factory=_now)


json_codec = msgspec.json


def encode(obj) -> bytes:
    return json_codec.encode(obj)


def decode(cls, payload: bytes):
    return json_codec.decode(payload, type=cls)


# Registry of all structs for JSONB columns / generic decoding
STRUCT_REGISTRY: Dict[str, type] = {
    "MarketTick": MarketTick,
    "MarketSnapshot": MarketSnapshot,
    "ChargeBreakdown": ChargeBreakdown,
    "CostQuote": CostQuote,
    "SignalMetrics": SignalMetrics,
    "TradeSignal": TradeSignal,
    "MarginCheck": MarginCheck,
    "RiskConstraint": RiskConstraint,
    "RiskVerdict": RiskVerdict,
    "OrderRequest": OrderRequest,
    "OrderEvent": OrderEvent,
    "QuoteState": QuoteState,
    "StrategyInfo": StrategyInfo,
    "DecisionMetrics": DecisionMetrics,
    "EngineHeartbeat": EngineHeartbeat,
    "PipelineSnapshot": PipelineSnapshot,
    "Command": Command,
}