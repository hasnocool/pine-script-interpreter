"""Data models and report types for deterministic market-data backtests."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, overload

BACKTEST_RUNTIME_VERSION = "0.3.1"


class BacktestError(Exception):
    """Base class for errors raised by the backtest package."""

    location: str | None

    def __init__(self, message: str, location: str | None = None) -> None:
        self.location = location
        super().__init__(message)


class BacktestValidationError(BacktestError, ValueError):
    """Raised when a strategy, candle, or configuration is invalid."""


class BacktestExecutionLimitError(BacktestValidationError):
    """Raised when a strategy exceeds the configured execution-step budget."""

    partial_report: BacktestReport | None = None


class BacktestExecutionTimeoutError(BacktestValidationError):
    """Raised when a strategy exceeds the configured wall-clock budget."""

    partial_report: BacktestReport | None = None


@dataclass(frozen=True, slots=True)
class Candle:
    """One normalized OHLCV bar."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        timestamp = self.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        else:
            timestamp = timestamp.astimezone(UTC)
        object.__setattr__(self, "timestamp", timestamp)
        self.validate()

    @classmethod
    def from_ohlcv(
        cls,
        row: list[float | int],
        timestamp_unit: str = "ms",
    ) -> Candle:
        """Build a candle from the list format returned by CCXT."""

        if len(row) < 5:
            raise BacktestValidationError("CCXT OHLCV rows need at least five values")
        timestamp_value = float(row[0])
        if timestamp_unit == "s":
            timestamp = datetime.fromtimestamp(timestamp_value, UTC)
        elif timestamp_unit == "ms":
            timestamp = datetime.fromtimestamp(timestamp_value / 1_000, UTC)
        elif timestamp_unit == "us":
            timestamp = datetime.fromtimestamp(timestamp_value / 1_000_000, UTC)
        else:
            raise BacktestValidationError(f"unsupported OHLCV timestamp unit: {timestamp_unit!r}")
        return cls(
            timestamp,
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
            float(row[5]) if len(row) > 5 else 0.0,
        )

    @property
    def timestamp_ms(self) -> int:
        return int(self.timestamp.timestamp() * 1_000)

    def validate(self) -> None:
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(value) for value in values):
            raise BacktestValidationError("candle values must be finite")
        if self.high < max(self.open, self.close, self.low):
            raise BacktestValidationError("candle high is below another price")
        if self.low > min(self.open, self.close, self.high):
            raise BacktestValidationError("candle low is above another price")
        if self.volume < 0:
            raise BacktestValidationError("candle volume cannot be negative")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Execution assumptions for a backtest."""

    initial_cash: float = 10_000.0
    fee_rate: float = 0.001
    slippage_bps: float = 0.0
    default_qty: float = 1.0
    pyramiding: int = 1
    allow_short: bool = False
    close_at_end: bool = False
    max_execution_steps: int = 1_000_000
    max_execution_seconds: float = 30.0
    intrabar_policy: str = "stop_first"
    spread_bps: float = 0.0
    funding_rate_bps: float = 0.0
    commission_per_trade: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise BacktestValidationError("initial_cash must be positive and finite")
        if not math.isfinite(self.fee_rate) or not 0 <= self.fee_rate < 1:
            raise BacktestValidationError("fee_rate must be in [0, 1)")
        if not math.isfinite(self.slippage_bps) or self.slippage_bps < 0:
            raise BacktestValidationError("slippage_bps must be non-negative")
        if not math.isfinite(self.default_qty) or self.default_qty <= 0:
            raise BacktestValidationError("default_qty must be positive and finite")
        if (
            not isinstance(self.pyramiding, int)
            or isinstance(self.pyramiding, bool)
            or self.pyramiding < 1
        ):
            raise BacktestValidationError("pyramiding must be a positive integer")
        if (
            not isinstance(self.max_execution_steps, int)
            or isinstance(self.max_execution_steps, bool)
            or self.max_execution_steps <= 0
        ):
            raise BacktestValidationError("max_execution_steps must be a positive integer")
        if not math.isfinite(self.max_execution_seconds) or self.max_execution_seconds <= 0:
            raise BacktestValidationError("max_execution_seconds must be positive and finite")
        if self.intrabar_policy not in {"stop_first", "limit_first", "open_first"}:
            raise BacktestValidationError(
                "intrabar_policy must be stop_first, limit_first, or open_first"
            )
        if not math.isfinite(self.spread_bps) or self.spread_bps < 0:
            raise BacktestValidationError("spread_bps must be non-negative and finite")
        if not math.isfinite(self.funding_rate_bps):
            raise BacktestValidationError("funding_rate_bps must be finite")
        if not math.isfinite(self.commission_per_trade) or self.commission_per_trade < 0:
            raise BacktestValidationError("commission_per_trade must be non-negative and finite")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BacktestConfig:
        """Build a validated configuration from a serialized mapping."""

        try:
            return cls(
                initial_cash=float(data.get("initial_cash", 10_000)),
                fee_rate=float(data.get("fee_rate", 0.001)),
                slippage_bps=float(data.get("slippage_bps", 0.0)),
                default_qty=float(data.get("default_qty", 1.0)),
                pyramiding=int(data.get("pyramiding", 1)),
                allow_short=bool(data.get("allow_short", False)),
                close_at_end=bool(data.get("close_at_end", False)),
                max_execution_steps=int(data.get("max_execution_steps", 1_000_000)),
                max_execution_seconds=float(data.get("max_execution_seconds", 30.0)),
                intrabar_policy=str(data.get("intrabar_policy", "stop_first")),
                spread_bps=float(data.get("spread_bps", 0.0)),
                funding_rate_bps=float(data.get("funding_rate_bps", 0.0)),
                commission_per_trade=float(data.get("commission_per_trade", 0.0)),
            )
        except (TypeError, ValueError) as exc:
            raise BacktestValidationError(f"invalid serialized backtest config: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Trade:
    """A completed round-trip position."""

    entry_index: int
    exit_index: int
    entry_timestamp: datetime
    exit_timestamp: datetime
    side: str
    quantity: float
    entry_price: float
    exit_price: float
    pnl: float
    fees: float
    reason: str = "exit"

    @property
    def return_pct(self) -> float:
        basis = abs(self.entry_price * self.quantity)
        return self.pnl / basis if basis else 0.0

    @property
    def bars_held(self) -> int:
        return self.exit_index - self.entry_index


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Mark-to-market equity at one bar close."""

    index: int
    timestamp: datetime
    equity: float
    drawdown: float = 0.0


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Serializable and printable result of a backtest."""

    name: str
    symbol: str
    timeframe: str
    exchange: str
    initial_cash: float
    final_equity: float
    trades: tuple[Trade, ...]
    equity_curve: tuple[EquityPoint, ...]
    bars: int
    execution_steps: int = 0
    approximations: tuple[str, ...] = ()
    runtime_version: str = BACKTEST_RUNTIME_VERSION
    config: BacktestConfig | None = None
    library_dependencies: tuple[str, ...] = ()
    partial: bool = False
    open_position: dict[str, Any] | None = None

    @property
    def metrics(self) -> dict[str, float | int | bool | None]:
        gross_profit = sum(trade.pnl for trade in self.trades if trade.pnl > 0)
        gross_loss = -sum(trade.pnl for trade in self.trades if trade.pnl < 0)
        winners = sum(trade.pnl > 0 for trade in self.trades)
        peak = self.initial_cash
        max_drawdown = 0.0
        for point in self.equity_curve:
            peak = max(peak, point.equity)
            max_drawdown = max(max_drawdown, peak - point.equity)
        return {
            "bars": self.bars,
            "execution_steps": self.execution_steps,
            "is_partial": self.partial,
            "trade_count": len(self.trades),
            "final_equity": self.final_equity,
            "total_return": self.final_equity - self.initial_cash,
            "total_return_pct": (
                (self.final_equity / self.initial_cash - 1) * 100 if self.initial_cash else 0.0
            ),
            "max_drawdown": max_drawdown,
            "max_drawdown_pct": (
                max_drawdown / self.initial_cash * 100 if self.initial_cash else 0.0
            ),
            "winning_trades": winners,
            "win_rate": winners / len(self.trades) if self.trades else 0.0,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "average_trade": (
                sum(trade.pnl for trade in self.trades) / len(self.trades) if self.trades else 0.0
            ),
        }

    @overload
    def __getitem__(self, key: str) -> object: ...

    @overload
    def __getitem__(self, key: int) -> Trade: ...

    def __getitem__(self, key: str | int) -> object:
        """Index metrics/collections by name or trades by integer position."""

        if isinstance(key, int):
            return self.trades[key]
        if key in {"metrics", "metric"}:
            return self.metrics
        if key == "trades":
            return self.trades
        if key == "equity_curve":
            return self.equity_curve
        if key == "name":
            return self.name
        if key == "symbol":
            return self.symbol
        if key == "timeframe":
            return self.timeframe
        if key == "exchange":
            return self.exchange
        if key == "initial_cash":
            return self.initial_cash
        if key == "final_equity":
            return self.final_equity
        if key == "bars":
            return self.bars
        if key == "runtime_version":
            return self.runtime_version
        if key == "partial":
            return self.partial
        if key == "open_position":
            return self.open_position
        if key == "config":
            return self.config
        metrics = self.metrics
        if key in metrics:
            return metrics[key]
        raise KeyError(key)

    def __iter__(self) -> Iterator[Trade]:
        return iter(self.trades)

    def __len__(self) -> int:
        return len(self.trades)

    def __str__(self) -> str:
        metrics = self.metrics
        return "\n".join(
            (
                f"{self.name} | {self.exchange}:{self.symbol}:{self.timeframe}"
                + (" | PARTIAL" if self.partial else ""),
                f"Bars: {self.bars} | Trades: {len(self.trades)}",
                f"Initial: {self.initial_cash:,.2f} | Final: {self.final_equity:,.2f}",
                (
                    "Return: "
                    f"{metrics['total_return_pct']:.2f}% | "
                    f"Max drawdown: {metrics['max_drawdown_pct']:.2f}%"
                ),
                f"Win rate: {metrics['win_rate']:.2%}",
            )
        )

    def print(self) -> None:
        print(self)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of the report."""

        return {
            "name": self.name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "exchange": self.exchange,
            "initial_cash": self.initial_cash,
            "final_equity": self.final_equity,
            "trades": [
                {
                    **asdict(trade),
                    "entry_timestamp": trade.entry_timestamp.isoformat(),
                    "exit_timestamp": trade.exit_timestamp.isoformat(),
                }
                for trade in self.trades
            ],
            "equity_curve": [
                {**asdict(point), "timestamp": point.timestamp.isoformat()}
                for point in self.equity_curve
            ],
            "bars": self.bars,
            "execution_steps": self.execution_steps,
            "approximations": list(self.approximations),
            "runtime_version": self.runtime_version,
            "config": asdict(self.config) if self.config is not None else None,
            "library_dependencies": list(self.library_dependencies),
            "partial": self.partial,
            "open_position": self.open_position,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BacktestReport:
        """Rehydrate a completed or partial report from JSON-compatible data."""

        try:
            trades = tuple(
                Trade(
                    entry_index=int(trade["entry_index"]),
                    exit_index=int(trade["exit_index"]),
                    entry_timestamp=cls._parse_datetime(trade["entry_timestamp"]),
                    exit_timestamp=cls._parse_datetime(trade["exit_timestamp"]),
                    side=str(trade["side"]),
                    quantity=float(trade["quantity"]),
                    entry_price=float(trade["entry_price"]),
                    exit_price=float(trade["exit_price"]),
                    pnl=float(trade["pnl"]),
                    fees=float(trade["fees"]),
                    reason=str(trade.get("reason", "exit")),
                )
                for trade in data.get("trades", [])
            )
            equity_curve = tuple(
                EquityPoint(
                    index=int(point["index"]),
                    timestamp=cls._parse_datetime(point["timestamp"]),
                    equity=float(point["equity"]),
                    drawdown=float(point.get("drawdown", 0.0)),
                )
                for point in data.get("equity_curve", [])
            )
            config_data = data.get("config")
            return cls(
                name=str(data["name"]),
                symbol=str(data["symbol"]),
                timeframe=str(data["timeframe"]),
                exchange=str(data["exchange"]),
                initial_cash=float(data["initial_cash"]),
                final_equity=float(data["final_equity"]),
                trades=trades,
                equity_curve=equity_curve,
                bars=int(data["bars"]),
                execution_steps=int(data.get("execution_steps", 0)),
                approximations=tuple(str(item) for item in data.get("approximations", [])),
                runtime_version=str(data.get("runtime_version", BACKTEST_RUNTIME_VERSION)),
                config=(
                    BacktestConfig.from_dict(config_data)
                    if isinstance(config_data, Mapping)
                    else None
                ),
                library_dependencies=tuple(
                    str(item) for item in data.get("library_dependencies", [])
                ),
                partial=bool(data.get("partial", False)),
                open_position=(
                    dict(data["open_position"])
                    if isinstance(data.get("open_position"), Mapping)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BacktestValidationError(f"invalid serialized backtest report: {exc}") from exc

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def print_report(report: BacktestReport) -> None:
    """Print a concise report to stdout."""

    print(report)
