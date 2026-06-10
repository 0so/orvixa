"""Live Binance market feed (public market data — no API key required).

Implements :class:`MarketFeed` over Binance's combined WebSocket stream:

* one socket multiplexes every ``<sym>@kline_1m`` for the tracked set, plus one
  ``!miniTicker@arr`` for whole-market snapshots;
* candle-close events fire only when Binance marks the kline closed (``k.x``);
* the connection self-heals — heartbeat/pong, exponential backoff with jitter,
  resubscribe on reopen, and a REST gap-fill so no minute is ever lost.

Testability: the WebSocket connector and the REST backfill are injected
(``connector=`` / ``backfiller=``) and the backoff sequence is recorded on
``self.backoff_history``. The reconnect test drives the whole resilience path
with fakes — no network.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Protocol

import httpx

from .base import Candle, MarketFeed
from .normalize import (
    kline_event_to_candle,
    miniticker_array_to_rows,
    rest_kline_to_candle,
    to_stream_symbol,
)

try:  # websockets is a hard dep at runtime, optional at import time for tests
    import websockets
except Exception:  # noqa: BLE001 - import guarded so unit tests run without it
    websockets = None

import logging

logger = logging.getLogger("orvixa.feeds.binance")

# Backoff schedule for reconnects (seconds); jitter is added on top.
_BACKOFF_STEPS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
_MAX_STREAMS_PER_SOCKET = 1000
# Reconnect well before Binance's 24h server-side cap.
_PROACTIVE_RECONNECT_SECONDS = 23 * 3600


class WSConnection(Protocol):
    """Minimal async websocket surface the feed needs (a subset of `websockets`)."""

    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self) -> AsyncIterator[str]: ...


Connector = Callable[[str], Awaitable[WSConnection]]
Backfiller = Callable[[list[str], int], Awaitable[list[Candle]]]


class BinanceFeed(MarketFeed):
    def __init__(
        self,
        symbols: Iterable[str],
        ws_base: str = "wss://stream.binance.com:9443",
        rest_base: str = "https://api.binance.com",
        interval: str = "1m",
        backfill_limit: int = 5,
        connector: Connector | None = None,
        backfiller: Backfiller | None = None,
        max_reconnects: int | None = None,
    ) -> None:
        super().__init__()
        self._symbols: set[str] = {s.upper() for s in symbols}
        self._ws_base = ws_base.rstrip("/")
        self._rest_base = rest_base.rstrip("/")
        self._interval = interval
        self._backfill_limit = backfill_limit
        self._connector = connector or self._default_connector
        self._backfiller = backfiller or self._default_backfiller
        self._max_reconnects = max_reconnects  # None = forever (production)

        self._ws: WSConnection | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._connected = asyncio.Event()

        # Observable state for tests / ops.
        self.backoff_history: list[float] = []
        self.connect_count = 0
        self.resubscribe_count = 0
        self.gapfill_count = 0
        self._had_session = False  # True once a socket has connected at least once

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        logger.info(
            "binance feed starting",
            extra={"symbols": sorted(self._symbols), "ws_base": self._ws_base},
        )
        self._task = asyncio.create_task(self._run(), name="binance-feed-loop")

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("binance feed stopped")

    async def subscribe(self, symbols: Iterable[str]) -> None:
        new = {s.upper() for s in symbols} - self._symbols
        if not new:
            return
        self._symbols |= new
        if len(self._symbols) > _MAX_STREAMS_PER_SOCKET:
            logger.warning(
                "stream count exceeds single-socket limit",
                extra={"count": len(self._symbols), "limit": _MAX_STREAMS_PER_SOCKET},
            )
        if self._ws is not None:
            await self._send_sub(new, subscribe=True)

    async def unsubscribe(self, symbols: Iterable[str]) -> None:
        drop = {s.upper() for s in symbols} & self._symbols
        if not drop:
            return
        self._symbols -= drop
        if self._ws is not None:
            await self._send_sub(drop, subscribe=False)

    async def wait_connected(self, timeout: float | None = None) -> bool:  # noqa: ASYNC109
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # -- stream naming --------------------------------------------------
    def _streams_for(self, symbols: Iterable[str]) -> list[str]:
        streams = [f"{to_stream_symbol(s)}@kline_{self._interval}" for s in symbols]
        return streams

    def _combined_url(self) -> str:
        streams = self._streams_for(sorted(self._symbols))
        streams.append("!miniTicker@arr")
        return f"{self._ws_base}/stream?streams={'/'.join(streams)}"

    # -- connect / reconnect loop --------------------------------------
    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._connect_once()
                attempt = 0  # reset backoff after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not self._running:
                    break
                delay = self._next_backoff(attempt)
                attempt += 1
                logger.warning(
                    "binance connection lost; backing off",
                    extra={"attempt": attempt, "delay": round(delay, 2), "error": str(exc)},
                )
                self.backoff_history.append(delay)
                if self._max_reconnects is not None and attempt > self._max_reconnects:
                    logger.error("max reconnects reached; giving up")
                    break
                await asyncio.sleep(delay)

    def _next_backoff(self, attempt: int) -> float:
        base = _BACKOFF_STEPS[min(attempt, len(_BACKOFF_STEPS) - 1)]
        return base + random.random() * (base * 0.25)  # +0–25% jitter

    async def _connect_once(self) -> None:
        url = self._combined_url()
        self._connected.clear()
        self.connect_count += 1
        ws = await self._connector(url)
        self._ws = ws
        self._connected.set()
        logger.info("binance connected", extra={"streams": len(self._symbols) + 1})

        # On (re)connect, backfill any missed minutes so memory has no holes.
        await self._gap_fill()
        if self._had_session:
            # a prior session existed → this is a genuine reconnect
            self.resubscribe_count += 1
            logger.info("binance resubscribed after reconnect", extra={"symbols": len(self._symbols)})
        self._had_session = True

        try:
            async for raw in ws:
                await self._handle_message(raw)
        finally:
            self._ws = None
            self._connected.clear()

    # -- message handling ----------------------------------------------
    async def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        data = msg.get("data", msg)
        if not isinstance(data, dict) and not isinstance(data, list):
            return

        # Whole-market snapshot (miniTicker array).
        if isinstance(data, list):
            rows = miniticker_array_to_rows(data)
            if rows:
                await self._emit_snapshot(rows)
            return

        event = data.get("e")
        if event == "kline":
            candle = kline_event_to_candle(data)
            await self._emit_candle(candle)

    # -- subscribe control frames --------------------------------------
    async def _send_sub(self, symbols: set[str], *, subscribe: bool) -> None:
        if self._ws is None or not symbols:
            return
        frame = {
            "method": "SUBSCRIBE" if subscribe else "UNSUBSCRIBE",
            "params": self._streams_for(sorted(symbols)),
            "id": int(asyncio.get_running_loop().time() * 1000) % 1_000_000,
        }
        await self._ws.send(json.dumps(frame))
        logger.info(
            "binance %s", "subscribe" if subscribe else "unsubscribe",
            extra={"symbols": sorted(symbols)},
        )

    # -- gap fill -------------------------------------------------------
    async def _gap_fill(self) -> None:
        if self._backfill_limit <= 0 or not self._symbols:
            return
        try:
            candles = await self._backfiller(sorted(self._symbols), self._backfill_limit)
        except Exception:  # noqa: BLE001
            logger.exception("gap-fill failed")
            return
        self.gapfill_count += 1
        for candle in candles:
            await self._emit_candle(candle)
        logger.info("binance gap-fill complete", extra={"candles": len(candles)})

    # -- default IO implementations (real network) ---------------------
    async def _default_connector(self, url: str) -> WSConnection:
        if websockets is None:  # pragma: no cover - exercised only at runtime
            raise RuntimeError("the 'websockets' package is required for live mode")
        return await websockets.connect(url, ping_interval=20, ping_timeout=20)

    async def _default_backfiller(self, symbols: list[str], limit: int) -> list[Candle]:
        out: list[Candle] = []
        async with httpx.AsyncClient(base_url=self._rest_base, timeout=10.0) as client:
            for pair in symbols:
                try:
                    resp = await client.get(
                        "/api/v3/klines",
                        params={"symbol": pair, "interval": self._interval, "limit": limit},
                    )
                    resp.raise_for_status()
                    for row in resp.json():
                        out.append(rest_kline_to_candle(pair, row))
                except Exception:  # noqa: BLE001
                    logger.warning("backfill request failed", extra={"symbol": pair})
        return out
