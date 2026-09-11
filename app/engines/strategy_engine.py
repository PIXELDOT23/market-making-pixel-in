"""
app/engines/strategy_engine.py
------------------------------
STRATEGY ENGINE

  * Lists every strategy that is live (registry -> StrategyUniverse).
  * Runs each strategy's decision loop against fresh snapshots, signal
    metrics and cost quotes (all pushed on the Redis bus by their engines).
  * Measures the decisions: latency, quote placement/cancel counts,
    fills, cycles completed, realized/unrealized PnL, and publishes
    DecisionMetrics for the Monitor engine.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Optional

from app import schema
from app import sizing as sizing_mod
from app.config import settings
from app.engines.base import Engine
from app.infra import logging as log
from app.infra.db import Database
from app.infra.redis import RedisBus
from app.scanner import RowBuilder, ScannerCache, assign_rom_weights, compute_row
from app.strategies.base import BaseStrategy, StrategyUniverse
from app.weighting import WeightTracker
from app.ml import engine as ml_engine


def _exchange_of(symbol: str) -> str:
    """Exchange id ("NSE" | "BSE" | "MCX" | "") from a broker symbol prefix."""
    prefix = str(symbol).split(":", 1)[0].upper()
    return prefix if prefix in ("NSE", "BSE", "MCX") else ""


def _consume_margin_result(task: asyncio.Task):
    """Sink for background margin-refresh tasks: surface failures but never
    propagate (they run detached from the scanner loop)."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        log.warn(f"[scanner] background margin refresh: {exc!r}")


