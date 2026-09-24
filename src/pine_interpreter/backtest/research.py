"""Out-of-sample and walk-forward research helpers."""

from __future__ import annotations

import json
import math
import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import mean, median
from typing import Any, overload

from pine_interpreter.backtest.engine import BacktestEngine
from pine_interpreter.backtest.models import (
    BACKTEST_RUNTIME_VERSION,
    BacktestConfig,
    BacktestReport,
    BacktestValidationError,
    Candle,
)


@dataclass(frozen=True, slots=True)
class ResearchSplit:
    """Three chronological candle partitions."""

    train: tuple[Candle, ...]
    validation: tuple[Candle, ...]
    test: tuple[Candle, ...]

    def to_dict(self) -> dict[str, int]:
        return {
            "train": len(self.train),
            "validation": len(self.validation),
            "test": len(self.test),
        }


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    """One train/test window without any future-bar overlap."""

    name: str
    train: tuple[Candle, ...]
    test: tuple[Candle, ...]


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """The two reports produced for one walk-forward window."""

    name: str
    train: BacktestReport
    test: BacktestReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "train": {
                "bars": self.train.bars,
                "runtime_version": self.train.runtime_version,
                "config": asdict(self.train.config) if self.train.config is not None else None,
                "library_dependencies": list(self.train.library_dependencies),
                "metrics": self.train.metrics,
            },
            "test": {
                "bars": self.test.bars,
                "runtime_version": self.test.runtime_version,
                "config": asdict(self.test.config) if self.test.config is not None else None,
                "library_dependencies": list(self.test.library_dependencies),
                "metrics": self.test.metrics,
            },
        }


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """Aggregated walk-forward results with train/test comparison."""

    name: str
    symbol: str
    timeframe: str
    exchange: str
    folds: tuple[WalkForwardFold, ...]
    periods_per_year: int = 365

    @property
    def validation_status(self) -> str:
        return (
            "validated"
            if self.folds and all(fold.test.bars > 0 for fold in self.folds)
            else "unvalidated"
        )

    @property
    def summary(self) -> dict[str, int | float | str]:
        train_returns = [fold.train.metrics["total_return_pct"] for fold in self.folds]
        test_returns = [fold.test.metrics["total_return_pct"] for fold in self.folds]
        test_drawdowns = [fold.test.metrics["max_drawdown_pct"] for fold in self.folds]
        numeric_train = [float(value or 0.0) for value in train_returns]
        numeric_test = [float(value or 0.0) for value in test_returns]
        numeric_drawdowns = [float(value or 0.0) for value in test_drawdowns]
        return {
            "folds": len(self.folds),
            "validation_status": self.validation_status,
            "positive_test_folds": sum(value > 0 for value in numeric_test),
            "negative_test_folds": sum(value < 0 for value in numeric_test),
            "mean_train_return_pct": mean(numeric_train) if numeric_train else 0.0,
            "mean_test_return_pct": mean(numeric_test) if numeric_test else 0.0,
            "median_test_return_pct": median(numeric_test) if numeric_test else 0.0,
            "mean_test_drawdown_pct": mean(numeric_drawdowns) if numeric_drawdowns else 0.0,
            "generalization_gap_pct": (
                mean(numeric_train) - mean(numeric_test) if numeric_train and numeric_test else 0.0
            ),
        }

    @property
    def by_name(self) -> dict[str, WalkForwardFold]:
        return {fold.name: fold for fold in self.folds}

    def __len__(self) -> int:
        return len(self.folds)

    def __iter__(self) -> Iterator[WalkForwardFold]:
        return iter(self.folds)

    @overload
    def __getitem__(self, key: int) -> WalkForwardFold: ...

    @overload
    def __getitem__(self, key: str) -> object: ...

    def __getitem__(self, key: int | str) -> WalkForwardFold | object:
        if isinstance(key, int):
            return self.folds[key]
        if key == "summary":
            return self.summary
        if key == "validation_status":
            return self.validation_status
        if key == "folds":
            return self.folds
        result = self.by_name.get(key)
        if result is not None:
            return result
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                "name": self.name,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "exchange": self.exchange,
                "periods_per_year": self.periods_per_year,
                "runtime_version": BACKTEST_RUNTIME_VERSION,
            },
            "summary": self.summary,
            "folds": [fold.to_dict() for fold in self.folds],
        }

    def write_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return destination

    def print(self) -> None:
        summary = self.summary
        print(
            f"Walk-forward: {self.name} | {self.symbol}:{self.timeframe} | {len(self.folds)} folds"
        )
        print(
            f"Mean train return: {summary['mean_train_return_pct']:.2f}% | "
            f"mean test return: {summary['mean_test_return_pct']:.2f}% | "
            f"positive test folds: {summary['positive_test_folds']}/{len(self.folds)}"
        )


