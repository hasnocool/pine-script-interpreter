"""Batch Pine backtests with a progress bar, ETA, and indexed baseline report."""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, overload

from pine_interpreter.backtest.engine import BacktestEngine
from pine_interpreter.backtest.models import BacktestConfig, BacktestValidationError, Candle
from pine_interpreter.diagnostics import PineRuntimeError, PineSyntaxError


@dataclass(frozen=True, slots=True)
class StrategyResult:
    """Normalized outcome for one Pine strategy file."""

    path: str
    name: str
    status: str
    trades: int = 0
    final_equity: float | None = None
    total_return_pct: float | None = None
    max_drawdown_pct: float | None = None
    elapsed_seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BatchBacktestReport:
    """Indexed collection of strategy outcomes and run metadata."""

    exchange: str
    symbol: str
    timeframe: str
    started_at: datetime
    finished_at: datetime
    candles: int
    results: tuple[StrategyResult, ...]

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, (self.finished_at - self.started_at).total_seconds())

    @property
    def summary(self) -> dict[str, int | float | str]:
        statuses: dict[str, int] = {}
        for result in self.results:
            statuses[result.status] = statuses.get(result.status, 0) + 1
        return {
            "total": len(self.results),
            "candles": self.candles,
            "elapsed_seconds": self.elapsed_seconds,
            "backtested": statuses.get("backtested", 0),
            "no_orders": statuses.get("no_orders", 0),
            "parse_errors": statuses.get("parse_error", 0),
            "validation_errors": statuses.get("validation_error", 0),
            "runtime_errors": statuses.get("runtime_error", 0) + statuses.get("worker_error", 0),
            "io_errors": statuses.get("io_error", 0),
            **{f"status_{key}": value for key, value in sorted(statuses.items())},
        }

    @property
    def by_name(self) -> dict[str, StrategyResult]:
        return {result.name: result for result in self.results}

    @property
    def by_path(self) -> dict[str, StrategyResult]:
        return {result.path: result for result in self.results}

    @property
    def index(self) -> dict[str, str]:
        """Map stable result names and paths to their source path."""

        return {
            **{result.name: result.path for result in self.results},
            **{result.path: result.path for result in self.results},
        }

    @property
    def successful(self) -> tuple[StrategyResult, ...]:
        return tuple(
            result for result in self.results if result.status in {"backtested", "no_orders"}
        )

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self) -> Iterator[StrategyResult]:
        return iter(self.results)

    @overload
    def __getitem__(self, key: int) -> StrategyResult: ...

    @overload
    def __getitem__(self, key: str) -> object: ...

    def __getitem__(self, key: int | str) -> StrategyResult | object:
        if isinstance(key, int):
            return self.results[key]
        if key == "summary":
            return self.summary
        if key == "results":
            return self.results
        if key == "index":
            return self.index
        if key == "symbol":
            return self.symbol
        if key == "timeframe":
            return self.timeframe
        if key == "exchange":
            return self.exchange
        if key == "candles":
            return self.candles
        result = self.by_name.get(key) or self.by_path.get(key)
        if result is not None:
            return result
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": {
                "exchange": self.exchange,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat(),
                "candles": self.candles,
            },
            "summary": self.summary,
            "index": self.index,
            "results": [result.to_dict() for result in self.results],
        }

    def write_json(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return destination

    def write_csv(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fields = list(StrategyResult.__dataclass_fields__)
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(result.to_dict() for result in self.results)
        return destination

    def print(self) -> None:
        print(
            f"Baseline: {self.exchange}:{self.symbol}:{self.timeframe} | "
            f"{len(self.results)} strategies | {self.candles} candles"
        )
        print(
            f"Completed in {self.elapsed_seconds:.1f}s | "
            f"backtested={self.summary['backtested']} | "
            f"no_orders={self.summary['no_orders']} | "
            f"errors={len(self.results) - len(self.successful)}"
        )


_WORKER_CANDLES: tuple[Candle, ...] = ()
_WORKER_CONFIG: BacktestConfig = BacktestConfig()


def _initialize_worker(candles: tuple[Candle, ...], config: BacktestConfig) -> None:
    global _WORKER_CANDLES, _WORKER_CONFIG
    _WORKER_CANDLES = candles
    _WORKER_CONFIG = config


def _run_strategy(path_text: str) -> StrategyResult:
    path = Path(path_text)
    started = time.perf_counter()
    name = path.stem
    try:
        source = path.read_text(encoding="utf-8")
        report = BacktestEngine(_WORKER_CONFIG).run(
            source,
            _WORKER_CANDLES,
            name=name,
            symbol="batch",
            timeframe="batch",
            exchange="batch",
        )
        metrics = report.metrics
        final_equity = metrics["final_equity"]
        total_return = metrics["total_return_pct"]
        drawdown = metrics["max_drawdown_pct"]
        return StrategyResult(
            str(path),
            name,
            "backtested" if report.trades else "no_orders",
            len(report),
            float(final_equity) if final_equity is not None else None,
            float(total_return) if total_return is not None else None,
            float(drawdown) if drawdown is not None else None,
            time.perf_counter() - started,
        )
    except FileNotFoundError as exc:
        return _failed_result(path, name, "io_error", started, exc)
    except PineSyntaxError as exc:
        return _failed_result(path, name, "parse_error", started, exc)
    except BacktestValidationError as exc:
        return _failed_result(path, name, "validation_error", started, exc)
    except PineRuntimeError as exc:
        return _failed_result(path, name, "runtime_error", started, exc)
    except Exception as exc:  # defensive: one bad script must not stop a batch
        return _failed_result(path, name, "runtime_error", started, exc)


def _failed_result(
    path: Path,
    name: str,
    status: str,
    started: float,
    error: BaseException,
) -> StrategyResult:
    return StrategyResult(
        str(path),
        name,
        status,
        elapsed_seconds=time.perf_counter() - started,
        error=str(error),
    )


class _Progress:
    def __init__(self, total: int, enabled: bool) -> None:
        self.total = total
        self.enabled = enabled
        self.completed = 0
        self.started = time.perf_counter()

    def update(self) -> None:
        self.completed += 1
        if not self.enabled:
            return
        elapsed = max(time.perf_counter() - self.started, 1e-9)
        rate = self.completed / elapsed
        remaining = max(0, self.total - self.completed)
        eta = remaining / rate if rate > 0 else float("inf")
        print(
            f"\rBatch backtest {self.completed}/{self.total} "
            f"({self.completed / self.total:6.2%}) | {rate:6.2f} strategies/s | "
            f"ETA {_format_duration(eta)}",
            end="",
            file=sys.stderr,
            flush=True,
        )

    def close(self) -> None:
        if self.enabled:
            print(file=sys.stderr)


def _format_duration(seconds: float) -> str:
    if seconds == float("inf"):
        return "--"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def discover_strategy_files(root: str | Path) -> tuple[Path, ...]:
    """Return sorted Pine files from an archive's ``Strategies`` directory."""

    strategy_dir = Path(root) / "Strategies"
    if not strategy_dir.is_dir():
        raise BacktestValidationError(f"strategy directory does not exist: {strategy_dir}")
    return tuple(sorted(strategy_dir.glob("*.pine")))


def run_strategy_batch(
    paths: Iterable[str | Path],
    candles: Sequence[Candle],
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    config: BacktestConfig | None = None,
    max_workers: int | None = None,
    show_progress: bool = True,
) -> BatchBacktestReport:
    """Run many independent strategies in parallel and return an indexed report."""

    path_list = tuple(Path(path) for path in paths)
    if not path_list:
        raise BacktestValidationError("no strategy files were provided")
    if not candles:
        raise BacktestValidationError("at least one candle is required")
    batch_config = config or BacktestConfig()
    workers = max_workers or min(8, os.cpu_count() or 1)
    workers = max(1, workers)
    started_at = datetime.now(UTC)
    progress = _Progress(len(path_list), show_progress)
    results: list[StrategyResult | None] = [None] * len(path_list)
    if workers == 1:
        _initialize_worker(tuple(candles), batch_config)
        for index, path in enumerate(path_list):
            results[index] = _run_strategy(str(path))
            progress.update()
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_initialize_worker,
            initargs=(tuple(candles), batch_config),
        ) as executor:
            futures = {
                executor.submit(_run_strategy, str(path)): index
                for index, path in enumerate(path_list)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    path = path_list[index]
                    results[index] = _failed_result(
                        path, path.stem, "worker_error", progress.started, exc
                    )
                progress.update()
    progress.close()
    finished_at = datetime.now(UTC)
    return BatchBacktestReport(
        exchange,
        symbol,
        timeframe,
        started_at,
        finished_at,
        len(candles),
        tuple(result for result in results if result is not None),
    )
