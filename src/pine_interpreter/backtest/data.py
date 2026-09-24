"""Public OHLCV data access through an optional CCXT dependency."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pine_interpreter.backtest.models import BacktestValidationError, Candle


@dataclass(frozen=True, slots=True)
class CandleSnapshot:
    """Reproducible metadata and normalized candles for one data run."""

    exchange: str
    symbol: str
    timeframe: str
    candles: tuple[Candle, ...]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = "ccxt"
    warnings: tuple[str, ...] = ()

    @property
    def content_hash(self) -> str:
        return candle_data_hash(self.candles)

    @property
    def start(self) -> datetime | None:
        return self.candles[0].timestamp if self.candles else None

    @property
    def end(self) -> datetime | None:
        return self.candles[-1].timestamp if self.candles else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "fetched_at": self.fetched_at.isoformat(),
            "source": self.source,
            "content_hash": self.content_hash,
            "candle_count": len(self.candles),
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "warnings": list(self.warnings),
            "candles": candles_to_rows(self.candles),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CandleSnapshot:
        rows = data.get("candles")
        if not isinstance(rows, list):
            raise BacktestValidationError("candle snapshot is missing its candles list")
        fetched_at_value = data.get("fetched_at")
        try:
            fetched_at = (
                datetime.fromisoformat(str(fetched_at_value))
                if fetched_at_value
                else datetime.now(UTC)
            )
            fetched_at = (
                fetched_at.replace(tzinfo=UTC)
                if fetched_at.tzinfo is None
                else fetched_at.astimezone(UTC)
            )
        except ValueError as exc:
            raise BacktestValidationError("candle snapshot has an invalid fetched_at") from exc
        warnings_value = data.get("warnings", [])
        warnings = list(
            str(item) for item in (warnings_value if isinstance(warnings_value, list) else [])
        )
        candles = tuple(Candle.from_ohlcv(row) for row in rows)
        stored_hash = data.get("content_hash")
        if stored_hash and str(stored_hash) != candle_data_hash(candles):
            warnings.append("stored candle content hash does not match candle rows")
        return cls(
            str(data.get("exchange", "unknown")),
            str(data.get("symbol", "unknown")),
            str(data.get("timeframe", "unknown")),
            candles,
            fetched_at,
            str(data.get("source", "unknown")),
            tuple(warnings),
        )


def candle_data_hash(candles: Sequence[Candle]) -> str:
    """Return a stable SHA-256 hash for a normalized candle sequence."""

    rows = [[float(value) for value in row] for row in candles_to_rows(candles)]
    payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def candle_quality_warnings(
    candles: Sequence[Candle],
    timeframe: str | None = None,
) -> tuple[str, ...]:
    """Return non-fatal data-quality observations for a candle sequence."""

    warnings: list[str] = []
    if not candles:
        return ("candle sequence is empty",)
    timestamps = [candle.timestamp for candle in candles]
    if timestamps != sorted(timestamps):
        warnings.append("candles are not in chronological order")
    duplicate_count = len(timestamps) - len(set(timestamps))
    if duplicate_count:
        warnings.append(f"candle sequence contains {duplicate_count} duplicate timestamps")
    zero_volume_count = sum(candle.volume == 0 for candle in candles)
    if zero_volume_count:
        warnings.append(f"candle sequence contains {zero_volume_count} zero-volume candles")
    expected_seconds = timeframe_seconds(timeframe) if timeframe else None
    if expected_seconds is not None and len(timestamps) > 1:
        gaps = sum(
            (current - previous).total_seconds() > expected_seconds * 1.5
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        )
        if gaps:
            warnings.append(f"candle sequence contains {gaps} likely timeframe gaps")
    return tuple(warnings)


def timeframe_seconds(timeframe: str | None) -> int | None:
    """Convert common simple timeframes to seconds for quality checks."""

    if not timeframe:
        return None
    value = timeframe.strip().lower()
    units = {"s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}
    suffix = value[-1]
    if suffix not in units or not value[:-1].isdigit():
        return None
    count = int(value[:-1])
    return count * units[suffix] if count > 0 else None


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
        """Fetch and normalize public OHLCV candles.

        CCXT endpoints commonly cap a single OHLCV request at 1,000 rows.  The
        feed transparently pages forward when ``since`` is supplied and pages
        backward with an ``until`` parameter when the caller asks for a long
        latest-data window without a start timestamp.  Exchanges that do not
        implement ``until`` may return fewer rows than requested rather than
        causing a retry loop.
        """

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100_000:
            raise BacktestValidationError("limit must be an integer in [1, 100000]")
        request_params = dict(self.params)
        request_params.update(params or {})
        exchange = self._get_exchange()
        collected: dict[int, Candle] = {}
        cursor = since
        backward_until: int | None = None
        page_number = 0
        while len(collected) < limit and page_number < 100:
            request_limit = min(1_000, limit - len(collected))
            page_params = dict(request_params)
            request_since = since if page_number == 0 else cursor
            if page_number > 0 and backward_until is not None:
                page_params.setdefault("until", backward_until)
                request_since = None
            try:
                rows = exchange.fetch_ohlcv(
                    self.symbol,
                    timeframe=self.timeframe,
                    since=request_since,
                    limit=request_limit,
                    params=page_params,
                )
            except Exception as exc:
                raise BacktestValidationError(
                    f"CCXT OHLCV request failed for {self.exchange_id}:{self.symbol}: {exc}"
                ) from exc
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise BacktestValidationError("CCXT returned an invalid OHLCV response")
            page_candles: list[Candle] = []
            for row in rows:
                if not isinstance(row, Sequence) or len(row) < 5 or row[0] is None:
                    continue
                candle = Candle.from_ohlcv(list(row))
                if cursor is not None and page_number > 0 and candle.timestamp_ms < cursor:
                    continue
                if (
                    backward_until is not None
                    and page_number > 0
                    and candle.timestamp_ms > backward_until
                ):
                    continue
                page_candles.append(candle)
            before = len(collected)
            for candle in page_candles:
                collected[candle.timestamp_ms] = candle
            if not page_candles or len(collected) == before:
                break
            if page_candles:
                newest = max(candle.timestamp_ms for candle in page_candles)
                oldest = min(candle.timestamp_ms for candle in page_candles)
                if since is None and backward_until is None:
                    if len(page_candles) >= request_limit:
                        backward_until = oldest - 1
                        cursor = None
                    else:
                        break
                else:
                    cursor = newest + 1
            page_number += 1
        candles = sorted(collected.values(), key=lambda candle: candle.timestamp)
        return tuple(candles[:limit])

    def fetch_snapshot(
        self,
        *,
        limit: int = 500,
        since: int | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> CandleSnapshot:
        """Fetch candles and attach reproducible snapshot metadata."""

        candles = self.fetch(limit=limit, since=since, params=params)
        warnings = list(candle_quality_warnings(candles, self.timeframe))
        if len(candles) < limit:
            warnings.append(f"requested {limit} candles but the exchange returned {len(candles)}")
        return CandleSnapshot(
            self.exchange_id,
            self.symbol,
            self.timeframe,
            candles,
            datetime.now(UTC),
            "ccxt",
            tuple(warnings),
        )

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


def fetch_public_snapshot(
    exchange_id: str = "binance",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    *,
    limit: int = 500,
    since: int | None = None,
    params: Mapping[str, Any] | None = None,
) -> CandleSnapshot:
    """One-call public CCXT snapshot helper."""

    return CCXTDataFeed(exchange_id, symbol, timeframe).fetch_snapshot(
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


def save_snapshot(path: str | Path, snapshot: CandleSnapshot) -> Path:
    """Persist a candle snapshot with provenance and quality metadata."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    return destination


def load_snapshot(path: str | Path) -> CandleSnapshot:
    """Load a snapshot, accepting the legacy list-only cache format."""

    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BacktestValidationError(f"could not load candle cache {source}: {exc}") from exc
    if isinstance(payload, Mapping):
        return CandleSnapshot.from_dict(payload)
    if isinstance(payload, list):
        candles = tuple(Candle.from_ohlcv(row) for row in payload)
        return CandleSnapshot(
            "unknown",
            "unknown",
            "unknown",
            candles,
            datetime.now(UTC),
            "legacy-list-cache",
            candle_quality_warnings(candles),
        )
    raise BacktestValidationError("candle cache must contain a JSON list or snapshot object")


def load_candles(path: str | Path) -> tuple[Candle, ...]:
    """Load candles from either the current or legacy cache format."""

    return load_snapshot(path).candles


def utc_now() -> datetime:
    """Return the current UTC time (useful for report metadata)."""

    return datetime.now(UTC)
