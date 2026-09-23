"""CLI for running the full strategy baseline."""

from __future__ import annotations

import argparse
from pathlib import Path

from pine_interpreter.backtest.batch import discover_strategy_files, run_strategy_batch
from pine_interpreter.backtest.data import CCXTDataFeed, load_candles, save_candles
from pine_interpreter.backtest.models import BacktestConfig, BacktestError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest every Pine file in an archive Strategies directory"
    )
    parser.add_argument("--root", type=Path, required=True, help="PineScripts_All archive root")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--cash", type=float, default=10_000)
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=0)
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--long-only", action="store_true", help="Reject short entries")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=250_000,
        help="Maximum runtime steps per strategy before recording an execution limit",
    )
    parser.add_argument("--output", type=Path, default=Path("backtest-baseline.json"))
    parser.add_argument("--csv", type=Path, default=None, help="Optional CSV results path")
    parser.add_argument("--cache", type=Path, default=None, help="Optional candle cache path")
    parser.add_argument("--refresh", action="store_true", help="Refetch when a cache exists")
    parser.add_argument("--no-progress", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.cache is not None and args.cache.exists() and not args.refresh:
            candles = load_candles(args.cache)
        else:
            candles = CCXTDataFeed(args.exchange, args.symbol, args.timeframe).fetch(
                limit=args.limit
            )
            if args.cache is not None:
                save_candles(args.cache, candles)
        paths = discover_strategy_files(args.root)
        report = run_strategy_batch(
            paths,
            candles,
            exchange=args.exchange,
            symbol=args.symbol,
            timeframe=args.timeframe,
            config=BacktestConfig(
                initial_cash=args.cash,
                fee_rate=args.fee,
                slippage_bps=args.slippage_bps,
                default_qty=args.qty,
                allow_short=not args.long_only,
                max_execution_steps=args.max_steps,
            ),
            max_workers=args.workers,
            show_progress=not args.no_progress,
        )
        report.write_json(args.output)
        if args.csv is not None:
            report.write_csv(args.csv)
    except (OSError, BacktestError) as exc:
        raise SystemExit(str(exc)) from exc
    report.print()
    print(f"JSON report: {args.output}")
    if args.csv is not None:
        print(f"CSV report: {args.csv}")


if __name__ == "__main__":
    main()
