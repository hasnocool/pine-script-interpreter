"""Run a small Pine backtest without network access."""

from datetime import UTC, datetime, timedelta

from pine_interpreter import BacktestConfig, BacktestEngine, Candle

SOURCE = """
//@version=6
strategy("SMA crossover", overlay=true)
fast = ta.sma(close, 2)
slow = ta.sma(close, 4)
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("Long")
"""


def main() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    prices = [100, 101, 102, 101, 100, 99, 100, 101, 102, 101, 100]
    candles = tuple(
        Candle(start + timedelta(days=index), price, price + 1, price - 1, price)
        for index, price in enumerate(prices)
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        SOURCE,
        candles,
        name="sma-crossover",
        symbol="SYNTH/USDT",
        timeframe="1d",
    )
    report.print()
    print("Return:", report["total_return_pct"])
    print("Trades:", len(report))


if __name__ == "__main__":
    main()
