from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from pine_interpreter import (
    BacktestConfig,
    BacktestEngine,
    BacktestExecutionLimitError,
    BacktestValidationError,
    Candle,
    CCXTDataFeed,
    build_overall_markdown,
    build_top_strategies_markdown,
    discover_strategy_files,
    print_report,
    run_strategy_batch,
    write_plain_english_reports,
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


def test_strategy_batch_indexes_results_and_records_failures(tmp_path: Path) -> None:
    strategy_dir = tmp_path / "Strategies"
    strategy_dir.mkdir()
    first = strategy_dir / "alpha.pine"
    second = strategy_dir / "broken.pine"
    first.write_text(PINE_STRATEGY, encoding="utf-8")
    second.write_text("this is not valid Pine ===", encoding="utf-8")

    paths = discover_strategy_files(tmp_path)
    report = run_strategy_batch(
        paths,
        make_candles([100, 101, 102, 101, 100, 99, 100, 101]),
        exchange="synthetic",
        symbol="TEST/USDT",
        timeframe="1d",
        max_workers=1,
        show_progress=False,
    )

    assert len(report) == 2
    alpha = report.by_name["alpha"]
    assert alpha.status in {"backtested", "no_orders"}
    assert report[1].status == "parse_error"
    assert report.summary["total"] == 2
    assert report.index["alpha"].endswith("alpha.pine")

    output = report.write_json(tmp_path / "baseline.json")
    assert output.exists()
    loaded = type(report).from_json(output)
    assert loaded.by_name["alpha"].status == alpha.status
    assert loaded.config == report.config

    top_markdown = build_top_strategies_markdown(report, top_count=1)
    overall_markdown = build_overall_markdown(report, top_count=1)
    assert "# Top 1 Pine Strategies" in top_markdown
    assert "# Overall Pine Backtest Baseline" in overall_markdown
    assert "Results at a glance" in overall_markdown

    top_path, overall_path = write_plain_english_reports(
        report,
        tmp_path / "markdown",
        top_count=1,
    )
    assert top_path.exists()
    assert overall_path.exists()


def test_runtime_step_limit_stops_runaway_strategy() -> None:
    source = dedent(
        """
        //@version=6
        strategy("runaway", overlay=true)
        if bar_index == 0
            while true
                value = close
        """
    )

    with pytest.raises(BacktestExecutionLimitError, match="execution step limit"):
        BacktestEngine(BacktestConfig(max_execution_steps=20)).run(
            source,
            make_candles([100, 101]),
        )
