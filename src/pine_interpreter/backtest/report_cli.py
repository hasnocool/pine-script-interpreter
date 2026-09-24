"""CLI for turning a batch JSON report into plain-English Markdown."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from pine_interpreter.backtest.batch import BatchBacktestReport
from pine_interpreter.backtest.data import load_candles
from pine_interpreter.backtest.markdown import (
    Ranking,
    build_benchmark_markdown,
    write_plain_english_reports,
)
from pine_interpreter.backtest.models import BacktestError
from pine_interpreter.backtest.research import benchmark_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write plain-English Markdown reports from a Pine batch JSON report"
    )
    parser.add_argument("--input", type=Path, required=True, help="Batch JSON report")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--top-count", type=int, default=100)
    parser.add_argument(
        "--ranking",
        choices=("return", "risk-adjusted", "drawdown"),
        default="return",
        help="Ranking used for the top-strategy table",
    )
    parser.add_argument("--min-trades", type=int, default=1)
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Optional candle cache used to add the exact date range",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        report = BatchBacktestReport.from_json(args.input)
        candle_start = None
        candle_end = None
        candles = None
        if args.cache is not None:
            candles = load_candles(args.cache)
            if candles:
                candle_start = min(candle.timestamp for candle in candles)
                candle_end = max(candle.timestamp for candle in candles)
        top_path, overall_path = write_plain_english_reports(
            report,
            args.output_dir,
            top_count=args.top_count,
            ranking=cast(Ranking, args.ranking),
            min_trades=args.min_trades,
            candle_start=candle_start,
            candle_end=candle_end,
        )
        if candles:
            try:
                benchmarks = benchmark_reports(
                    candles,
                    config=report.config,
                    symbol=report.symbol,
                    timeframe=report.timeframe,
                    exchange=report.exchange,
                )
                with overall_path.open("a", encoding="utf-8") as handle:
                    handle.write("\n" + build_benchmark_markdown(benchmarks))
            except (BacktestError, ValueError):
                # A short or unusual candle window should not prevent the main
                # batch report from being rendered.
                pass
    except (OSError, BacktestError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Top strategies report: {top_path}")
    print(f"Overall report: {overall_path}")


if __name__ == "__main__":
    main()
