"""
app/strategies/base.py
----------------------
Base class for every live strategy.

A strategy:
  * is created with a name + asset (symbol)
  * runs as an asyncio task inside the StrategyEngine
  * consumes the latest market snapshot + signal metrics
  * asks the RiskEngine for a pre-trade verdict
  * asks the ExecutionEngine to place/cancel/replace quotes
  * reports DecisionMetrics (produced by the StrategyEngine)

Strategies never touch FYERS or Redis directly — only engines.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app import schema


class BaseStrategy(ABC):
    def __init__(
        self,
        name: str,
        symbol: str,
        segment: str,
        params: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.symbol = symbol
        self.segment = segment
        self.params = params or {}
        self.enabled = True
        self.started_ts = time.time()

        # decision metrics (measured by the StrategyEngine)
        self.decisions_total = 0
        self.quotes_placed = 0
        self.quotes_cancelled = 0
        self.fills_received = 0
        self.cycles_completed = 0
        self.realized_pnl_rs = 0.0
        self._last_realized_baseline = 0.0
        self._decision_latencies: List[float] = []

    # ------------------------------------------------------------------ hooks
    @abstractmethod
    async def on_signal(
        self,
        snapshot: schema.MarketSnapshot,
        signal: schema.SignalMetrics,
        cost: schema.CostQuote,
        engines,
    ) -> str:
        """Decide what to do. Returns a short decision label."""

    def on_fill(self, ev: schema.OrderEvent):
        self.fills_received += 1

    def on_cycle_closed(self, realized_pnl_total_rs: float):
        """Accrue only the delta since the last closed cycle (the risk engine
        reports a running total, not a per-cycle value)."""
        delta = realized_pnl_total_rs - self._last_realized_baseline
        self._last_realized_baseline = realized_pnl_total_rs
        if abs(delta) > 1e-9:
            self.realized_pnl_rs += delta
        self.cycles_completed += 1

    def info(self) -> schema.StrategyInfo:
        return schema.StrategyInfo(
            name=self.name, symbol=self.symbol, segment=self.segment,
            enabled=self.enabled, started_ts=self.started_ts, params=self.params,
        )

    def record_latency(self, ms: float):
        self._decision_latencies.append(ms)
        if len(self._decision_latencies) > 10_000:
            self._decision_latencies = self._decision_latencies[-10_000:]

    @property
    def avg_latency_ms(self) -> float:
        if not self._decision_latencies:
            return 0.0
        return sum(self._decision_latencies) / len(self._decision_latencies)

    @property
    def p99_latency_ms(self) -> float:
        if not self._decision_latencies:
            return 0.0
        s = sorted(self._decision_latencies)
        idx = int(len(s) * 0.99)
        return s[min(idx, len(s) - 1)]


class StrategyUniverse:
    """Registry of strategies to bring live at engine boot."""

    def build(self) -> List[BaseStrategy]:
        from app.config import settings
        from app.strategies.market_maker import MarketMakerStrategy

        return [
            MarketMakerStrategy(
                name="mm_natgas",
                symbol=settings.symbol,
                segment=settings.segment,
                params={
                    "qty": settings.quote_qty,
                    "spread_ticks": settings.spread_ticks,
                    "min_profit_margin_ticks": settings.min_profit_margin_ticks,
                },
            )
        ]