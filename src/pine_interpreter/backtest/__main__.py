"""Command-line entry point for quick public-data Pine backtests."""

from __future__ import annotations

import argparse
from pathlib import Path

from pine_interpreter.backtest.data import CCXTDataFeed
from pine_interpreter.backtest.engine import BacktestEngine
from pine_interpreter.backtest.models import BacktestConfig, BacktestError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backtest Pine source on public CCXT candles")
    parser.add_argument("source", type=Path, help="Pine source file")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--cash", type=float, default=10_000)
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage-bps", type=float, default=0)
    parser.add_argument("--qty", type=float, default=1.0)
    parser.add_argument("--name", default="pine-strategy")
    parser.add_argument(
        "--keep-position",
        action="store_true",
        help="Do not close an open final position",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        candles = CCXTDataFeed(args.exchange, args.symbol, args.timeframe).fetch(
            limit=args.limit
        )
        report = BacktestEngine(
            BacktestConfig(
                initial_cash=args.cash,
                fee_rate=args.fee,
                slippage_bps=args.slippage_bps,
                default_qty=args.qty,
                close_at_end=not args.keep_position,
            )
        ).run(
            args.source.read_text(encoding="utf-8"),
            candles,
            name=args.name,
            symbol=args.symbol,
            timeframe=args.timeframe,
            exchange=args.exchange,
        )
    except (OSError, BacktestError) as exc:
        raise SystemExit(str(exc)) from exc
    report.print()


if __name__ == "__main__":
    main()
