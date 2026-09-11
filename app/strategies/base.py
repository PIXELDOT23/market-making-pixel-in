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
    """Registry of strategies to bring live at engine boot.

    With a multi-asset ``UNIVERSE`` set this brings one market-making strategy
    live per instrument (commodity, equity futures, cash equity can coexist).
    Without it, the legacy single ``SYMBOL`` settings path is used.
    """

    def __init__(self):
        self._instruments: Optional[List["Instrument"]] = None

    def instruments(self) -> List["Instrument"]:
        if self._instruments is None:
            from app.config import settings
            from app.infra.instrument import instrument_registry, parse_universe_all

            instruments = parse_universe_all(
                settings.universe,
                settings.symbol,
                settings.asset_type or settings.segment,
                settings.lot_size,
                settings.tick_size,
                settings.margin_per_lot_rs,
            )
            if settings.nse_all_equity_scan or settings.mcx_all_futures_scan:
                instruments = self._merge_whole_market(instruments)
            self._register(instruments)
        return self._instruments

    async def instruments_async(self) -> List["Instrument"]:
        """Like ``instruments()`` but runs the whole-market master-contract
        download off the event loop so boot never blocks."""
        if self._instruments is None:
            from app.config import settings
            from app.infra.instrument import instrument_registry, parse_universe_all

            instruments = parse_universe_all(
                settings.universe,
                settings.symbol,
                settings.asset_type or settings.segment,
                settings.lot_size,
                settings.tick_size,
                settings.margin_per_lot_rs,
            )
            if settings.nse_all_equity_scan or settings.mcx_all_futures_scan:
                instruments = await self._merge_whole_market_async(instruments)
            self._register(instruments)
        return self._instruments

    def _register(self, instruments: List["Instrument"]):
        from app.infra.instrument import instrument_registry

        instrument_registry.clear()
        for inst in instruments:
            instrument_registry.register(inst)
        self._instruments = instruments

    @staticmethod
    def _merge_whole_market(explicit: List["Instrument"]) -> List["Instrument"]:
        """Union of an explicit UNIVERSE with the whole NFO + MCX future
        masters (deduped, explicit entries win). Master download failures are
        logged and fell back to whatever the explicit set covers."""
        from app.config import settings
        from app.infra import logging as log
        from app.infra import master_contracts

        master = []
        try:
            master = master_contracts.whole_market(
                settings.nse_all_equity_scan,
                settings.mcx_all_futures_scan,
                mcx_near_contract_only=settings.mcx_near_contract_only,
                nfo_near_contract_only=settings.nfo_near_contract_only,
            )
            log.info(
                f"whole-market universe: {len(master)} instruments "
                f"(nfo_futures={settings.nse_all_equity_scan}, mcx={settings.mcx_all_futures_scan})"
            )
        except Exception as e:
            log.warn(f"whole-market master fetch failed ({e}); using configured universe")

        by_symbol = {inst.symbol: inst for inst in explicit}
        for inst in master:
            by_symbol.setdefault(inst.symbol, inst)
        merged = list(by_symbol.values())
        log.info(f"merged universe total: {len(merged)} instruments")
        return merged

    @staticmethod
    async def _merge_whole_market_async(explicit: List["Instrument"]) -> List["Instrument"]:
        """Async whole-market merge; the blocking master-contract I/O is moved
        to threads so a stale-cache refresh can never stall the event loop."""
        from app.config import settings
        from app.infra import logging as log
        from app.infra import master_contracts

        master = []
        try:
            master = await master_contracts.whole_market_async(
                settings.nse_all_equity_scan,
                settings.mcx_all_futures_scan,
                mcx_near_contract_only=settings.mcx_near_contract_only,
                nfo_near_contract_only=settings.nfo_near_contract_only,
            )
            log.info(
                f"whole-market universe: {len(master)} instruments "
                f"(nfo_futures={settings.nse_all_equity_scan}, mcx={settings.mcx_all_futures_scan})"
            )
        except Exception as e:
            log.warn(f"whole-market master fetch failed ({e}); using configured universe")

        by_symbol = {inst.symbol: inst for inst in explicit}
        for inst in master:
            by_symbol.setdefault(inst.symbol, inst)
        merged = list(by_symbol.values())
        log.info(f"merged universe total: {len(merged)} instruments")
        return merged

    @staticmethod
    def _strategy_name(symbol: str) -> str:
        safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in symbol.lower())
        return f"mm_{safe}"

    def build(self) -> List[BaseStrategy]:
        from app.config import settings
        from app.strategies.market_maker import MarketMakerStrategy

        return [
            MarketMakerStrategy(
                name=self._strategy_name(inst.symbol),
                symbol=inst.symbol,
                segment=inst.segment.value,
                params={
                    # commodity/equity size is decided per decision by the margin/vol
                    # sizer anyway; this qty is the strategy fallback only.
                    "qty": 1,
                    "spread_ticks": settings.spread_ticks,
                    "min_profit_margin_ticks": settings.min_profit_margin_ticks,
                    "asset_type": inst.asset_type.value,
                    "lot_size": inst.lot_size,
                    "tick_size": inst.tick_size,
                    "margin_per_lot_rs": inst.margin_per_lot_rs,
                },
            )
            for inst in self.instruments()
        ]

    async def build_async(self) -> List[BaseStrategy]:
        """Async build: resolves the universe (incl. whole-market download)
        without blocking the event loop."""
        from app.config import settings
        from app.strategies.market_maker import MarketMakerStrategy

        instruments = await self.instruments_async()
        return [
            MarketMakerStrategy(
                name=self._strategy_name(inst.symbol),
                symbol=inst.symbol,
                segment=inst.segment.value,
                params={
                    "qty": 1,
                    "spread_ticks": settings.spread_ticks,
                    "min_profit_margin_ticks": settings.min_profit_margin_ticks,
                    "asset_type": inst.asset_type.value,
                    "lot_size": inst.lot_size,
                    "tick_size": inst.tick_size,
                    "margin_per_lot_rs": inst.margin_per_lot_rs,
                },
            )
            for inst in instruments
        ]