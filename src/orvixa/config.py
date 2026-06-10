"""Application configuration.

A single ``Settings`` object, populated from environment variables and an
optional ``.env`` file (via pydantic-settings). Comma-separated symbol lists
are parsed into ``list[str]`` and upper-cased.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_VALID_FEEDS = ("sim", "binance")


def _split_csv_raw(value: object) -> object:
    """Turn ``"a, b"`` into ``["a", "b"]`` without case-folding (e.g. URLs)."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _split_csv(value: object) -> object:
    """Turn ``"BTCUSDT, ethusdt"`` into ``["BTCUSDT", "ETHUSDT"]``.

    Leaves already-parsed lists untouched so the validator is idempotent.
    """
    if isinstance(value, str):
        return [item.strip().upper() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- feed selection -------------------------------------------------
    feed: str = "sim"  # "sim" | "binance"

    # --- binance endpoints (public market data; no key needed) ----------
    binance_ws_base: str = "wss://stream.binance.com:9443"
    binance_rest_base: str = "https://api.binance.com"

    # --- symbol universe (Tier 0 + M1 seed set; dynamic from M3) ---------
    # NoDecode: these arrive as comma-separated strings (e.g. "BTCUSDT,ETHUSDT"),
    # not JSON, so we bypass pydantic-settings' default JSON decoding for list
    # fields and parse them ourselves in `_parse_symbol_lists` below.
    core_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    )
    seed_symbols: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- candle / reconnect ---------------------------------------------
    kline_interval: str = "1m"
    backfill_limit: int = 5  # candles fetched on first connect when no history

    # --- symbol manager (M3) ---------------------------------------------
    # Curated meme set — always Tier 1 regardless of volume ranking.
    meme_symbols: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI"]
    )
    # How often the symbol manager re-discovers/re-ranks the universe.
    symbol_refresh_interval_seconds: float = 300.0
    # Top-N symbols by volume rank (excluding Tier 0/meme) get Tier 1.
    tier1_size: int = 15
    # Rolling window (snapshots) for the breadth engine's trend/high/low calc.
    breadth_trend_window: int = 20
    # A Tier-2 symbol whose 24h quote volume or trade count grows by this
    # multiple, or whose 24h change exceeds `promotion_volatility_pct`, is
    # promoted to Tier 1 ("spike" tag).
    promotion_volume_multiplier: float = 3.0
    promotion_volatility_pct: float = 8.0
    # Consecutive calm refresh cycles before a spike-promoted symbol is
    # demoted back to Tier 2.
    demotion_grace_cycles: int = 3

    # --- analytics engine (M4) -------------------------------------------
    # EMA periods (fast/slow) used for trend direction/strength/slope.
    ema_fast_period: int = 9
    ema_slow_period: int = 21
    # Wilder RSI / ATR lookback periods.
    rsi_period: int = 14
    atr_period: int = 14
    # Rolling window (closes) for realized volatility (stdev of log returns, %).
    realized_vol_window: int = 20
    # Rolling window (candles) for relative volume (current vs. average).
    relative_volume_window: int = 20
    # Rolling window (candles, excluding current) for breakout/breakdown
    # high/low levels.
    breakout_window: int = 20
    # Pump/dump: % price change over this many candles triggers an event.
    pump_dump_window: int = 5
    pump_dump_pct: float = 5.0
    # Volatility-spike: realized vol vs. its rolling baseline average,
    # multiplied by this factor.
    vol_spike_window: int = 20
    vol_spike_multiplier: float = 2.0
    # HIGH VOLATILITY signal threshold (realized vol, %).
    high_volatility_pct: float = 3.0
    # Minimum confidence (0-100) for a signal to be persisted.
    signal_min_confidence: int = 60
    # How often the regime/health engines recompute from breadth + trends.
    regime_refresh_interval_seconds: float = 60.0
    # indicator batch writer: flush whichever comes first
    indicator_batch_max_size: int = 200
    indicator_batch_interval_seconds: float = 2.0

    # --- logging --------------------------------------------------------
    log_level: str = "INFO"

    # --- web/API (Phase 2) ----------------------------------------------
    # Deployment mode. "production" (the default) enforces a real API_KEY and
    # forbids wildcard CORS. Set to "development" for local work only.
    app_env: str = "production"
    # Shared API key required in the X-API-Key header. Must be set to a
    # non-default value in production; see Settings.check_security below.
    api_key: str = ""
    # Comma-separated list of allowed CORS origins for the API.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:8080"]
    )
    # Max signal rows returned by GET /signals/{symbol}.
    api_signals_limit: int = 50
    # Daemon supervisor restart/poll interval (seconds) for the looped runners.
    daemon_interval_seconds: float = 5.0

    _split_cors_origins = field_validator("cors_origins", mode="before")(_split_csv_raw)

    @field_validator("cors_origins", mode="after")
    @classmethod
    def _validate_cors_origins(cls, value: list[str], info) -> list[str]:
        app_env = (info.data.get("app_env") or "production").lower()
        if app_env == "production" and "*" in value:
            raise RuntimeError(
                "SECURITY ERROR: wildcard CORS origin '*' is not allowed in production"
            )
        return value

    @field_validator("api_key", mode="after")
    @classmethod
    def _validate_api_key(cls, value: str, info) -> str:
        app_env = (info.data.get("app_env") or "production").lower()
        if app_env == "production" and (not value or value == "orvixa-dev-key"):
            raise RuntimeError(
                "SECURITY ERROR: default or missing API_KEY is not allowed in production"
            )
        return value

    # --- persistence (M2) -------------------------------------------------
    postgres_dsn: str = "postgresql://orvixa:orvixa@postgres:5432/orvixa"
    redis_url: str = "redis://redis:6379/0"

    # asyncpg pool sizing
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5

    # candle batch writer: flush whichever comes first
    candle_batch_max_size: int = 200
    candle_batch_interval_seconds: float = 2.0

    # --- simulator tuning (dev) -----------------------------------------
    sim_candle_seconds: float = 3.0

    @field_validator("core_symbols", "seed_symbols", "meme_symbols", mode="before")
    @classmethod
    def _parse_symbol_lists(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("feed", mode="before")
    @classmethod
    def _validate_feed(cls, value: object) -> str:
        text = str(value).lower()
        if text not in _VALID_FEEDS:
            raise ValueError(f"feed must be one of {_VALID_FEEDS}, got {value!r}")
        return text

    @property
    def all_symbols(self) -> list[str]:
        """Core + seed symbols, de-duplicated, order preserved."""
        ordered: dict[str, None] = {}
        for symbol in (*self.core_symbols, *self.seed_symbols):
            ordered[symbol] = None
        return list(ordered)


def get_settings() -> Settings:
    """Build a fresh ``Settings`` from the current environment / .env."""
    return Settings()