@dataclass(frozen=True, slots=True)
class ParameterRun:
    """One input configuration in a bounded parameter sweep."""

    name: str
    overrides: dict[str, Any]
    report: BacktestReport | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "overrides": self.overrides,
            "error": self.error,
            "runtime_version": self.report.runtime_version if self.report is not None else None,
            "library_dependencies": list(self.report.library_dependencies)
            if self.report is not None
            else [],
            "metrics": self.report.metrics if self.report is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ParameterSweepReport:
    """Results for a bounded parameter sweep."""

    name: str
    symbol: str
    timeframe: str
    exchange: str
    runs: tuple[ParameterRun, ...]

    @property
    def summary(self) -> dict[str, int | float | str]:
        successful = [run for run in self.runs if run.report is not None]
        returns = [
            float(run.report.metrics["total_return_pct"] or 0) for run in successful if run.report
        ]
        return {
            "runs": len(self.runs),
            "successful": len(successful),
            "failed": len(self.runs) - len(successful),
            "best_return_pct": max(returns) if returns else 0.0,
            "mean_return_pct": mean(returns) if returns else 0.0,
        }

    @property
    def by_name(self) -> dict[str, ParameterRun]:
        return {run.name: run for run in self.runs}

    def __len__(self) -> int:
        return len(self.runs)

    def __iter__(self) -> Iterator[ParameterRun]:
        return iter(self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                "name": self.name,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "exchange": self.exchange,
                "runtime_version": BACKTEST_RUNTIME_VERSION,
            },
            "summary": self.summary,
            "runs": [run.to_dict() for run in self.runs],
        }

    def write_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return destination


@dataclass(frozen=True, slots=True)
class MarketRun:
    """One strategy result for a market/timeframe configuration."""

    exchange: str
    symbol: str
    timeframe: str
    report: BacktestReport | None
    error: str | None = None

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}:{self.timeframe}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "error": self.error,
            "runtime_version": self.report.runtime_version if self.report is not None else None,
            "library_dependencies": list(self.report.library_dependencies)
            if self.report is not None
            else [],
            "metrics": self.report.metrics if self.report is not None else None,
        }


@dataclass(frozen=True, slots=True)
class MarketComparisonReport:
    """Comparable results across several public-market data configurations."""

    name: str
    runs: tuple[MarketRun, ...]

    @property
    def summary(self) -> dict[str, int | float | str]:
        successful = [run.report for run in self.runs if run.report is not None]
        returns = [float(report.metrics["total_return_pct"] or 0) for report in successful]
        return {
            "markets": len(self.runs),
            "successful": len(successful),
            "failed": len(self.runs) - len(successful),
            "mean_return_pct": mean(returns) if returns else 0.0,
            "best_return_pct": max(returns) if returns else 0.0,
        }

    def __len__(self) -> int:
        return len(self.runs)

    def __iter__(self) -> Iterator[MarketRun]:
        return iter(self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                "name": self.name,
                "runtime_version": BACKTEST_RUNTIME_VERSION,
            },
            "summary": self.summary,
            "markets": [run.to_dict() for run in self.runs],
        }

    def write_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return destination


def compare_markets(
    source: str,
    datasets: Mapping[tuple[str, str, str], Sequence[Candle]],
    *,
    config: BacktestConfig | None = None,
    name: str = "pine-strategy",
    library_root: str | Path | None = None,
) -> MarketComparisonReport:
    """Run one source against multiple exchange/symbol/timeframe datasets."""

    if not datasets:
        raise BacktestValidationError("at least one market dataset is required")
    runs: list[MarketRun] = []
    for (exchange, symbol, timeframe), candles in datasets.items():
        try:
            report = BacktestEngine(config, library_root=library_root).run(
                source,
                candles,
                name=f"{name}-{symbol}-{timeframe}",
                symbol=symbol,
                timeframe=timeframe,
                exchange=exchange,
            )
            runs.append(MarketRun(exchange, symbol, timeframe, report))
        except Exception as exc:
            runs.append(MarketRun(exchange, symbol, timeframe, None, str(exc)))
    return MarketComparisonReport(name, tuple(runs))


