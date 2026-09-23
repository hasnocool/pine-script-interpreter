"""Public OHLCV data access through an optional CCXT dependency."""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pine_interpreter.backtest.models import BacktestValidationError, Candle


class CCXTDataFeed:
    """Fetch public candles without API keys.

    CCXT is imported lazily so parsing and deterministic tests do not require
    the optional dependency.  The feed deliberately uses only ``fetch_ohlcv``;
    no private or authenticated endpoint is contacted.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        *,
        exchange: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        if not exchange_id.strip():
            raise BacktestValidationError("exchange_id cannot be empty")
        if not symbol.strip():
            raise BacktestValidationError("symbol cannot be empty")
        if not timeframe.strip():
            raise BacktestValidationError("timeframe cannot be empty")
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.timeframe = timeframe
        self.exchange = exchange
        self.params = dict(params or {})

    def _get_exchange(self) -> Any:
        if self.exchange is not None:
            return self.exchange
        try:
            ccxt = importlib.import_module("ccxt")
        except ImportError as exc:
            raise BacktestValidationError(
                "CCXT is required for live data; install pine-interpreter[backtest]"
            ) from exc
        exchange_class = getattr(ccxt, self.exchange_id, None)
        if exchange_class is None:
            raise BacktestValidationError(f"unknown CCXT exchange: {self.exchange_id!r}")
        self.exchange = exchange_class({"enableRateLimit": True})
        return self.exchange

    def fetch(
        self,
        *,
        limit: int = 500,
        since: int | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[Candle, ...]:
        """Fetch and normalize public OHLCV candles."""

        if not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise BacktestValidationError("limit must be an integer in [1, 1000]")
        request_params = dict(self.params)
        request_params.update(params or {})
        exchange = self._get_exchange()
        try:
            rows = exchange.fetch_ohlcv(
                self.symbol,
                timeframe=self.timeframe,
                since=since,
                limit=limit,
                params=request_params,
            )
        except Exception as exc:
            raise BacktestValidationError(
                f"CCXT OHLCV request failed for {self.exchange_id}:{self.symbol}: {exc}"
            ) from exc
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise BacktestValidationError("CCXT returned an invalid OHLCV response")
        candles: list[Candle] = []
        for row in rows:
            if not isinstance(row, Sequence) or len(row) < 5 or row[0] is None:
                continue
            candles.append(Candle.from_ohlcv(list(row)))
        candles.sort(key=lambda candle: candle.timestamp)
        unique: list[Candle] = []
        for candle in candles:
            if unique and candle.timestamp == unique[-1].timestamp:
                continue
            unique.append(candle)
        return tuple(unique)

    def fetch_ohlcv(
        self,
        *,
        limit: int = 500,
        since: int | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[Candle, ...]:
        """Alias matching CCXT's method name for discoverability."""

        return self.fetch(limit=limit, since=since, params=params)

    def fetch_public_ohlcv(
        self,
        *,
        limit: int = 500,
        since: int | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[Candle, ...]:
        """Explicitly named alias for public-data callers."""

        return self.fetch(limit=limit, since=since, params=params)


def fetch_public_ohlcv(
    exchange_id: str = "binance",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    *,
    limit: int = 500,
    since: int | None = None,
    params: Mapping[str, Any] | None = None,
) -> tuple[Candle, ...]:
    """One-call public CCXT data helper."""

    return CCXTDataFeed(exchange_id, symbol, timeframe).fetch(
        limit=limit, since=since, params=params
    )


def candles_to_rows(candles: Sequence[Candle]) -> list[list[float]]:
    """Convert candles back to CCXT-style rows for adapters and reports."""

    return [
        [
            candle.timestamp_ms,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
        ]
        for candle in candles
    ]


def save_candles(path: str | Path, candles: Sequence[Candle]) -> Path:
    """Persist normalized candles as a small JSON cache."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(candles_to_rows(candles)),
        encoding="utf-8",
    )
    return destination


def load_candles(path: str | Path) -> tuple[Candle, ...]:
    """Load a cache created by :func:`save_candles`."""

    source = Path(path)
    try:
        rows = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestValidationError(f"could not load candle cache {source}: {exc}") from exc
    if not isinstance(rows, list):
        raise BacktestValidationError("candle cache must contain a JSON list")
    return tuple(Candle.from_ohlcv(row) for row in rows)


def utc_now() -> datetime:
    """Return the current UTC time (useful for report metadata)."""

    return datetime.now(UTC)
