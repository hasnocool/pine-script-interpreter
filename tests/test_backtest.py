from datetime import UTC, datetime, timedelta

import pytest

from pine_interpreter import (
    BacktestConfig,
    BacktestEngine,
    BacktestValidationError,
    Candle,
    CCXTDataFeed,
    print_report,
)


def make_candles(prices: list[float]) -> tuple[Candle, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return tuple(
        Candle(
            start + timedelta(days=index),
            price,
            price + 1,
            price - 1,
            price,
            10,
        )
        for index, price in enumerate(prices)
    )


PINE_STRATEGY = """
//@version=6
strategy("SMA validation", overlay=true)
fast = ta.sma(close, 2)
slow = ta.sma(close, 4)
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("Long")
"""


def test_pine_backtest_creates_indexable_printable_report() -> None:
    candles = make_candles([100, 101, 102, 101, 100, 99, 100, 101, 102, 101, 100])
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        PINE_STRATEGY,
        candles,
        name="sma-crossover",
        symbol="BTC/USDT",
        timeframe="1d",
    )

    assert report.name == "sma-crossover"
    assert report.bars == len(candles)
    assert report["trade_count"] == len(report)
    assert report["final_equity"] == report.final_equity
    assert isinstance(report[0].side, str)
    assert "sma-crossover" in str(report)

    print_report(report)


def test_backtest_validation_rejects_empty_or_invalid_input() -> None:
    engine = BacktestEngine()
    with pytest.raises(BacktestValidationError, match="source cannot be empty"):
        engine.validate(" ")
    with pytest.raises(BacktestValidationError, match="at least one candle"):
        engine.validate(PINE_STRATEGY, [])
    with pytest.raises(BacktestValidationError, match="high"):
        Candle(datetime(2024, 1, 1), 100, 99, 98, 100)


def test_ccxt_feed_normalizes_public_ohlcv_rows() -> None:
    class FakeExchange:
        def fetch_ohlcv(
            self,
            symbol: str,
            timeframe: str,
            since: int | None,
            limit: int,
            params: dict[str, object],
        ) -> list[list[float]]:
            assert symbol == "BTC/USDT"
            assert timeframe == "1h"
            assert limit == 2
            assert since is None
            assert params == {}
            return [
                [1_700_000_000_000, 100, 102, 99, 101, 10],
                [1_700_003_600_000, 101, 103, 100, 102, 12],
            ]

    feed = CCXTDataFeed(symbol="BTC/USDT", timeframe="1h", exchange=FakeExchange())
    candles = feed.fetch_public_ohlcv(limit=2)

    assert len(candles) == 2
    assert candles[0].open == 100
    assert candles[1].close == 102


def test_candle_from_ccxt_row_converts_milliseconds() -> None:
    candle = Candle.from_ohlcv([1_700_000_000_000, 1, 2, 0.5, 1.5, 3])

    assert candle.timestamp == datetime.fromtimestamp(1_700_000_000, UTC)
    assert candle.volume == 3
