"""CLI for out-of-sample and walk-forward Pine research."""

from __future__ import annotations

import argparse
from pathlib import Path

from pine_interpreter.backtest.data import CCXTDataFeed, load_snapshot
from pine_interpreter.backtest.models import BacktestConfig, BacktestError
from pine_interpreter.backtest.research import run_out_of_sample, run_walk_forward


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run walk-forward or out-of-sample Pine research")
    parser.add_argument("--source", type=Path, required=True, help="Pine source file")
    parser.add_argument("--library-root", type=Path, default=None, help="Pine library directory")
    parser.add_argument("--cache", type=Path, default=None, help="Candle snapshot cache")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--mode", choices=("walk-forward", "out-of-sample"), default="walk-forward")
    parser.add_argument("--train-size", type=int, default=250)
    parser.add_argument("--test-size", type=int, default=100)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument("--anchored", action="store_true")
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--periods-per-year", type=int, default=365)
    parser.add_argument("--cash", type=float, default=10_000)
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=0)
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--pyramiding", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=1_000_000)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--intrabar-policy",
        choices=("stop_first", "limit_first", "open_first"),
        default="stop_first",
    )
    parser.add_argument("--output", type=Path, default=Path("research-report.json"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        source = args.source.read_text(encoding="utf-8")
        if args.cache is not None and args.cache.exists():
            snapshot = load_snapshot(args.cache)
        else:
            snapshot = CCXTDataFeed(args.exchange, args.symbol, args.timeframe).fetch_snapshot(
                limit=args.limit
            )
        config = BacktestConfig(
            initial_cash=args.cash,
            fee_rate=args.fee,
            slippage_bps=args.slippage_bps,
            default_qty=args.qty,
            pyramiding=args.pyramiding,
            allow_short=True,
            max_execution_steps=args.max_steps,
            max_execution_seconds=args.timeout_seconds,
            intrabar_policy=args.intrabar_policy,
        )
        if args.mode == "out-of-sample":
            report = run_out_of_sample(
                source,
                snapshot.candles,
                train_ratio=args.train_ratio,
                config=config,
                name=args.source.stem,
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                exchange=snapshot.exchange,
                periods_per_year=args.periods_per_year,
                library_root=args.library_root,
            )
        else:
            report = run_walk_forward(
                source,
                snapshot.candles,
                train_size=args.train_size,
                test_size=args.test_size,
                step=args.step,
                anchored=args.anchored,
                config=config,
                name=args.source.stem,
                symbol=snapshot.symbol,
                timeframe=snapshot.timeframe,
                exchange=snapshot.exchange,
                periods_per_year=args.periods_per_year,
                library_root=args.library_root,
            )
        report.write_json(args.output)
    except (OSError, BacktestError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    report.print()
    print(f"Research report: {args.output}")


if __name__ == "__main__":
    main()
