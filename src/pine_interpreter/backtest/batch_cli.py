"""CLI for running the full strategy baseline."""

from __future__ import annotations

import argparse
from pathlib import Path

from pine_interpreter.backtest.batch import (
    discover_strategy_files,
    run_strategy_batch,
    validate_strategy_batch,
)
from pine_interpreter.backtest.data import (
    CandleSnapshot,
    CCXTDataFeed,
    candle_quality_warnings,
    load_snapshot,
    save_snapshot,
)
from pine_interpreter.backtest.models import BacktestConfig, BacktestError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest every Pine file in an archive Strategies directory"
    )
    parser.add_argument("--root", type=Path, required=True, help="PineScripts_All archive root")
    parser.add_argument(
        "--libraries",
        type=Path,
        default=None,
        help="Optional Pine library directory (defaults to ROOT/Libraries)",
    )
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--cash", type=float, default=10_000)
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=0)
    parser.add_argument("--spread-bps", type=float, default=0)
    parser.add_argument("--funding-rate-bps", type=float, default=0)
    parser.add_argument("--commission-per-trade", type=float, default=0)
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--pyramiding", type=int, default=1)
    parser.add_argument("--long-only", action="store_true", help="Reject short entries")
    parser.add_argument(
        "--intrabar-policy",
        choices=("stop_first", "limit_first", "open_first"),
        default="stop_first",
        help="Policy when a candle touches both a stop and limit",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=250_000,
        help="Maximum runtime steps per strategy before recording an execution limit",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Maximum wall-clock seconds per strategy",
    )
    parser.add_argument("--output", type=Path, default=Path("backtest-baseline.json"))
    parser.add_argument("--csv", type=Path, default=None, help="Optional CSV results path")
    parser.add_argument("--cache", type=Path, default=None, help="Optional candle cache path")
    parser.add_argument("--refresh", action="store_true", help="Refetch when a cache exists")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Parse and validate sources without fetching data or running orders",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        paths = discover_strategy_files(args.root)
        if args.validate_only:
            report = validate_strategy_batch(
                paths,
                max_workers=args.workers,
                show_progress=not args.no_progress,
            )
        else:
            cache_needs_save = False
            if args.cache is not None and args.cache.exists() and not args.refresh:
                snapshot = load_snapshot(args.cache)
                cache_needs_save = snapshot.source == "legacy-list-cache"
            else:
                snapshot = CCXTDataFeed(args.exchange, args.symbol, args.timeframe).fetch_snapshot(
                    limit=args.limit
                )
                if args.cache is not None:
                    save_snapshot(args.cache, snapshot)
            if (
                snapshot.exchange != args.exchange
                or snapshot.symbol != args.symbol
                or snapshot.timeframe != args.timeframe
            ):
                snapshot = CandleSnapshot(
                    args.exchange,
                    args.symbol,
                    args.timeframe,
                    snapshot.candles,
                    snapshot.fetched_at,
                    "converted-legacy-list-cache"
                    if snapshot.source == "legacy-list-cache"
                    else snapshot.source,
                    candle_quality_warnings(snapshot.candles, args.timeframe),
                )
                cache_needs_save = True
            if args.cache is not None and cache_needs_save:
                save_snapshot(args.cache, snapshot)
            report = run_strategy_batch(
                paths,
                snapshot.candles,
                exchange=args.exchange,
                symbol=args.symbol,
                timeframe=args.timeframe,
                config=BacktestConfig(
                    initial_cash=args.cash,
                    fee_rate=args.fee,
                    slippage_bps=args.slippage_bps,
                    spread_bps=args.spread_bps,
                    funding_rate_bps=args.funding_rate_bps,
                    commission_per_trade=args.commission_per_trade,
                    default_qty=args.qty,
                    pyramiding=args.pyramiding,
                    allow_short=not args.long_only,
                    max_execution_steps=args.max_steps,
                    max_execution_seconds=args.timeout_seconds,
                    intrabar_policy=args.intrabar_policy,
                ),
                max_workers=args.workers,
                show_progress=not args.no_progress,
                data_snapshot=snapshot,
                library_root=(
                    args.libraries
                    if args.libraries is not None
                    else (args.root / "Libraries" if (args.root / "Libraries").is_dir() else None)
                ),
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