def split_candles(
    candles: Sequence[Candle],
    *,
    train_ratio: float = 0.7,
    validation_ratio: float = 0.15,
) -> ResearchSplit:
    """Split candles chronologically into train, validation, and test sets."""

    if not 0 < train_ratio < 1:
        raise BacktestValidationError("train_ratio must be between zero and one")
    if not 0 <= validation_ratio < 1:
        raise BacktestValidationError("validation_ratio must be between zero and one")
    if train_ratio + validation_ratio >= 1:
        raise BacktestValidationError("train_ratio + validation_ratio must be below one")
    normalized = tuple(candles)
    if len(normalized) < 3:
        raise BacktestValidationError("at least three candles are required for a split")
    train_end = max(1, int(len(normalized) * train_ratio))
    validation_end = max(train_end + 1, int(len(normalized) * (train_ratio + validation_ratio)))
    validation_end = min(validation_end, len(normalized) - 1)
    return ResearchSplit(
        tuple(normalized[:train_end]),
        tuple(normalized[train_end:validation_end]),
        tuple(normalized[validation_end:]),
    )


def walk_forward_windows(
    candles: Sequence[Candle],
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
    anchored: bool = False,
) -> tuple[WalkForwardWindow, ...]:
    """Create chronological rolling or anchored train/test windows."""

    normalized = tuple(candles)
    if train_size < 1 or test_size < 1:
        raise BacktestValidationError("train_size and test_size must be positive")
    stride = test_size if step is None else step
    if stride < 1:
        raise BacktestValidationError("step must be positive")
    last_start = len(normalized) - train_size - test_size
    if last_start < 0:
        raise BacktestValidationError("not enough candles for the requested windows")
    windows: list[WalkForwardWindow] = []
    for index, start in enumerate(range(0, last_start + 1, stride), start=1):
        train_start = 0 if anchored else start
        train_end = start + train_size
        test_end = train_end + test_size
        windows.append(
            WalkForwardWindow(
                f"fold-{index}",
                tuple(normalized[train_start:train_end]),
                tuple(normalized[train_end:test_end]),
            )
        )
    return tuple(windows)


def run_walk_forward(
    source: str,
    candles: Sequence[Candle],
    *,
    windows: Sequence[WalkForwardWindow] | None = None,
    train_ratio: float = 0.7,
    train_size: int | None = None,
    test_size: int = 100,
    step: int | None = None,
    anchored: bool = False,
    config: BacktestConfig | None = None,
    name: str = "pine-strategy",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
    periods_per_year: int = 365,
    library_root: str | Path | None = None,
) -> WalkForwardReport:
    """Run independent train/test folds and return a comparison report."""

    if periods_per_year < 1:
        raise BacktestValidationError("periods_per_year must be positive")
    if windows is None:
        if train_size is not None:
            windows = walk_forward_windows(
                candles,
                train_size=train_size,
                test_size=test_size,
                step=step,
                anchored=anchored,
            )
        else:
            split = split_candles(candles, train_ratio=train_ratio)
            windows = (
                WalkForwardWindow(
                    "fold-1",
                    split.train + split.validation,
                    split.test,
                ),
            )
    if not windows:
        raise BacktestValidationError("at least one walk-forward window is required")
    engine = BacktestEngine(config, library_root=library_root)
    folds: list[WalkForwardFold] = []
    for window in windows:
        train_report = engine.run(
            source,
            window.train,
            name=f"{name}-{window.name}-train",
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        )
        test_report = engine.run(
            source,
            window.test,
            name=f"{name}-{window.name}-test",
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        )
        folds.append(WalkForwardFold(window.name, train_report, test_report))
    return WalkForwardReport(name, symbol, timeframe, exchange, tuple(folds), periods_per_year)


def run_out_of_sample(
    source: str,
    candles: Sequence[Candle],
    *,
    train_ratio: float = 0.7,
    config: BacktestConfig | None = None,
    name: str = "pine-strategy",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
    periods_per_year: int = 365,
    library_root: str | Path | None = None,
) -> WalkForwardReport:
    """Run one chronological train/test split and expose both reports."""

    split = split_candles(candles, train_ratio=train_ratio, validation_ratio=0.0)
    window = WalkForwardWindow("out-of-sample", split.train, split.test)
    return run_walk_forward(
        source,
        candles,
        windows=(window,),
        config=config,
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        periods_per_year=periods_per_year,
        library_root=library_root,
    )


def buy_and_hold_report(
    candles: Sequence[Candle],
    *,
    config: BacktestConfig | None = None,
    name: str = "buy-and-hold",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
) -> BacktestReport:
    """Run a simple buy-and-hold benchmark through the same broker model."""

    normalized = tuple(candles)
    if not normalized:
        raise BacktestValidationError("at least one candle is required")
    settings = config or BacktestConfig(close_at_end=True)
    quantity = settings.initial_cash / normalized[0].close * 0.999
    source = (
        "//@version=6\n"
        f'strategy("{name}", overlay=true)\n'
        "if bar_index == 0\n"
        f'    strategy.entry("Long", strategy.long, qty={quantity!r})\n'
        f"if bar_index == {len(normalized) - 1}\n"
        '    strategy.close("Long")\n'
    )
    return BacktestEngine(settings).run(
        source,
        normalized,
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
    )


