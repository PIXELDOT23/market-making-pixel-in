"""
app/config.py
-------------
Central, environment-driven configuration for the multi-engine platform.

Every engine reads from the same Settings object so credentials, redis,
postgres, and trading parameters stay consistent across the whole project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


@dataclass(frozen=True)
class Settings:
    # ---- FYERS App Credentials ----
    # Never shipped as defaults — must come from the environment (or a .env
    # file). A run that needs to hit the broker without them fails fast at the
    # auth layer with a clear message instead of silently using a bogus id.
    client_id: str = field(
        default_factory=lambda: os.getenv("FYERS_CLIENT_ID", "0B23JHNNLH-200")
    )
    secret_key: str = field(
        default_factory=lambda: os.getenv("FYERS_SECRET_KEY", "vprdMcFWr1ZHiQpc")
    )
    redirect_uri: str = field(
        default_factory=lambda: os.getenv(
            "FYERS_REDIRECT_URI", "http://localhost:2000/callback"
        )
    )

    # ---- Segment & Universe ----
    segment: str = field(default_factory=lambda: os.getenv("SEGMENT", "COMMODITY"))
    # Asset type: COMMODITY (futures, margin-capped lots), EQUITY (cash, shares),
    # EQUITY_FUT / FUT (equity futures, lots). Used for position sizing policy.
    asset_type: str = field(
        default_factory=lambda: os.getenv("ASSET_TYPE", "COMMODITY")
    )
    symbol: str = field(
        default_factory=lambda: os.getenv("SYMBOL", "MCX:NATURALGAS26SEPFUT")
    )
    product_type: str = field(
        default_factory=lambda: os.getenv("PRODUCT_TYPE", "INTRADAY")
    )
    lot_size: int = field(default_factory=lambda: int(os.getenv("LOT_SIZE", "0")))
    tick_size: float = field(
        default_factory=lambda: float(os.getenv("TICK_SIZE", "0"))
    )
    # Broker margin required for ONE lot of the configured symbol. If 0, the
    # risk engine learns it from the first FYERS margin API response.
    margin_per_lot_rs: float = field(
        default_factory=lambda: float(os.getenv("MARGIN_PER_LOT_RS", "0"))
    )

    # ---- Position sizing (equity / equity-futures) ----
    # Fraction of free margin used to derive the dynamic quote size. Commodity
    # futures ignore this and always quote the configured quote_qty (1 lot).
    margin_risk_fraction: float = field(
        default_factory=lambda: float(os.getenv("MARGIN_RISK_FRACTION", "0.25"))
    )
    quote_qty: int = field(default_factory=lambda: int(os.getenv("QUOTE_QTY", "1")))

    # ---- Scanner / universe ----
    # Comma-separated candidate instruments: symbol:segment:lot_size:tick_size.
    # If empty, scan/quote only the configured SYMBOL.
    universe: str = field(default_factory=lambda: os.getenv("UNIVERSE", ""))
    # How many of the top-ranked assets may actually rest quotes (rank 1..N).
    max_scanner_active: int = field(
        default_factory=lambda: int(os.getenv("MAX_SCANNER_ACTIVE", "10"))
    )
    # Reset scanner ranking every N seconds (avoid sys-lag from penny ticks).
    scanner_refresh_sec: float = field(
        default_factory=lambda: float(os.getenv("SCANNER_REFRESH_SEC", "1.0"))
    )
    # Min seconds between two decisions on the same symbol (per-strategy throttle).
    decision_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("DECISION_INTERVAL_SEC", "2.0"))
    )
    # Minimum liquidity (0..1 depth ratio) for an asset to be quote-worthy.
    scanner_min_liquidity: float = field(
        default_factory=lambda: float(os.getenv("SCANNER_MIN_LIQUIDITY", "0.05"))
    )
    # Volatility scaling for dynamic size: 1 share per extra vol tick is
    # reduced by this fraction so the book stays flat in wild moves.
    vol_size_reduction_per_tick: float = field(
        default_factory=lambda: float(os.getenv("VOL_SIZE_REDUCTION_PER_TICK", "0.10"))
    )

    # ---- Whole-market scan (no explicit UNIVERSE) ----
    # Take over the ENTIRE market via the FYERS symbol masters: every live
    # NFO equity/index future (NSE_FO "XX" contracts) + every live MCX
    # commodity future is scanned + ranked.
    nse_all_equity_scan: bool = field(
        default_factory=lambda: os.getenv("NSE_ALL_EQUITY_SCAN", "1").lower() in ("1", "true", "yes")
    )
    mcx_all_futures_scan: bool = field(
        default_factory=lambda: os.getenv("MCX_ALL_FUTURES_SCAN", "1").lower() in ("1", "true", "yes")
    )
    # Only trade the NEAREST expiring contract per underlying family; when it
    # expires the next boot rolls over to the new near contract automatically.
    mcx_near_contract_only: bool = field(
        default_factory=lambda: os.getenv("MCX_NEAR_CONTRACT_ONLY", "1").lower() in ("1", "true", "yes")
    )
    nfo_near_contract_only: bool = field(
        default_factory=lambda: os.getenv("NFO_NEAR_CONTRACT_ONLY", "1").lower() in ("1", "true", "yes")
    )
    # Master-contract download timeout (first boot / stale cache).
    master_fetch_timeout_sec: float = field(
        default_factory=lambda: float(os.getenv("MASTER_FETCH_TIMEOUT_SEC", "20.0"))
    )
    # Penny-stock protection: NFO equity futures whose underlying's last close
    # is below this price are skimmed out of the whole-market universe. Their
    # per-lot SPAN margin is disproportionate (50%+ of notional) and they are
    # not market-making targets. 0 disables the filter.
    nfo_min_underlying_price_rs: float = field(
        default_factory=lambda: float(os.getenv("NFO_MIN_UNDERLYING_PRICE_RS", "100"))
    )
    # Broker margin refresh (POST /multiorder/margin, one symbol per call since
    # the endpoint returns only basket totals). Every symbol is re-queried at
    # most once per ``margin_refresh_sec``, and at most ``margin_refresh_batch``
    # per scan cycle so the loop never blocks on the broker.
    margin_refresh_sec: float = field(
        default_factory=lambda: float(os.getenv("MARGIN_REFRESH_SEC", "30"))
    )
    margin_refresh_batch: int = field(
        default_factory=lambda: int(os.getenv("MARGIN_REFRESH_BATCH", "3"))
    )
    # Broker REST timeout for the margin calculator / positions calls. A wedged
    # broker socket is degraded like a -429 rate-limit (back off the symbol)
    # instead of hanging an executor thread forever.
    margin_api_timeout_sec: float = field(
        default_factory=lambda: float(os.getenv("MARGIN_API_TIMEOUT_SEC", "12.0"))
    )
    positions_api_timeout_sec: float = field(
        default_factory=lambda: float(os.getenv("POSITIONS_API_TIMEOUT_SEC", "10.0"))
    )
    # Power cut / hard-kill recovery: a sudden power loss never runs the Ctrl+C
    # emergency-flatten, so any net positions left on the broker by a previous
    # run are squared off before the new book starts quoting.
    flatten_orphans_at_boot: bool = field(
        default_factory=lambda: os.getenv("FLATTEN_ORPHANS_AT_BOOT", "1").lower()
        in ("1", "true", "yes")
    )

    # ---- Redis (cache + inter-engine bus + auth token store) ----
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    # ---- PostgreSQL (persistence, OPTIONAL) ----
    # A reachable PostgreSQL is not required: when DATABASE_URL is unset/empty
    # (the default) or the server is down, the Database becomes a no-op and all
    # engines keep running without persistence. Do not point this at a server
    # that is not listening or the periodic flush will just retry it forever.
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "")
    )

    # ---- Engine tuning / low latency ----
    engine_heartbeat_sec: float = field(
        default_factory=lambda: float(os.getenv("ENGINE_HEARTBEAT_SEC", "1.0"))
    )
    # Bounded FIFO per bus subscription. The pump only enqueues and a slow
    # consumer drops-newest (throttled warn) instead of stalling the feed.
    bus_handler_queue_size: int = field(
        default_factory=lambda: int(os.getenv("BUS_HANDLER_QUEUE", "1024"))
    )
    # Broker /positions REST poll cadence. The order WebSocket already drives
    # inventory in real time; this is only a manual-trade / broker-reconcile
    # safety net, so once a second is pure waste.
    positions_poll_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("POSITIONS_POLL_INTERVAL_SEC", "15.0"))
    )
    db_flush_batch: int = field(
        default_factory=lambda: int(os.getenv("DB_FLUSH_BATCH", "200"))
    )
    db_flush_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("DB_FLUSH_INTERVAL_SEC", "1.0"))
    )
    # Hard cap on rows buffered for Postgres. If the DB stays unreachable the
    # flush buffer is dropped (with a warning) instead of growing until OOM.
    db_pending_cap: int = field(
        default_factory=lambda: int(os.getenv("DB_PENDING_CAP", "200000"))
    )
    data_feed_buffer: int = field(
        default_factory=lambda: int(os.getenv("DATA_FEED_BUFFER", "10000"))
    )

    # ---- Quoting / cost protection ----
    spread_ticks: int = field(default_factory=lambda: int(os.getenv("SPREAD_TICKS", "4")))
    auto_widen_spread: bool = field(
        default_factory=lambda: os.getenv("AUTO_WIDEN_SPREAD", "true").lower() in ("1", "true", "yes")
    )
    min_profit_margin_ticks: int = field(
        default_factory=lambda: int(os.getenv("MIN_PROFIT_MARGIN_TICKS", "2"))
    )
    min_net_profit_per_cycle_rs: float = field(
        default_factory=lambda: float(os.getenv("MIN_NET_PROFIT_PER_CYCLE_RS", "25.0"))
    )
    brokerage_per_order: float = field(
        default_factory=lambda: float(os.getenv("BROKERAGE_PER_ORDER", "20.0"))
    )
    requote_tolerance_ticks: int = field(
        default_factory=lambda: int(os.getenv("REQUOTE_TOLERANCE_TICKS", "2"))
    )
    # Order-book-imbalance guard: when one side of the book is this many times
    # heavier than the other, refuse to quote the "hot" side (quoting into a
    # 3x-lopsided stampede is adverse-selection risk). 0 disables the gate.
    obi_gate_ratio: float = field(
        default_factory=lambda: float(os.getenv("OBI_GATE_RATIO", "3.0"))
    )
    obi_gate_enabled: bool = field(
        default_factory=lambda: os.getenv("OBI_GATE", "true").lower() in ("1", "true", "yes")
    )
    # Seconds a symbol stands down after its order is hard-rejected by the
    # broker (reject circuit-breaker). Prevents the whole-market scanner from
    # flooding the broker + margin API with the same doomed order every tick.
    reject_cooldown_sec: float = field(
        default_factory=lambda: float(os.getenv("REJECT_COOLDOWN_SEC", "30.0"))
    )

    # ---- RoM-weighted capital allocation ----
    # Allocate quoting weight by cycle-profit-per-margin (RoM) so scarce margin
    # flows to the markets that earn the most per rupee committed. The signal is
    # REALIZED first: once a symbol has completed at least ``weight_min_cycles``
    # cycles its weight uses (realized pnl / booked margin); below that it falls
    # back to the theoretical touch RoM (net_profit_rs / margin_req_rs). Values
    # are EWMA-smoothed (``weight_smoothing_alpha``), blended with a normalized
    # log-volume factor (``weight_volume_floor`` = weight of a zero-volume row)
    # so dead books can't win on a wide spread alone, and capped (``weight_max``).
    weight_enabled: bool = field(
        default_factory=lambda: os.getenv("WEIGHT_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    weight_min_cycles: int = field(
        default_factory=lambda: int(os.getenv("WEIGHT_MIN_CYCLES", "3"))
    )
    weight_smoothing_alpha: float = field(
        default_factory=lambda: float(os.getenv("WEIGHT_SMOOTHING_ALPHA", "0.15"))
    )
    weight_volume_floor: float = field(
        default_factory=lambda: float(os.getenv("WEIGHT_VOLUME_FLOOR", "0.30"))
    )
    weight_max: float = field(
        default_factory=lambda: float(os.getenv("WEIGHT_MAX", "5.0"))
    )
    # Per-segment quoting slots become proportional to the segment's summed
    # weight (winning segment gets more of the shared active_limit) instead of
    # each segment getting an equal share. 0 restores the equal-slot behaviour.
    slot_weight_enabled: bool = field(
        default_factory=lambda: os.getenv("SLOT_WEIGHT_ENABLED", "true").lower()
        in ("1", "true", "yes", "on")
    )
    # Equity/equity-futures size multiplier range produced by the normalized
    # per-symbol weight: worst name sizes at ``size_weight_min`` x margin
    # fraction, best at ``size_weight_max`` x. Commodity sizing ignores weight
    # (stays margin-capped only).
    size_weight_min: float = field(
        default_factory=lambda: float(os.getenv("SIZE_WEIGHT_MIN", "0.5"))
    )
    size_weight_max: float = field(
        default_factory=lambda: float(os.getenv("SIZE_WEIGHT_MAX", "2.0"))
    )

    # ---- Risk ----
    # Daily-loss / engine halt auto-clears after this many seconds so a
    # morning loss-halt doesn't leave the bot dead all day; the position stays
    # flat and the market-hours + reject-circuit gates still apply after
    # recovery. 0 disables auto-recovery (manual RESET only).
    halt_cooldown_sec: float = field(
        default_factory=lambda: float(os.getenv("HALT_COOLDOWN_SEC", "900.0"))
    )
    # Position cap per symbol AND per fill: qty is deliberately held at 1 lot
    # for every FUT contract (both NFO equity and MCX). Capital is NOT capacity:
    # deploying the margin across MORE instruments (breadth) is worth more than
    # stacking it onto fewer ones (depth), because per-name edge decays fast as
    # you size up while the account's 95% budget still wants covering. The
    # scanner's greedy budget then walks the ranked list as far as the
    # 95%-utilization it fits, reserving 1 lot per name.
    max_position_qty: int = field(default_factory=lambda: int(os.getenv("MAX_POSITION_QTY", "1")))
    max_daily_loss_rs: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_RS", "2500.0"))
    )
    max_orders_per_minute: int = field(
        default_factory=lambda: int(os.getenv("MAX_ORDERS_PER_MINUTE", "15"))
    )
    check_margin_before_order: bool = field(
        default_factory=lambda: os.getenv("CHECK_MARGIN_BEFORE_ORDER", "true").lower()
        in ("1", "true", "yes")
    )
    min_free_margin_buffer_rs: float = field(
        default_factory=lambda: float(os.getenv("MIN_FREE_MARGIN_BUFFER_RS", "500.0"))
    )
    max_position_age_sec: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_AGE_SEC", "300.0"))
    )
    max_loss_per_position_rs: float = field(
        default_factory=lambda: float(os.getenv("MAX_LOSS_PER_POSITION_RS", "1500.0"))
    )

    # ---- Global multi-asset margin budget (aggregation) ----
    # The scanner's greedy margin budget treats every selected name's quote as
    # reserving margin. ``margin_reserve_both_sides`` accounts for a standing
    # two-sided market-making pair being able to fill BOTH its legs (one order
    # per side), so each name reserves 2 x its per-lot margin. Together with
    # ``min_free_margin_buffer_rs`` the budget never corners the account, and
    # ``margin_remaining()`` feeds the same aggregated figure into per-symbol
    # sizing so the quiet scanner reservation and the live quote agree.
    margin_ledger_enabled: bool = field(
        default_factory=lambda: os.getenv("MARGIN_LEDGER", "true").lower()
        in ("1", "true", "yes", "on")
    )
    # A standing two-sided make pair DOES reserve margin for both legs, but
    # futures margin is netted per position by the broker (equal long+short
    # release it), so 2x overhead on every name pins the book at ~1 lot each
    # and leaves most of the account idle. Default is a NET-position view; the
    # per-order broker margin API remains the hard backstop.
    margin_reserve_both_sides: bool = field(
        default_factory=lambda: os.getenv("MARGIN_RESERVE_BOTH_SIDES", "false").lower()
        in ("1", "true", "yes", "on")
    )
    # Fraction of available margin the book is allowed to deploy. The greedy
    # scanner budget and per-symbol sizing are both capped at
    # ``margin_utilization_target`` x available, so e.g. 0.95 keeps 5% free for
    # slippage/margin spikes while pushing the live book to ~95% utilisation.
    margin_utilization_target: float = field(
        default_factory=lambda: float(os.getenv("MARGIN_UTILIZATION_TARGET", "0.95"))
    )

    # ---- Volatility-aware quoting ----
    volatility_window_sec: float = field(
        default_factory=lambda: float(os.getenv("VOLATILITY_WINDOW_SEC", "30.0"))
    )
    volatility_widen_factor: float = field(
        default_factory=lambda: float(os.getenv("VOLATILITY_WIDEN_FACTOR", "2.0"))
    )
    max_spread_widen_ticks: int = field(
        default_factory=lambda: int(os.getenv("MAX_SPREAD_WIDEN_TICKS", "10"))
    )
    # When live churn (ticks/sec) exceeds this the symbol stops quoting until
    # it calms down — adverse-selection protection. 0 disables the gate.
    volatility_halt_quoting_tps: float = field(
        default_factory=lambda: float(os.getenv("VOLATILITY_HALT_QUOTING_TICKSPERSEC", "0.0"))
    )

    # ---- Inventory-skew quoting ----
    # Instead of cancelling the "adding" side wholesale when a position is
    # open, shift the whole quote pair toward flattening: when long the pair
    # skews down (bids less likely to add, asks more likely to flatten) and
    # short it skews up — the book keeps earning while de-risking. The
    # increasing side is still dropped at the hard inventory cap.
    inventory_skew_quoting: bool = field(
        default_factory=lambda: os.getenv("INVENTORY_SKEW_QUOTING", "true").lower()
        in ("1", "true", "yes")
    )
    # Skew magnitude per lot of open inventory, in ticks (clamped to the spread cap).
    inventory_skew_ticks_per_lot: int = field(
        default_factory=lambda: int(os.getenv("INVENTORY_SKEW_TICKS_PER_LOT", "1"))
    )

    # ---- Trading hours ----
    trading_start: str = field(
        default_factory=lambda: os.getenv("TRADING_START", "09:05")
    )
    trading_end: str = field(
        default_factory=lambda: os.getenv("TRADING_END", "23:00")
    )

    # ---- Session model (exchange + asset aware) ----
    # MCX natural gas futures trade Monday-Friday 09:00 IST open; the close is
    # 23:30 IST while US DST is in effect, else 23:55 IST (per MCX circulars).
    session_open: str = field(
        default_factory=lambda: os.getenv("SESSION_OPEN", "09:00")
    )
    session_close: str = field(
        default_factory=lambda: os.getenv("SESSION_CLOSE", "23:30")
    )
    session_close_dst_disabled: str = field(
        default_factory=lambda: os.getenv("SESSION_CLOSE_DST_DISABLED", "23:55")
    )
    session_weekdays: str = field(
        default_factory=lambda: os.getenv("SESSION_WEEKDAYS", "0,1,2,3,4")
    )
    # NSE cash / equity-futures session (09:15-15:30 IST, Mon-Fri). MCX keeps
    # the legacy SESSION_* knobs above; NSE gets its own so a closed NSE session
    # never leaves equity quotes resting while MCX is still live at night.
    nse_session_open: str = field(
        default_factory=lambda: os.getenv("NSE_SESSION_OPEN", "09:15")
    )
    nse_session_close: str = field(
        default_factory=lambda: os.getenv("NSE_SESSION_CLOSE", "15:30")
    )
    close_flatten_seconds: int = field(
        default_factory=lambda: int(os.getenv("CLOSE_FLATTEN_SECONDS", "120"))
    )

    # ---- ML engine ----
    ml_enabled: bool = field(
        default_factory=lambda: os.getenv("ML_ENABLED", "0").lower() in ("1", "true", "yes")
    )
    # Data collection runs independently of inference so the order_book_depths /
    # ml_features tables grow from day one. Set ML_ENABLED=1 to also run the
    # model and widen the book; ML_CAPTURE_ENABLED can turn the capture off.
    ml_capture_enabled: bool = field(
        default_factory=lambda: os.getenv("ML_CAPTURE_ENABLED", "1").lower() in ("1", "true", "yes")
    )
    ml_model_name: str = field(
        default_factory=lambda: os.getenv("ML_MODEL_NAME", "adverse_selection_v1")
    )
    ml_model_path: str = field(
        default_factory=lambda: os.getenv("ML_MODEL_PATH", "models/adverse_selection_v1.onnx")
    )
    ml_widen_threshold: float = field(
        default_factory=lambda: float(os.getenv("ML_WIDEN_THRESHOLD", "0.65"))
    )
    ml_widen_extra_ticks: int = field(
        default_factory=lambda: int(os.getenv("ML_WIDEN_EXTRA_TICKS", "1"))
    )
    ml_depth_persist_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("ML_DEPTH_PERSIST_INTERVAL_SEC", "1.0"))
    )
    ml_feature_persist_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("ML_FEATURE_PERSIST_INTERVAL_SEC", "0.5"))
    )
    # How many seconds before an exchange session close quoting must stop and
    # every position for that segment must be squared off. NSE futures close at
    # 15:30 IST -> with this at 900 the whole NSE book stands down at 15:15.
    # MCX closes 23:30/23:55 IST -> wind-down at 23:15/23:40. 0 disables the
    # early wind-down (falls back to the CLOSE_FLATTEN_SECONDS hard flatten).
    close_winddown_seconds: int = field(
        default_factory=lambda: int(os.getenv("CLOSE_WINDDOWN_SECONDS", "900"))
    )

    @property
    def session_weekdays_set(self):
        return {int(x) for x in self.session_weekdays.split(",") if x.strip() != ""}

    # ---- API / frontend ----
    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(os.getenv("API_PORT", "8000")))

    # ---- instrument / asset resolution (lazy, uses the global registry) ----
    def active_instrument(self):
        from app.infra.instrument import build_instrument, instrument_registry
        inst = instrument_registry.get(self.symbol)
        if inst is not None:
            return inst
        seg = self.asset_type if self.asset_type else self.segment
        inst = build_instrument(
            symbol=self.symbol,
            segment=seg,
            lot_size=self.lot_size,
            tick_size=self.tick_size,
            margin_per_lot_rs=self.margin_per_lot_rs,
        )
        instrument_registry.register(inst)
        return inst

    @property
    def resolved_lot_size(self) -> int:
        return self.active_instrument().lot_size

    @property
    def resolved_tick_size(self) -> float:
        return self.active_instrument().tick_size

    @property
    def tick_value_rs(self) -> float:
        return self.active_instrument().tick_value_rs


def load_settings() -> Settings:
    return Settings()


settings = load_settings()