class StrategyEngine(Engine):
    name = "strategy_engine"

    def __init__(self, bus: RedisBus, db: Database, swarm: StrategyUniverse, engines: Dict[str, object]):
        super().__init__(bus, db)
        self.swarm = swarm
        self.engines = engines                # {risk, execution, data, cost, signal}
        self.strategies: Dict[str, BaseStrategy] = {}
        self._by_symbol: Dict[str, BaseStrategy] = {}
        self._latest_signal: Dict[str, schema.SignalMetrics] = {}
        self._last_decision_ts: Dict[str, float] = {}
        self._no_mid_warned: set = set()
        self._no_strategy_warned: set = set()
        self._listener_task: Optional[asyncio.Task] = None
        self.scanner: Optional[ScannerCache] = None
        self._scanner_task: Optional[asyncio.Task] = None
        self._scanner_builder: Optional[RowBuilder] = None
        self._last_metrics: Dict[str, tuple] = {}
        self._margin_refresh_task: Optional[asyncio.Task] = None
        self._scanner_active_line: str = ""
        self._weight_tracker = WeightTracker(settings.weight_smoothing_alpha)

    # ------------------------------------------------------------------ lifecycle
    async def start_strategies(self):
        from app.config import settings
        from app.infra import master_contracts

        for strategy in await self.swarm.build_async():
            self.strategies[strategy.name] = strategy
            self._by_symbol[strategy.symbol] = strategy
            await self.db.upsert_strategy(strategy.info())
        # flag MCX/NFO contracts whose near expiry has passed (rollover = restart)
        expired = [
            s.symbol for s in self.strategies.values()
            if s.segment == "MCX" and master_contracts.is_mcx_expired(s.symbol)
        ]
        if expired:
            log.warn(
                f"[strategy] {len(expired)} MCX near contract(s) already expired "
                f"({', '.join(sorted(expired)[:6])}...) — restart to roll over to "
                f"the next near contract."
            )
        # register all strategy symbols in the data engine after build
        symbols = [s.symbol for s in self.strategies.values()]
        if self.engines.get("data") is not None:
            self.engines["data"].register_symbols(symbols)
            self.engines["data"].set_tick_sizes({
                s.symbol: self._tick_size(s) for s in self.strategies.values()
            })
        # register symbols with ML engine for feature extraction
        for sym in symbols:
            ml_engine.register_symbol(sym)
        # rank the whole traded universe for market making
        instruments = await self.swarm.instruments_async()
        if instruments:
            self.scanner = ScannerCache(
                instruments, active_limit=settings.max_scanner_active
            )
            self.scanner.refresh_sec = settings.scanner_refresh_sec
            if settings.weight_enabled:
                self.scanner._weight_assigner = self._apply_scanner_weights
            self.engines["scanner"] = self.scanner

    def strategy_list(self) -> List[schema.StrategyInfo]:
        return [s.info() for s in self.strategies.values()]

    def scanner_rows(self) -> List[schema.ScannerRow]:
        return self.scanner.rows() if self.scanner is not None else []

    def strategy_metrics(self) -> List[schema.DecisionMetrics]:
        out = []
        for name, s in self.strategies.items():
            m = self._build_metrics(name, s)
            if m is not None:
                out.append(m)
        return out

    def _build_metrics(self, name: str, s: BaseStrategy) -> Optional[schema.DecisionMetrics]:
        window = max(time.time() - s.started_ts, 1)
        return schema.DecisionMetrics(
            strategy=name,
            ts=time.time(),
            decisions_total=s.decisions_total,
            decisions_per_min=round(s.decisions_total / (window / 60.0), 2),
            quotes_placed=s.quotes_placed,
            quotes_cancelled=s.quotes_cancelled,
            fills_received=s.fills_received,
            cycles_completed=s.cycles_completed,
            realized_pnl_rs=round(s.realized_pnl_rs, 2),
            unrealized_pnl_rs=0.0,  # refreshed from risk inventory in monitor
            inventory=0,
            avg_decision_latency_ms=round(s.avg_latency_ms, 3),
            p99_decision_latency_ms=round(s.p99_latency_ms, 3),
            last_decision=s.__dict__.get("last_decision", "-"),
        )

    # ------------------------------------------------------------------ main loop
    async def run(self):
        await self.start_strategies()

        # collect market ticks as they land (through the data engine memory)
        async def market_handler(ch: str, raw: bytes):
            try:
                tick = schema.decode(schema.MarketTick, raw)
                await self._on_tick(tick)
            except Exception as exc:
                log.error(f"[market handler] {exc!r}")
        await self.bus.subscribe("fyers:market:*", handler=market_handler)

        async def signal_handler(ch: str, raw: bytes):
            try:
                sig = schema.decode(schema.SignalMetrics, raw)
                self._latest_signal[sig.symbol] = sig
            except Exception as exc:
                log.error(f"[signal handler] {exc!r}")
        await self.bus.subscribe("fyers:signal:*", handler=signal_handler)

        if self.scanner is not None:
            self._scanner_builder = self._make_scanner_builder()
            self._scanner_task = asyncio.create_task(self._scanner_loop())
            self._scanner_task.add_done_callback(self._scanner_done)

        hb = asyncio.create_task(self._heartbeat_loop())
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            try:
                await self._publish_metrics()
            except Exception as exc:
                log.warn(f"[strategy] publish metrics error: {exc!r}")
        hb.cancel()
        if self._scanner_task is not None:
            self._scanner_task.cancel()
            try:
                await self._scanner_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._margin_refresh_task is not None:
            self._margin_refresh_task.cancel()
            try:
                await self._margin_refresh_task
            except (asyncio.CancelledError, Exception):
                pass

    def _scanner_done(self, task: asyncio.Task):
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warn(f"[strategy] scanner task exited: {exc!r}")

    async def _scanner_loop(self):
        if self.scanner is None:
            return
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(self.scanner.refresh_sec, 0.1)
                )
                break
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            try:
                # Warm real per-symbol broker margins (top-ranked first) so the
                # scanner shows the broker's figure instead of an estimate. This
                # is OFF the critical path: the per-symbol REST calls run in a
                # background task so a slow broker response can never stall the
                # scanner rebuild below (low latency).
                risk = self.engines.get("risk")
                if (
                    risk is not None
                    and self.scanner is not None
                    and (self._margin_refresh_task is None or self._margin_refresh_task.done())
                ):
                    data = self.engines.get("data")
                    symbols = [r.symbol for r in self.scanner.rows()]
                    if not symbols:
                        symbols = [inst.symbol for inst in self.scanner.instruments]
                    prices = {}
                    if data is not None:
                        for s in symbols:
                            snap = data.snapshot(s)
                            if snap is not None and snap.mid:
                                prices[s] = snap.mid
                    self._margin_refresh_task = asyncio.create_task(
                        risk.refresh_margins(symbols, prices)
                    )
                    self._margin_refresh_task.add_done_callback(_consume_margin_result)
                self.scanner.refresh(self._scanner_builder)
                # Push the selected symbols' reservation into the risk engine's
                # global margin ledger so per-symbol sizing and the pre-trade
                # gate see one aggregated budget across the whole book. Stored
                # amounts are reserve-multiplied to match the scanner's greedy
                # budget (both-sides pairs reserve for both legs).
                risk = self.engines.get("risk")
                if risk is not None:
                    from app.config import settings
                    mult = 2.0 if (
                        settings.margin_ledger_enabled and settings.margin_reserve_both_sides
                    ) else 1.0
                    risk.sync_margin_bookings({
                        r.symbol: r.margin_req_rs * mult
                        for r in self.scanner.rows()
                        if r.quoteable and r.margin_req_rs > 0
                    })
                # Log only rows that are *actually* resting quotes, and only when
                # the set changes — the ranked top-N also contains feed-down /
                # margin-blocked names and reprinting it every second floods the
                # log (and the UI) with identical lines during quiet phases.
                active = ", ".join(
                    f"{r.rank}.{r.symbol} qty={r.quote_qty} sc={r.score}"
                    for r in self.scanner.top_n() if r.quoteable
                )
                if active and active != self._scanner_active_line:
                    self._scanner_active_line = active
                    log.status(f"[scanner] active: {active}")
            except Exception as exc:
                log.warn(f"[scanner] refresh failed: {exc!r}")

    def _make_scanner_builder(self) -> RowBuilder:
        """Build a ScannerRow for each instrument from live engine state."""
        data = self.engines.get("data")
        risk = self.engines.get("risk")
        cost = self.engines.get("cost")
        signal_map = self._latest_signal

        def build(inst) -> Optional[schema.ScannerRow]:
            from app.config import settings

            # Segment session gate: once a segment's session is closed or has
            # entered its wind-down window (NSE at 15:15, MCX 15 min before its
            # night close) it drops out of the scan entirely so the ranked
            # surface focuses on the remaining open segment(s). After NSE
            # closes at 15:30 the scanner is MCX-only.
            from app.infra import market_hours
            if not market_hours.is_open(inst.segment) or market_hours.in_winddown(inst.segment):
                return None

            snap = data.snapshot(inst.symbol) if data is not None else None
            if snap is None:
                return None
            margin_avail = risk.margin_available(inst.symbol) if risk is not None else 0.0
            margin_per_lot = risk.margin_per_lot(inst.symbol) if risk is not None else inst.margin_per_lot_rs
            # If the broker hasn't reported a real margin yet (and the risk
            # engine has no stale mid to estimate from), estimate from the live
            # tick now so the "Mgn ₹" column is never blank for a priced symbol.
            if margin_per_lot <= 0 and snap is not None and snap.mid:
                margin_per_lot = sizing_mod._estimate_margin_per_lot(snap.mid, inst.lot_size)
            inventory = risk.position(inst.symbol) if risk is not None else 0

            def cost_calc(mid: float, qty: int) -> Optional[schema.ScanCost]:
                if cost is None or mid is None or qty <= 0:
                    return None
                product = settings.product_type
                exchange = _exchange_of(inst.symbol)
                breakeven = cost.breakeven_spread_ticks(
                    mid, qty, product, inst.tick_size, inst.segment.value,
                    inst.lot_size, settings.brokerage_per_order, exchange,
                )
                required = max(breakeven + settings.min_profit_margin_ticks, 1)
                # Feasibility, not touch-profit: a thin per-tick-value contract
                # (most NSE equity/index futures) can never clear round-trip
                # charges on its 1-2-tick touch even though it IS profitably
                # quotable once the spread is widened. Work out the net the same
                # way on_signal accepts a quote — at the required-widened pair —
                # and only rule the symbol out when it cannot widen enough
                # (required spread beyond max_spread_widen_ticks) to clear the
                # charges.
                if required <= settings.max_spread_widen_ticks:
                    half = (required * inst.tick_size) / 2.0
                    bid = mid - half
                    ask = mid + half
                    net = cost.round_trip_net_profit(
                        bid, ask, qty, 1, product, inst.segment.value,
                        inst.lot_size, settings.brokerage_per_order, exchange,
                    )
                else:
                    net = 0.0
                charges = cost.round_trip_cost(
                    mid, qty, product, inst.segment.value, inst.lot_size,
                    settings.brokerage_per_order, exchange,
                )
                return schema.ScanCost(
                    net_profit_rs=round(net, 2),
                    round_trip_charges_rs=round(charges, 2),
                    breakeven_spread_ticks=breakeven,
                    required_spread_ticks=required,
                    profitable=net >= settings.min_net_profit_per_cycle_rs,
                )

            return compute_row(
                symbol=inst.symbol,
                inst=inst,
                snap=snap,
                signal=signal_map.get(inst.symbol),
                margin_avail=margin_avail,
                margin_per_lot=margin_per_lot,
                max_position_qty=settings.max_position_qty,
                margin_risk_fraction=settings.margin_risk_fraction,
                vol_reduction_per_tick=settings.vol_size_reduction_per_tick,
                min_liquidity=settings.scanner_min_liquidity,
                inventory=inventory,
                cost_calc=cost_calc,
            )
        return build

    def _apply_scanner_weights(self, rows: List[schema.ScannerRow]):
        """RoM weighting hook bound to the scanner refresh: realized-first
        cycle-profit-per-margin, EWMA-smoothed, volume-blended (see
        scanner.assign_rom_weights). Reads realized PnL / completed cycles from
        each live strategy and the booked margin from the risk ledger."""
        from app.config import settings

        risk = self.engines.get("risk")

        def realized_of(symbol: str):
            s = self._by_symbol.get(symbol)
            if s is None:
                return (0.0, 0)
            return (s.realized_pnl_rs, s.cycles_completed)

        def booked_of(symbol: str) -> float:
            return risk.booked_margin_for(symbol) if risk is not None else 0.0

        assign_rom_weights(
            rows, self._weight_tracker, realized_of, booked_of,
            min_cycles=settings.weight_min_cycles,
            volume_floor=settings.weight_volume_floor,
            weight_max=settings.weight_max,
            size_min=settings.size_weight_min,
            size_max=settings.size_weight_max,
        )

    async def _on_tick(self, tick: schema.MarketTick):
        from app.config import settings
        decision_interval = settings.decision_interval_sec
        last = self._last_decision_ts.get(tick.symbol, 0.0)
        if time.time() - last < decision_interval:
            return
        self._last_decision_ts[tick.symbol] = time.time()

        strategy = self._by_symbol.get(tick.symbol)
        if strategy is None:
            if tick.symbol not in self._no_strategy_warned:
                self._no_strategy_warned.add(tick.symbol)
                log.warn(f"[strategy] no strategy for {tick.symbol}")
            return
        t0 = time.perf_counter()

        data = self.engines.get("data")
        if data is None:
            return
        snapshot = data.snapshot(tick.symbol)
        signal = self._latest_signal.get(tick.symbol)
        if snapshot is None or snapshot.mid is None:
            if tick.symbol not in self._no_mid_warned:
                self._no_mid_warned.add(tick.symbol)
                log.warn(f"[strategy] no snapshot/mid for {tick.symbol}")
            return
        # Ranked-surface guard: a symbol outside the scanner's quoting ranks
        # can only ever resolve to HOLD_RANK, so short-circuit it here BEFORE
        # the (expensive) cost quote + full decision loop. A whole-market scan
        # holds ~250 symbols; paying the full cost-quote + on_signal for every
        # inactive name each decision interval is what saturated the market
        # handler and dropped ticks (stale rows -> everything HOLD_RANK).
        scanner = self.engines.get("scanner")
        if scanner is not None and not scanner.is_quoteable(tick.symbol):
            execu = self.engines.get("execution")
            if execu is not None and (strategy.active_buy_id or strategy.active_sell_id):
                await strategy._cancel_pair(execu)
                log.warn(f"[mm:{tick.symbol}] dropped from scanner rank -> cancelling quotes")
            return
        cost = self.engines.get("cost")
        if cost is None:
            return
        cost_quote = await cost.quote_for(
            symbol=tick.symbol, strategy=strategy.name, qty=strategy.qty,
            segment=self._segment(strategy), lot_size=self._lot_size(strategy),
            tick_size=self._tick_size(strategy), price=snapshot.mid,
            exchange=_exchange_of(tick.symbol),
        )
        sig = signal
        if sig is None or (sig.churn_ticks_per_sec <= 0 and snapshot.churn_ticks_per_sec > 0):
            # Boot warm-up: the signal engine computes live churn from its own
            # tick stream, so until its first rolling window fills, derive the
            # widening from the data snapshot's churn instead of quoting at a
            # vol-blind spread.
            churn = max((sig.churn_ticks_per_sec if sig else 0.0), snapshot.churn_ticks_per_sec or 0.0)
            widening = int(churn * settings.volatility_widen_factor) if churn > 0 else 0
            # ML adverse-selection widening on top of churn-based widening
            if snapshot.ml_should_widen and snapshot.ml_extra_ticks > 0:
                widening = widening + snapshot.ml_extra_ticks
            sig = schema.SignalMetrics(
                symbol=tick.symbol,
                strategy=sig.strategy if sig is not None else strategy.name,
                ts=sig.ts if sig is not None else tick.ts,
                mid=sig.mid if sig is not None else snapshot.mid,
                churn_ticks_per_sec=max(churn, sig.churn_ticks_per_sec if sig is not None else 0.0),
                vol_widening_ticks=min(
                    max(sig.vol_widening_ticks if sig is not None else 0, widening),
                    settings.max_spread_widen_ticks,
                ),
                liquidity_grade=sig.liquidity_grade if sig is not None else 0.5,
                spread_ticks_now=sig.spread_ticks_now if sig is not None else 0,
                quoteable=sig.quoteable if sig is not None else True,
                reasons=sig.reasons if sig is not None else [],
            )

        decision = await strategy.on_signal(snapshot, sig, cost_quote, self.engines)

        latency_ms = (time.perf_counter() - t0) * 1000
        strategy.decisions_total += 1
        strategy.record_latency(latency_ms)
        strategy.__dict__["last_decision"] = f"{decision} ({latency_ms:.1f}ms)"
        if decision not in ("HOLD",):
            ml_tag = ""
            if snapshot.ml_should_widen:
                ml_tag = f" ml_p={snapshot.ml_adverse_prob:.2f}"
            log.status(
                f"[strategy:{tick.symbol}] {decision} "
                f"mid={snapshot.mid:.2f} qty={strategy.qty} ({latency_ms:.1f}ms){ml_tag}"
            )
        self._latest_signal[tick.symbol] = sig
        if decision in ("EXIT_SELL", "EXIT_BUY"):
            risk = self.engines.get("risk")
            if risk is not None and risk.position(tick.symbol) == 0:
                strategy.on_cycle_closed(risk.realized_pnl_for(tick.symbol))

    def _strategy_for_symbol(self, symbol: str) -> Optional[str]:
        strat = self._by_symbol.get(symbol)
        return strat.name if strat is not None else None

    def _segment(self, strategy: BaseStrategy) -> str:
        inst = getattr(strategy, "instrument", None)
        if inst is not None:
            return inst.segment.value
        return strategy.segment

    def _lot_size(self, strategy: BaseStrategy) -> int:
        inst = getattr(strategy, "instrument", None)
        if inst is not None:
            return inst.lot_size
        from app.config import settings
        return settings.resolved_lot_size

    def _tick_size(self, strategy: BaseStrategy) -> float:
        inst = getattr(strategy, "instrument", None)
        if inst is not None:
            return inst.tick_size
        from app.config import settings
        return settings.resolved_tick_size

    async def _publish_metrics(self):
        for name, s in self.strategies.items():
            state = (
                s.decisions_total, s.quotes_placed, s.quotes_cancelled,
                s.fills_received, s.cycles_completed, s.realized_pnl_rs,
                s.avg_latency_ms, s.__dict__.get("last_decision", "-"),
            )
            if self._last_metrics.get(name) == state:
                continue
            self._last_metrics[name] = state
            m = self._build_metrics(name, s)
            if m is None:
                continue
            try:
                await self.obs.publish_decision(m)
                await self.db.insert_decision(m)
            except Exception:
                pass