def no_trade_report(
    candles: Sequence[Candle],
    *,
    config: BacktestConfig | None = None,
    name: str = "no-trade",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
) -> BacktestReport:
    """Return a no-order baseline under the same execution assumptions."""

    normalized = tuple(candles)
    if not normalized:
        raise BacktestValidationError("at least one candle is required")
    settings = replace(config or BacktestConfig(), close_at_end=True)
    source = f'//@version=6\nstrategy("{name}", overlay=true)\nvalue = close\n'
    return BacktestEngine(settings).run(
        source,
        normalized,
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
    )


def moving_average_crossover_report(
    candles: Sequence[Candle],
    *,
    fast_length: int = 10,
    slow_length: int = 30,
    config: BacktestConfig | None = None,
    name: str = "ma-crossover",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
) -> BacktestReport:
    """Run a reproducible moving-average crossover benchmark."""

    if fast_length < 1 or slow_length < 1 or fast_length >= slow_length:
        raise BacktestValidationError("benchmark lengths must satisfy 0 < fast < slow")
    normalized = tuple(candles)
    if len(normalized) < slow_length:
        raise BacktestValidationError("not enough candles for the moving-average benchmark")
    settings = replace(config or BacktestConfig(), close_at_end=True)
    source = (
        "//@version=6\n"
        f'strategy("{name}", overlay=true)\n'
        f"fast = ta.sma(close, {fast_length})\n"
        f"slow = ta.sma(close, {slow_length})\n"
        "if ta.crossover(fast, slow)\n"
        '    strategy.entry("Long", strategy.long)\n'
        "if ta.crossunder(fast, slow)\n"
        '    strategy.close("Long")\n'
    )
    return BacktestEngine(settings).run(
        source,
        normalized,
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
    )


