"""Batch Pine backtests with a progress bar, ETA, and indexed baseline report."""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, overload

from pine_interpreter.backtest.data import CandleSnapshot
from pine_interpreter.backtest.engine import BacktestEngine
from pine_interpreter.backtest.errors import classify_error, error_location, source_digest
from pine_interpreter.backtest.models import (
    BACKTEST_RUNTIME_VERSION,
    BacktestConfig,
    BacktestExecutionLimitError,
    BacktestExecutionTimeoutError,
    BacktestReport,
    BacktestValidationError,
    Candle,
)
from pine_interpreter.backtest.semantic import analyze_source
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
    error_category: str | None = None
    error_location: str | None = None
    source_hash: str | None = None
    execution_steps: int | None = None
    features: tuple[str, ...] = ()
    validation_issues: tuple[str, ...] = ()
    approximations: tuple[str, ...] = ()
    library_dependencies: tuple[str, ...] = ()
    partial_report: BacktestReport | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["partial_report"] = (
            self.partial_report.to_dict() if self.partial_report is not None else None
        )
        return result


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
    config: BacktestConfig | None = None
    data_hash: str | None = None
    data_start: datetime | None = None
    data_end: datetime | None = None
    data_warnings: tuple[str, ...] = ()
    runtime_version: str = BACKTEST_RUNTIME_VERSION
    data_source: str | None = None
    data_fetched_at: datetime | None = None

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
            "validated": statuses.get("validated", 0),
            "no_orders": statuses.get("no_orders", 0),
            "parse_errors": statuses.get("parse_error", 0),
            "validation_errors": statuses.get("validation_error", 0),
            "execution_limits": statuses.get("execution_limit", 0),
            "execution_timeouts": statuses.get("execution_timeout", 0),
            "partial_results": sum(result.partial_report is not None for result in self.results),
            "runtime_errors": statuses.get("runtime_error", 0) + statuses.get("worker_error", 0),
            "io_errors": statuses.get("io_error", 0),
            **{f"status_{key}": value for key, value in sorted(statuses.items())},
        }

    @property
    def duplicate_hashes(self) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for result in self.results:
            if result.source_hash:
                grouped.setdefault(result.source_hash, []).append(result.path)
        return {digest: tuple(paths) for digest, paths in grouped.items() if len(paths) > 1}

    @property
    def error_categories(self) -> dict[str, int]:
        categories: dict[str, int] = {}
        for result in self.results:
            if result.error_category:
                categories[result.error_category] = categories.get(result.error_category, 0) + 1
        return categories

    def slowest_results(self, limit: int = 10) -> tuple[StrategyResult, ...]:
        """Return the longest wall-clock strategy executions in stable order."""

        if limit < 1:
            raise BacktestValidationError("limit must be at least one")
        return tuple(
            sorted(
                self.results,
                key=lambda result: (-result.elapsed_seconds, result.name.casefold()),
            )[:limit]
        )

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
            result
            for result in self.results
            if result.status in {"backtested", "no_orders", "validated"}
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
        if key == "error_categories":
            return self.error_categories
        if key == "partial_results":
            return tuple(result for result in self.results if result.partial_report is not None)
        if key == "duplicate_hashes":
            return self.duplicate_hashes
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
        if key == "config":
            return self.config
        if key == "data_hash":
            return self.data_hash
        if key == "data_start":
            return self.data_start
        if key == "data_end":
            return self.data_end
        if key == "data_warnings":
            return self.data_warnings
        if key == "runtime_version":
            return self.runtime_version
        if key == "data_source":
            return self.data_source
        if key == "data_fetched_at":
            return self.data_fetched_at
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
                "data_hash": self.data_hash,
                "data_start": self.data_start.isoformat() if self.data_start else None,
                "data_end": self.data_end.isoformat() if self.data_end else None,
                "data_warnings": list(self.data_warnings),
                "data_source": self.data_source,
                "data_fetched_at": self.data_fetched_at.isoformat()
                if self.data_fetched_at
                else None,
                "runtime_version": self.runtime_version,
            },
            "summary": self.summary,
            "error_categories": self.error_categories,
            "duplicate_hashes": self.duplicate_hashes,
            "index": self.index,
            "config": asdict(self.config) if self.config is not None else None,
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BatchBacktestReport:
        metadata = data.get("metadata")
        if not isinstance(metadata, Mapping):
            raise BacktestValidationError("batch report metadata is missing")
        results_data = data.get("results")
        if not isinstance(results_data, list):
            raise BacktestValidationError("batch report results are missing")
        results: list[StrategyResult] = []
        for row in results_data:
            if not isinstance(row, Mapping):
                raise BacktestValidationError("batch report result must be an object")
            results.append(
                StrategyResult(
                    path=str(row.get("path", "")),
                    name=str(row.get("name", "")),
                    status=str(row.get("status", "runtime_error")),
                    trades=int(row.get("trades", 0)),
                    final_equity=(
                        None if row.get("final_equity") is None else float(row["final_equity"])
                    ),
                    total_return_pct=(
                        None
                        if row.get("total_return_pct") is None
                        else float(row["total_return_pct"])
                    ),
                    max_drawdown_pct=(
                        None
                        if row.get("max_drawdown_pct") is None
                        else float(row["max_drawdown_pct"])
                    ),
                    elapsed_seconds=float(row.get("elapsed_seconds", 0)),
                    error=None if row.get("error") is None else str(row["error"]),
                    error_category=(
                        None if row.get("error_category") is None else str(row["error_category"])
                    ),
                    error_location=(
                        None if row.get("error_location") is None else str(row["error_location"])
                    ),
                    source_hash=None if row.get("source_hash") is None else str(row["source_hash"]),
                    execution_steps=(
                        None if row.get("execution_steps") is None else int(row["execution_steps"])
                    ),
                    features=tuple(str(item) for item in row.get("features", [])),
                    validation_issues=tuple(str(item) for item in row.get("validation_issues", [])),
                    approximations=tuple(str(item) for item in row.get("approximations", [])),
                    library_dependencies=tuple(
                        str(item) for item in row.get("library_dependencies", [])
                    ),
                    partial_report=(
                        BacktestReport.from_dict(row["partial_report"])
                        if isinstance(row.get("partial_report"), Mapping)
                        else None
                    ),
                )
            )
        config_data = data.get("config")
        config: BacktestConfig | None = None
        if isinstance(config_data, Mapping):
            try:
                config = BacktestConfig.from_dict(config_data)
            except BacktestValidationError as exc:
                raise BacktestValidationError(f"invalid batch report config: {exc}") from exc
        try:
            started_at = datetime.fromisoformat(str(metadata["started_at"]))
            finished_at = datetime.fromisoformat(str(metadata["finished_at"]))
            data_start_value = metadata.get("data_start")
            data_end_value = metadata.get("data_end")
            data_start = datetime.fromisoformat(str(data_start_value)) if data_start_value else None
            data_end = datetime.fromisoformat(str(data_end_value)) if data_end_value else None
            warnings_value = metadata.get("data_warnings", [])
            data_warnings = (
                tuple(str(item) for item in warnings_value)
                if isinstance(warnings_value, list)
                else ()
            )
            fetched_value = metadata.get("data_fetched_at")
            data_fetched_at = datetime.fromisoformat(str(fetched_value)) if fetched_value else None
            if data_fetched_at is not None and data_fetched_at.tzinfo is None:
                data_fetched_at = data_fetched_at.replace(tzinfo=UTC)
            return cls(
                str(metadata["exchange"]),
                str(metadata["symbol"]),
                str(metadata["timeframe"]),
                started_at,
                finished_at,
                int(metadata["candles"]),
                tuple(results),
                config,
                None if metadata.get("data_hash") is None else str(metadata["data_hash"]),
                data_start,
                data_end,
                data_warnings,
                str(metadata.get("runtime_version", BACKTEST_RUNTIME_VERSION)),
                None if metadata.get("data_source") is None else str(metadata["data_source"]),
                data_fetched_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BacktestValidationError(f"invalid batch report metadata: {exc}") from exc

    @classmethod
    def from_json(cls, path: str | Path) -> BatchBacktestReport:
        source = Path(path)
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BacktestValidationError(f"could not load batch report {source}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise BacktestValidationError("batch report must contain a JSON object")
        return cls.from_dict(data)

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
            for result in self.results:
                row = result.to_dict()
                writer.writerow(
                    {
                        key: json.dumps(value, separators=(",", ":"))
                        if isinstance(value, (tuple, list, dict))
                        else value
                        for key, value in row.items()
                    }
                )
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
_WORKER_LIBRARY_ROOT: Path | None = None
_WORKER_EXCHANGE = "synthetic"
_WORKER_SYMBOL = "UNKNOWN"
_WORKER_TIMEFRAME = "unknown"


def _initialize_worker(
    candles: tuple[Candle, ...],
    config: BacktestConfig,
    library_root: Path | None = None,
    exchange: str = "synthetic",
    symbol: str = "UNKNOWN",
    timeframe: str = "unknown",
) -> None:
    global _WORKER_CANDLES, _WORKER_CONFIG, _WORKER_LIBRARY_ROOT
    global _WORKER_EXCHANGE, _WORKER_SYMBOL, _WORKER_TIMEFRAME
    _WORKER_CANDLES = candles
    _WORKER_CONFIG = config
    _WORKER_LIBRARY_ROOT = library_root
    _WORKER_EXCHANGE = exchange
    _WORKER_SYMBOL = symbol
    _WORKER_TIMEFRAME = timeframe


def _run_strategy(path_text: str) -> StrategyResult:
    path = Path(path_text)
    started = time.perf_counter()
    name = path.stem
    source_hash: str | None = None
    features: tuple[str, ...] = ()
    validation_issues: tuple[str, ...] = ()
    try:
        source = path.read_text(encoding="utf-8")
        source_hash = source_digest(source)
        analysis = analyze_source(source)
        features = analysis.features
        validation_issues = tuple(issue.code for issue in analysis.issues)
        report = BacktestEngine(
            _WORKER_CONFIG,
            library_root=_WORKER_LIBRARY_ROOT,
        ).run(
            source,
            _WORKER_CANDLES,
            name=name,
            symbol=_WORKER_SYMBOL,
            timeframe=_WORKER_TIMEFRAME,
            exchange=_WORKER_EXCHANGE,
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
            source_hash=source_hash,
            execution_steps=report.execution_steps,
            features=features,
            validation_issues=validation_issues,
            approximations=report.approximations,
            library_dependencies=report.library_dependencies,
        )
    except FileNotFoundError as exc:
        return _failed_result(
            path,
            name,
            "io_error",
            started,
            exc,
            source_hash=source_hash,
            features=features,
            validation_issues=validation_issues,
        )
    except PineSyntaxError as exc:
        return _failed_result(
            path,
            name,
            "parse_error",
            started,
            exc,
            source_hash=source_hash,
            features=features,
            validation_issues=validation_issues,
        )
    except BacktestExecutionLimitError as exc:
        return _failed_result(
            path,
            name,
            "execution_limit",
            started,
            exc,
            source_hash=source_hash,
            features=features,
            validation_issues=validation_issues,
        )
    except BacktestExecutionTimeoutError as exc:
        return _failed_result(
            path,
            name,
            "execution_timeout",
            started,
            exc,
            source_hash=source_hash,
            features=features,
            validation_issues=validation_issues,
        )
    except BacktestValidationError as exc:
        return _failed_result(
            path,
            name,
            "validation_error",
            started,
            exc,
            source_hash=source_hash,
            features=features,
            validation_issues=validation_issues,
        )
    except PineRuntimeError as exc:
        return _failed_result(
            path,
            name,
            "runtime_error",
            started,
            exc,
            source_hash=source_hash,
            features=features,
            validation_issues=validation_issues,
        )
    except Exception as exc:  # defensive: one bad script must not stop a batch
        return _failed_result(
            path,
            name,
            "runtime_error",
            started,
            exc,
            source_hash=source_hash,
            features=features,
            validation_issues=validation_issues,
        )


def _validate_strategy(path_text: str) -> StrategyResult:
    path = Path(path_text)
    started = time.perf_counter()
    name = path.stem
    source_hash: str | None = None
    try:
        source = path.read_text(encoding="utf-8")
        source_hash = source_digest(source)
        analysis = analyze_source(source)
        issue_codes = tuple(issue.code for issue in analysis.issues)
        if not analysis.valid:
            issue = analysis.errors[0]
            status = "parse_error" if issue.code == "parse_error" else "validation_error"
            error = BacktestValidationError(issue.message, location=issue.location)
            return _failed_result(
                path,
                name,
                status,
                started,
                error,
                source_hash=source_hash,
                features=analysis.features,
                validation_issues=issue_codes,
                category_override="parse_error" if status == "parse_error" else None,
            )
        return StrategyResult(
            str(path),
            name,
            "validated",
            elapsed_seconds=time.perf_counter() - started,
            source_hash=source_hash,
            features=analysis.features,
            validation_issues=issue_codes,
        )
    except FileNotFoundError as exc:
        return _failed_result(path, name, "io_error", started, exc, source_hash=source_hash)
    except PineSyntaxError as exc:
        return _failed_result(path, name, "parse_error", started, exc, source_hash=source_hash)
    except BacktestValidationError as exc:
        return _failed_result(path, name, "validation_error", started, exc, source_hash=source_hash)
    except Exception as exc:
        return _failed_result(path, name, "runtime_error", started, exc, source_hash=source_hash)


def validate_strategy_batch(
    paths: Iterable[str | Path],
    *,
    max_workers: int | None = None,
    show_progress: bool = True,
) -> BatchBacktestReport:
    """Parse and validate many scripts without fetching candles or executing orders."""

    path_list = tuple(Path(path) for path in paths)
    if not path_list:
        raise BacktestValidationError("no strategy files were provided")
    if max_workers is not None and (
        not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1
    ):
        raise BacktestValidationError("max_workers must be a positive integer")
    workers = max_workers or min(8, os.cpu_count() or 1)
    started_at = datetime.now(UTC)
    progress = _Progress(len(path_list), show_progress, "Validate")
    results: list[StrategyResult | None] = [None] * len(path_list)
    if workers == 1:
        for index, path in enumerate(path_list):
            results[index] = _validate_strategy(str(path))
            progress.update()
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_validate_strategy, str(path)): index
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
    return BatchBacktestReport(
        exchange="validation",
        symbol="none",
        timeframe="none",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        candles=0,
        results=tuple(result for result in results if result is not None),
    )


def _failed_result(
    path: Path,
    name: str,
    status: str,
    started: float,
    error: BaseException,
    *,
    source_hash: str | None = None,
    features: tuple[str, ...] = (),
    validation_issues: tuple[str, ...] = (),
    category_override: str | None = None,
) -> StrategyResult:
    category = classify_error(error)
    partial_report = getattr(error, "partial_report", None)
    return StrategyResult(
        str(path),
        name,
        status,
        elapsed_seconds=time.perf_counter() - started,
        error=str(error),
        error_category=category_override or category.value,
        error_location=error_location(error),
        source_hash=source_hash,
        execution_steps=(
            partial_report.execution_steps
            if isinstance(partial_report, BacktestReport)
            else None
        ),
        features=features,
        validation_issues=validation_issues,
        approximations=(
            partial_report.approximations
            if isinstance(partial_report, BacktestReport)
            else ()
        ),
        library_dependencies=(
            partial_report.library_dependencies
            if isinstance(partial_report, BacktestReport)
            else ()
        ),
        partial_report=(
            partial_report if isinstance(partial_report, BacktestReport) else None
        ),
    )


class _Progress:
    def __init__(self, total: int, enabled: bool, label: str = "Batch backtest") -> None:
        self.total = total
        self.enabled = enabled
        self.label = label
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
            f"\r{self.label} {self.completed}/{self.total} "
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
    """Return sorted strategy files from an archive root.

    Flat archives use ``ROOT/Strategies``. The maintained download tree stores
    source groups in child directories, so a single populated child
    ``Strategies`` directory (currently TradingView) is accepted as well.
    """

    source_root = Path(root)
    direct = source_root / "Strategies"
    if direct.is_dir():
        return tuple(sorted(direct.glob("*.pine")))
    candidates: list[Path] = []
    if source_root.is_dir():
        candidates = sorted(
            child / "Strategies"
            for child in source_root.iterdir()
            if child.is_dir() and (child / "Strategies").is_dir()
        )
    populated = [directory for directory in candidates if any(directory.glob("*.pine"))]
    if not populated:
        raise BacktestValidationError(f"strategy directory does not exist: {direct}")
    return tuple(sorted(path for directory in populated for path in directory.glob("*.pine")))


def discover_library_root(root: str | Path) -> Path | None:
    """Find the matching library directory for a flat or grouped archive root."""

    source_root = Path(root)
    direct = source_root / "Libraries"
    if direct.is_dir():
        return direct
    if not source_root.is_dir():
        return None
    if source_root.is_dir():
        strategy_sources = [
            child
            for child in source_root.iterdir()
            if child.is_dir()
            and (child / "Strategies").is_dir()
            and any((child / "Strategies").glob("*.pine"))
        ]
        if len(strategy_sources) == 1:
            matching_library = strategy_sources[0] / "Libraries"
            if matching_library.is_dir():
                return matching_library
    candidates = sorted(
        child / "Libraries"
        for child in source_root.iterdir()
        if child.is_dir() and (child / "Libraries").is_dir()
    )
    populated = [directory for directory in candidates if any(directory.rglob("*.pine"))]
    return populated[0] if len(populated) == 1 else None


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
    data_snapshot: CandleSnapshot | None = None,
    library_root: str | Path | None = None,
) -> BatchBacktestReport:
    """Run many independent strategies in parallel and return an indexed report."""

    path_list = tuple(Path(path) for path in paths)
    if not path_list:
        raise BacktestValidationError("no strategy files were provided")
    if not candles:
        raise BacktestValidationError("at least one candle is required")
    batch_config = config or BacktestConfig()
    if max_workers is not None and (
        not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1
    ):
        raise BacktestValidationError("max_workers must be a positive integer")
    workers = max_workers or min(8, os.cpu_count() or 1)
    started_at = datetime.now(UTC)
    progress = _Progress(len(path_list), show_progress)
    results: list[StrategyResult | None] = [None] * len(path_list)
    if workers == 1:
        _initialize_worker(
            tuple(candles),
            batch_config,
            Path(library_root) if library_root else None,
            exchange,
            symbol,
            timeframe,
        )
        for index, path in enumerate(path_list):
            results[index] = _run_strategy(str(path))
            progress.update()
    else:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_initialize_worker,
            initargs=(
                tuple(candles),
                batch_config,
                Path(library_root) if library_root else None,
                exchange,
                symbol,
                timeframe,
            ),
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
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        started_at=started_at,
        finished_at=finished_at,
        candles=len(candles),
        results=tuple(result for result in results if result is not None),
        config=batch_config,
        data_hash=data_snapshot.content_hash if data_snapshot is not None else None,
        data_start=data_snapshot.start if data_snapshot is not None else None,
        data_end=data_snapshot.end if data_snapshot is not None else None,
        data_warnings=data_snapshot.warnings if data_snapshot is not None else (),
        data_source=data_snapshot.source if data_snapshot is not None else None,
        data_fetched_at=data_snapshot.fetched_at if data_snapshot is not None else None,
    )
