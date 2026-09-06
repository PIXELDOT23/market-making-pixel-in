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
    quote_qty: int = field(default_factory=lambda: int(os.getenv("QUOTE_QTY", "1")))

    # ---- Redis (cache + inter-engine bus + auth token store) ----
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )

    # ---- PostgreSQL (persistence) ----
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/market_making",
        )
    )

    # ---- Engine tuning / low latency ----
    engine_heartbeat_sec: float = field(
        default_factory=lambda: float(os.getenv("ENGINE_HEARTBEAT_SEC", "1.0"))
    )
    db_flush_batch: int = field(
        default_factory=lambda: int(os.getenv("DB_FLUSH_BATCH", "200"))
    )
    db_flush_interval_sec: float = field(
        default_factory=lambda: float(os.getenv("DB_FLUSH_INTERVAL_SEC", "1.0"))
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

    # ---- Risk ----
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
        default_factory=lambda: float(os.getenv("MIN_FREE_MARGIN_BUFFER_RS", "1000.0"))
    )
    max_position_age_sec: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_AGE_SEC", "300.0"))
    )
    max_loss_per_position_rs: float = field(
        default_factory=lambda: float(os.getenv("MAX_LOSS_PER_POSITION_RS", "1500.0"))
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
    volatility_halt_quoting_tps: float = field(
        default_factory=lambda: float(os.getenv("VOLATILITY_HALT_QUOTING_TICKSPERSEC", "1.00"))
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
    close_flatten_seconds: int = field(
        default_factory=lambda: int(os.getenv("CLOSE_FLATTEN_SECONDS", "120"))
    )

    @property
    def session_weekdays_set(self):
        return {int(x) for x in self.session_weekdays.split(",") if x.strip() != ""}

    # ---- API / frontend ----
    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(os.getenv("API_PORT", "8000")))

    @property
    def resolved_lot_size(self) -> int:
        return self.lot_size or (1250 if self.segment == "COMMODITY" else 1)

    @property
    def resolved_tick_size(self) -> float:
        return self.tick_size or (0.10 if self.segment == "COMMODITY" else 0.05)

    @property
    def tick_value_rs(self) -> float:
        return self.resolved_lot_size * self.resolved_tick_size


def load_settings() -> Settings:
    return Settings()


settings = load_settings()