"""SQLite index for searching large batch backtest reports."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pine_interpreter.backtest.batch import BatchBacktestReport, StrategyResult


class BatchResultIndex:
    """Persist and query normalized strategy results without extra services."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def __enter__(self) -> BatchResultIndex:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS batch_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_results (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_hash TEXT,
                library_dependencies TEXT,
                status TEXT NOT NULL,
                error_category TEXT,
                error TEXT,
                trades INTEGER NOT NULL,
                final_equity REAL,
                total_return_pct REAL,
                max_drawdown_pct REAL,
                elapsed_seconds REAL NOT NULL,
                execution_steps INTEGER,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS strategy_results_status
                ON strategy_results(status);
            CREATE INDEX IF NOT EXISTS strategy_results_return
                ON strategy_results(total_return_pct);
            CREATE INDEX IF NOT EXISTS strategy_results_drawdown
                ON strategy_results(max_drawdown_pct);
            CREATE INDEX IF NOT EXISTS strategy_results_name
                ON strategy_results(name);
            """
        )
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(strategy_results)")
        }
        if "library_dependencies" not in columns:
            self.connection.execute(
                "ALTER TABLE strategy_results ADD COLUMN library_dependencies TEXT"
            )
        self.connection.commit()

    def replace_report(self, report: BatchBacktestReport) -> None:
        """Replace the indexed rows with one complete batch report."""

        with self.connection:
            self.connection.execute("DELETE FROM strategy_results")
            self.connection.execute("DELETE FROM batch_metadata")
            metadata = {
                "exchange": report.exchange,
                "symbol": report.symbol,
                "timeframe": report.timeframe,
                "candles": report.candles,
                "data_hash": report.data_hash,
                "data_start": report.data_start.isoformat() if report.data_start else None,
                "data_end": report.data_end.isoformat() if report.data_end else None,
                "data_warnings": list(report.data_warnings),
                "data_source": report.data_source,
                "data_fetched_at": report.data_fetched_at.isoformat()
                if report.data_fetched_at
                else None,
                "runtime_version": report.runtime_version,
                "config": asdict(report.config) if report.config is not None else None,
                "started_at": report.started_at.isoformat(),
                "finished_at": report.finished_at.isoformat(),
                "elapsed_seconds": report.elapsed_seconds,
            }
            self.connection.executemany(
                "INSERT INTO batch_metadata(key, value) VALUES (?, ?)",
                [(key, json.dumps(value)) for key, value in metadata.items()],
            )
            self.connection.executemany(
                """
                INSERT INTO strategy_results(
                    path, name, source_hash, library_dependencies, status, error_category, error,
                    trades, final_equity, total_return_pct, max_drawdown_pct,
                    elapsed_seconds, execution_steps, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self._result_row(result) for result in report.results),
            )

    @staticmethod
    def _result_row(result: StrategyResult) -> tuple[Any, ...]:
        return (
            result.path,
            result.name,
            result.source_hash,
            json.dumps(list(result.library_dependencies)),
            result.status,
            result.error_category,
            result.error,
            result.trades,
            result.final_equity,
            result.total_return_pct,
            result.max_drawdown_pct,
            result.elapsed_seconds,
            result.execution_steps,
            json.dumps(result.to_dict(), sort_keys=True),
        )

    def query(
        self,
        *,
        search: str | None = None,
        status: str | None = None,
        error_category: str | None = None,
        library: str | None = None,
        min_return_pct: float | None = None,
        max_drawdown_pct: float | None = None,
        sort_by: str = "return",
        descending: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        """Return matching result dictionaries ordered by return descending."""

        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset cannot be negative")
        clauses: list[str] = []
        parameters: list[Any] = []
        if search:
            clauses.append(
                "(name LIKE ? OR path LIKE ? OR source_hash LIKE ? OR library_dependencies LIKE ?)"
            )
            pattern = f"%{search}%"
            parameters.extend((pattern, pattern, pattern, pattern))
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if error_category:
            clauses.append("error_category = ?")
            parameters.append(error_category)
        if library:
            clauses.append("library_dependencies LIKE ?")
            parameters.append(f"%{library}%")
        if min_return_pct is not None:
            clauses.append("total_return_pct >= ?")
            parameters.append(min_return_pct)
        if max_drawdown_pct is not None:
            clauses.append("max_drawdown_pct <= ?")
            parameters.append(max_drawdown_pct)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sort_columns = {
            "return": "total_return_pct",
            "drawdown": "max_drawdown_pct",
            "trades": "trades",
            "runtime": "elapsed_seconds",
            "name": "name COLLATE NOCASE",
        }
        if sort_by not in sort_columns:
            raise ValueError(f"unknown sort field: {sort_by}")
        direction = "DESC" if descending else "ASC"
        order = f"{sort_columns[sort_by]} {direction}, name COLLATE NOCASE ASC"
        parameters.extend((limit, offset))
        rows = self.connection.execute(
            f"""
            SELECT path, name, source_hash, library_dependencies, status,
                   error_category, error, trades,
                   final_equity, total_return_pct, max_drawdown_pct,
                   elapsed_seconds, execution_steps, result_json
            FROM strategy_results
            {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
        return tuple(self._row_to_dict(row) for row in rows)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        raw_dependencies = result.get("library_dependencies")
        if isinstance(raw_dependencies, str):
            try:
                result["library_dependencies"] = json.loads(raw_dependencies)
            except json.JSONDecodeError:
                result["library_dependencies"] = []
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def count(self, *, status: str | None = None) -> int:
        if status is None:
            row = self.connection.execute("SELECT COUNT(*) FROM strategy_results").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM strategy_results WHERE status = ?", (status,)
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        self.connection.close()


def build_result_index(
    report: BatchBacktestReport,
    path: str | Path,
) -> BatchResultIndex:
    """Create an index and populate it from a batch report."""

    index = BatchResultIndex(path)
    index.replace_report(report)
    return index
