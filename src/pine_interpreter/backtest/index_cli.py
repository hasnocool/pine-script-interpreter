"""CLI for indexing and searching batch backtest results."""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path

from pine_interpreter.backtest.batch import BatchBacktestReport
from pine_interpreter.backtest.index import BatchResultIndex
from pine_interpreter.backtest.models import BacktestError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or query a SQLite Pine result index")
    parser.add_argument("--input", type=Path, default=None, help="Batch JSON report to index")
    parser.add_argument("--database", type=Path, required=True, help="SQLite database path")
    parser.add_argument("--search", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--error-category", default=None)
    parser.add_argument("--library", default=None, help="Filter by local library dependency")
    parser.add_argument("--min-return", type=float, default=None)
    parser.add_argument("--max-drawdown", type=float, default=None)
    parser.add_argument(
        "--sort-by",
        choices=("return", "drawdown", "trades", "runtime", "name"),
        default="return",
    )
    parser.add_argument("--ascending", action="store_true", help="Sort ascending")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--format", choices=("json", "csv", "markdown"), default="json")
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        with BatchResultIndex(args.database) as index:
            if args.input is not None:
                index.replace_report(BatchBacktestReport.from_json(args.input))
            rows = index.query(
                search=args.search,
                status=args.status,
                error_category=args.error_category,
                library=args.library,
                min_return_pct=args.min_return,
                max_drawdown_pct=args.max_drawdown,
                sort_by=args.sort_by,
                descending=not args.ascending,
                limit=args.limit,
                offset=args.offset,
            )
            if args.format == "json":
                rendered = json.dumps(rows, indent=2)
            elif args.format == "csv":
                buffer = io.StringIO()
                if rows:
                    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
                    writer.writeheader()
                    for row in rows:
                        writer.writerow(
                            {
                                key: json.dumps(value, sort_keys=True)
                                if isinstance(value, (dict, list))
                                else value
                                for key, value in row.items()
                            }
                        )
                rendered = buffer.getvalue()
            else:
                lines = [
                    "# Backtest result explorer",
                    "",
                    "| Name | Status | Trades | Return | Drawdown | Runtime | Source hash |",
                    "| --- | --- | ---: | ---: | ---: | ---: | --- |",
                ]
                for row in rows:
                    lines.append(
                        "| "
                        + " | ".join(
                            (
                                str(row["name"]).replace("|", "\\|"),
                                str(row["status"]),
                                str(row["trades"]),
                                f"{float(row['total_return_pct'] or 0):.2f}%",
                                f"{float(row['max_drawdown_pct'] or 0):.2f}%",
                                f"{float(row['elapsed_seconds']):.3f}s",
                                f"`{row['source_hash'] or ''}`",
                            )
                        )
                        + " |"
                    )
                rendered = "\n".join(lines) + "\n"
            if args.output is None:
                print(rendered, end="" if args.format != "json" else "\n")
            else:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
                print(f"Wrote {args.format} result: {args.output}")
    except (OSError, BacktestError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