def random_entry_report(
    candles: Sequence[Candle],
    *,
    trade_count: int | None = None,
    seed: int = 0,
    config: BacktestConfig | None = None,
    name: str = "random-entry",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
) -> BacktestReport:
    """Run a deterministic random-entry benchmark.

    The generated source places non-overlapping entries and exits on fixed
    bars.  Supplying ``trade_count`` lets callers match the completed-trade
    count of a strategy while keeping the comparison reproducible.
    """

    normalized = tuple(candles)
    if len(normalized) < 2:
        raise BacktestValidationError("at least two candles are required")
    maximum = max(1, len(normalized) // 2)
    requested = maximum if trade_count is None else int(trade_count)
    if requested < 1 or requested > maximum:
        raise BacktestValidationError("trade_count must be between one and half the candle count")
    generator = random.Random(seed)
    candidates = list(range(0, len(normalized) - 1, 2))
    generator.shuffle(candidates)
    selected = set(candidates[:requested])
    exits = {entry_index: entry_index + 1 for entry_index in selected}
    if len(selected) < requested:
        raise BacktestValidationError("could not place enough non-overlapping random trades")
    settings = replace(config or BacktestConfig(), close_at_end=True)
    quantity = settings.default_qty
    events: list[str] = []
    for entry_index in sorted(selected):
        exit_index = exits[entry_index]
        events.extend(
            (
                f"if bar_index == {entry_index}\n"
                f'    strategy.entry("Long", strategy.long, qty={quantity!r})\n',
                f'if bar_index == {exit_index}\n    strategy.close("Long")\n',
            )
        )
    source = "//@version=6\n" + f'strategy("{name}", overlay=true)\n' + "".join(events)
    return BacktestEngine(settings).run(
        source,
        normalized,
        name=name,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
    )


def benchmark_reports(
    candles: Sequence[Candle],
    *,
    config: BacktestConfig | None = None,
    trade_count: int | None = None,
    seed: int = 0,
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
) -> dict[str, BacktestReport]:
    """Return the standard no-trade, random, moving-average, and buy/hold set."""

    normalized = tuple(candles)
    if len(normalized) < 2:
        raise BacktestValidationError("at least two candles are required for benchmarks")
    fast_length = min(10, max(1, len(normalized) // 4))
    slow_length = min(30, max(fast_length + 1, len(normalized) // 2))
    benchmark_config = replace(
        config or BacktestConfig(),
        max_execution_steps=max((config or BacktestConfig()).max_execution_steps, 1_000_000),
        max_execution_seconds=max((config or BacktestConfig()).max_execution_seconds, 30.0),
    )
    return {
        "no_trade": no_trade_report(
            normalized,
            config=benchmark_config,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        ),
        "random": random_entry_report(
            normalized,
            trade_count=trade_count,
            seed=seed,
            config=benchmark_config,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        ),
        "ma_crossover": moving_average_crossover_report(
            normalized,
            fast_length=fast_length,
            slow_length=slow_length,
            config=benchmark_config,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        ),
        "buy_and_hold": buy_and_hold_report(
            normalized,
            config=benchmark_config,
            symbol=symbol,
            timeframe=timeframe,
            exchange=exchange,
        ),
    }


def risk_metrics(
    report: BacktestReport,
    *,
    periods_per_year: int = 365,
) -> dict[str, float | int | None]:
    """Calculate annualized and risk-adjusted metrics from an equity curve."""

    if periods_per_year < 1:
        raise BacktestValidationError("periods_per_year must be positive")
    equity = [report.initial_cash, *[point.equity for point in report.equity_curve]]
    returns = [
        equity[index] / equity[index - 1] - 1
        for index in range(1, len(equity))
        if equity[index - 1] != 0
    ]
    average_return = mean(returns) if returns else 0.0
    volatility = (
        math.sqrt(sum((value - average_return) ** 2 for value in returns) / len(returns))
        if returns
        else 0.0
    )
    annualized_return = (
        (report.final_equity / report.initial_cash) ** (periods_per_year / max(1, report.bars)) - 1
        if report.final_equity > 0 and report.initial_cash > 0
        else -1.0
    )
    max_drawdown_pct = float(report.metrics["max_drawdown_pct"] or 0.0)
    trades = report.trades
    winners = [trade for trade in trades if trade.pnl > 0]
    losers = [trade for trade in trades if trade.pnl < 0]
    gross_profit = sum(trade.pnl for trade in winners)
    gross_loss = -sum(trade.pnl for trade in losers)
    covered_bars: set[int] = set()
    for trade in trades:
        start = max(0, trade.entry_index)
        end = max(start + 1, min(report.bars, trade.exit_index + 1))
        covered_bars.update(range(start, end))
    downside = [min(value, 0.0) for value in returns]
    downside_deviation = (
        math.sqrt(sum(value**2 for value in downside) / len(downside)) if downside else 0.0
    )
    expectancy = sum(trade.pnl for trade in trades) / len(trades) if trades else 0.0
    return {
        "annualized_return_pct": annualized_return * 100,
        "annualized_volatility_pct": volatility * math.sqrt(periods_per_year) * 100,
        "sharpe": average_return / volatility * math.sqrt(periods_per_year) if volatility else 0.0,
        "sortino": (
            average_return / downside_deviation * math.sqrt(periods_per_year)
            if downside_deviation
            else 0.0
        ),
        "calmar": annualized_return * 100 / max_drawdown_pct if max_drawdown_pct else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "expectancy": expectancy,
        "win_rate": len(winners) / len(trades) if trades else 0.0,
        "average_trade": expectancy,
        "average_holding_bars": mean(trade.bars_held for trade in trades) if trades else 0.0,
        "time_in_market_pct": len(covered_bars) / report.bars * 100 if report.bars else 0.0,
        "trade_count": len(trades),
        "periods_per_year": periods_per_year,
    }


def run_parameter_sweep(
    source: str,
    candles: Sequence[Candle],
    parameter_sets: Sequence[Mapping[str, Any]],
    *,
    config: BacktestConfig | None = None,
    name: str = "pine-strategy",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
    exchange: str = "synthetic",
    library_root: str | Path | None = None,
) -> ParameterSweepReport:
    """Run a bounded set of input overrides and retain failures per run."""

    if not parameter_sets:
        raise BacktestValidationError("at least one parameter set is required")
    runs: list[ParameterRun] = []
    for index, overrides in enumerate(parameter_sets, start=1):
        normalized = dict(overrides)
        try:
            report = BacktestEngine(
                config, input_overrides=normalized, library_root=library_root
            ).run(
                source,
                candles,
                name=f"{name}-parameters-{index}",
                symbol=symbol,
                timeframe=timeframe,
                exchange=exchange,
            )
            runs.append(ParameterRun(f"parameters-{index}", normalized, report))
        except Exception as exc:
            runs.append(ParameterRun(f"parameters-{index}", normalized, None, str(exc)))
    return ParameterSweepReport(name, symbol, timeframe, exchange, tuple(runs))
