from datetime import UTC, datetime, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

from pine_interpreter import (
    BacktestConfig,
    BacktestEngine,
    BacktestExecutionLimitError,
    BacktestExecutionTimeoutError,
    BacktestValidationError,
    BatchResultIndex,
    Candle,
    CandleSnapshot,
    CCXTDataFeed,
    analyze_source,
    benchmark_reports,
    build_overall_markdown,
    build_top_strategies_markdown,
    buy_and_hold_report,
    candle_data_hash,
    compare_markets,
    discover_library_root,
    discover_strategy_files,
    load_snapshot,
    print_report,
    risk_metrics,
    run_parameter_sweep,
    run_strategy_batch,
    run_walk_forward,
    save_snapshot,
    split_candles,
    validate_strategy_batch,
    walk_forward_windows,
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


def test_semantic_analysis_invents_features_and_reports_parse_errors() -> None:
    analysis = analyze_source(PINE_STRATEGY)
    invalid = analyze_source("this is not valid Pine ===")

    assert analysis.valid
    assert "ta.sma" in analysis.features
    assert not invalid.valid
    assert invalid.errors[0].code == "parse_error"


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


def test_ccxt_feed_paginates_long_forward_requests() -> None:
    class FakeExchange:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_ohlcv(
            self,
            symbol: str,
            timeframe: str,
            since: int | None,
            limit: int,
            params: dict[str, object],
        ) -> list[list[float]]:
            self.calls += 1
            start = 1_700_000_000_000 if self.calls == 1 else since
            assert start is not None
            return [[start + index * 3_600_000, 100, 101, 99, 100, 1] for index in range(limit)]

    exchange = FakeExchange()
    candles = CCXTDataFeed(exchange=exchange).fetch(limit=1_500, since=1_700_000_000_000)

    assert len(candles) == 1_500
    assert exchange.calls == 2
    assert candles[0].timestamp_ms == 1_700_000_000_000
    assert candles[-1].timestamp_ms > candles[0].timestamp_ms


def test_candle_from_ccxt_row_converts_milliseconds() -> None:
    candle = Candle.from_ohlcv([1_700_000_000_000, 1, 2, 0.5, 1.5, 3])

    assert candle.timestamp == datetime.fromtimestamp(1_700_000_000, UTC)
    assert candle.volume == 3


def test_candle_snapshot_records_hash_and_round_trips(tmp_path: Path) -> None:
    candles = make_candles([100, 101, 102])
    snapshot = CandleSnapshot("synthetic", "TEST/USDT", "1d", candles)
    destination = save_snapshot(tmp_path / "snapshot.json", snapshot)
    loaded = load_snapshot(destination)

    assert loaded.content_hash == candle_data_hash(candles)
    assert loaded.exchange == "synthetic"
    assert loaded.candles == candles
    assert loaded.start == candles[0].timestamp
    assert loaded.end == candles[-1].timestamp
    tampered = destination.read_text(encoding="utf-8").replace(
        '"content_hash": "', '"content_hash": "tampered-'
    )
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(tampered, encoding="utf-8")
    assert any("content hash" in warning for warning in load_snapshot(tampered_path).warnings)


def test_grouped_archive_discovery_finds_populated_strategy_and_library_roots(
    tmp_path: Path,
) -> None:
    (tmp_path / "GitHub" / "Strategies").mkdir(parents=True)
    tradingview = tmp_path / "TradingView"
    strategy_dir = tradingview / "Strategies"
    library_dir = tradingview / "Libraries"
    strategy_dir.mkdir(parents=True)
    library_dir.mkdir(parents=True)
    strategy = strategy_dir / "alpha.pine"
    strategy.write_text(PINE_STRATEGY, encoding="utf-8")
    (library_dir / "helpers.pine").write_text("//@version=6\n", encoding="utf-8")

    assert discover_strategy_files(tmp_path) == (strategy,)
    assert discover_library_root(tmp_path) == library_dir


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
    assert report[1].error_category == "parse_error"
    assert alpha.source_hash
    assert alpha.execution_steps is not None
    assert report.summary["total"] == 2
    assert report.index["alpha"].endswith("alpha.pine")

    with BatchResultIndex(tmp_path / "results.sqlite") as result_index:
        result_index.replace_report(report)
        assert result_index.count() == 2
        assert result_index.query(search="alpha", limit=1)[0]["name"] == "alpha"
        assert result_index.query(search=alpha.source_hash, limit=1)[0]["name"] == "alpha"
        assert result_index.query(sort_by="name", descending=False, limit=1)[0]["name"] == "alpha"

    validation_report = validate_strategy_batch(
        paths,
        max_workers=1,
        show_progress=False,
    )
    assert validation_report.summary["validated"] == 1
    assert validation_report.by_name["broken"].status == "parse_error"

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


def test_batch_propagates_library_roots_and_records_dependencies(tmp_path: Path) -> None:
    strategy_dir = tmp_path / "Strategies"
    library_dir = tmp_path / "Libraries"
    strategy_dir.mkdir()
    library_dir.mkdir()
    (library_dir / "helpers.pine").write_text(
        '//@version=6\nindicator("helpers")\ndouble_value(x) => x * 2\n', encoding="utf-8"
    )
    strategy = strategy_dir / "imports.pine"
    strategy.write_text(
        dedent(
            """
            //@version=6
            strategy("batch import", overlay=true)
            import "helpers" as helpers
            value = helpers.double_value(close)
            if value > 1
                strategy.entry("Long", strategy.long, qty=1)
            """
        ),
        encoding="utf-8",
    )
    report = run_strategy_batch(
        [strategy],
        make_candles([1, 1, 1]),
        exchange="synthetic",
        symbol="TEST/USDT",
        timeframe="1d",
        config=BacktestConfig(close_at_end=True),
        max_workers=1,
        show_progress=False,
        library_root=library_dir,
    )
    result = report.by_name["imports"]
    assert result.library_dependencies == ("helpers",)
    assert result.status == "backtested"
    assert result.features


def test_input_overrides_allow_bounded_parameter_research() -> None:
    source = dedent(
        """
        //@version=6
        strategy("input override", overlay=true)
        length = input.int(2, "Fast length", minval=1)
        average = ta.sma(close, length)
        if ta.crossover(average, close)
            strategy.entry("Long", strategy.long, qty=1)
        if ta.crossunder(average, close)
            strategy.close("Long")
        """
    )
    candles = make_candles([100, 101, 102, 101, 100, 99, 100, 101])
    default = BacktestEngine(BacktestConfig(close_at_end=True)).run(source, candles)
    overridden = BacktestEngine(
        BacktestConfig(close_at_end=True),
        input_overrides={"Fast length": 4},
    ).run(source, candles)

    assert default.bars == overridden.bars == len(candles)
    assert int(default.metrics["execution_steps"] or 0) > 0
    assert int(overridden.metrics["execution_steps"] or 0) > 0
    sweep = run_parameter_sweep(
        source,
        candles,
        ({"Fast length": 2}, {"Fast length": 4}),
        config=BacktestConfig(close_at_end=True),
    )
    assert sweep.summary["successful"] == 2


def test_named_input_defaults_are_available_to_runtime_objects() -> None:
    source = dedent(
        """
        //@version=6
        strategy("named input", overlay=true)
        text = input.text_area(title="Text", defval="a,b")
        parts = str.split(text, ",")
        if bar_index == 0 and parts.size() == 2
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            strategy.close("Long")
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        make_candles([100, 101, 102]),
    )

    assert len(report) == 1


def test_walk_forward_and_risk_metrics_are_reproducible() -> None:
    candles = make_candles([100, 101, 102, 101, 100, 99, 100, 101, 102, 103, 102, 101])
    assert split_candles(candles).test
    windows = walk_forward_windows(candles, train_size=5, test_size=3, step=3)
    report = run_walk_forward(
        PINE_STRATEGY,
        candles,
        windows=windows,
        config=BacktestConfig(close_at_end=True),
        name="walk",
        symbol="TEST/USDT",
        timeframe="1d",
    )

    assert len(windows) == 2
    assert len(report) == 2
    assert report.summary["folds"] == 2
    assert "mean_test_return_pct" in report.summary
    assert report.by_name["fold-1"].test.bars == 3
    risk = risk_metrics(report[0].test, periods_per_year=365)
    assert risk["sharpe"] is not None
    assert float(risk["time_in_market_pct"] or 0) >= 0
    market_report = compare_markets(
        PINE_STRATEGY,
        {
            ("synthetic", "TEST/USDT", "1d"): candles,
            ("synthetic", "TEST/USDT", "1h"): candles,
        },
        config=BacktestConfig(close_at_end=True),
    )
    assert market_report.summary["markets"] == 2
    benchmark = buy_and_hold_report(candles)
    assert benchmark.bars == len(candles)
    assert len(benchmark) == 1
    benchmarks = benchmark_reports(candles, config=BacktestConfig(close_at_end=True))
    assert set(benchmarks) == {"no_trade", "random", "ma_crossover", "buy_and_hold"}
    assert benchmarks["no_trade"].trades == ()
    assert len(benchmarks["random"].trades) >= 1


def test_unknown_builtin_is_explicitly_rejected() -> None:
    source = '//@version=6\nstrategy("unknown")\nvalue = definitely_not_a_pine_builtin(close)\n'
    with pytest.raises(BacktestValidationError, match="unknown Pine builtin"):
        BacktestEngine().run(source, make_candles([1, 2]))


def test_request_security_is_marked_as_an_approximation() -> None:
    source = dedent(
        """
        //@version=6
        strategy("request", overlay=true)
        value = request.security(syminfo.tickerid, "1D", close)
        if value > open
            strategy.entry("Long", strategy.long, qty=1)
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        make_candles([100, 101, 102]),
    )

    assert "request.security" in report.approximations


def test_limit_entry_is_filled_on_a_later_candle() -> None:
    source = dedent(
        """
        //@version=6
        strategy("pending entry", overlay=true)
        if bar_index == 0
            strategy.entry("Long", strategy.long, qty=1, limit=99)
        if bar_index == 2
            strategy.close("Long")
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        (
            Candle(datetime(2024, 1, 1, tzinfo=UTC), 100, 101, 99, 100),
            Candle(datetime(2024, 1, 2, tzinfo=UTC), 100, 102, 98, 101),
            Candle(datetime(2024, 1, 3, tzinfo=UTC), 101, 103, 100, 102),
            Candle(datetime(2024, 1, 4, tzinfo=UTC), 102, 104, 101, 103),
        ),
    )

    assert len(report) == 1
    assert report[0].entry_index == 1
    assert report[0].entry_price == 99


def test_local_library_imports_can_execute_exported_functions(tmp_path: Path) -> None:
    library_root = tmp_path / "Libraries"
    library_root.mkdir()
    (library_root / "helpers.pine").write_text(
        '//@version=6\nindicator("helpers")\ndouble_value(x) => x * 2\n',
        encoding="utf-8",
    )
    source = dedent(
        """
        //@version=6
        strategy("library import", overlay=true)
        import "helpers" as helpers
        value = helpers.double_value(close)
        if value > 1
            strategy.entry("Long", strategy.long, qty=1)
        """
    )
    report = BacktestEngine(
        BacktestConfig(close_at_end=True),
        library_root=library_root,
    ).run(source, make_candles([1, 1, 1]))

    assert len(report) == 1
    assert report[0].entry_price == 1


def test_partial_exit_records_trade_attribution_and_keeps_remainder() -> None:
    source = dedent(
        """
        //@version=6
        strategy("partial exit", overlay=true)
        if bar_index == 0
            strategy.entry("Long", strategy.long, qty=4)
        if bar_index == 1
            strategy.exit("Half", qty=2)
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        make_candles([100, 101, 102]),
    )

    assert len(report) == 2
    assert report[0].quantity == 2
    assert report[1].quantity == 2
    assert report[0].reason == "strategy.exit"
    assert report[1].reason == "close_all"


def test_library_imports_reject_missing_escape_and_circular_paths(tmp_path: Path) -> None:
    library_root = tmp_path / "Libraries"
    library_root.mkdir()
    (library_root / "a.pine").write_text(
        '//@version=6\nindicator("a")\nimport "b" as b\nf() => b.g()\n', encoding="utf-8"
    )
    (library_root / "b.pine").write_text(
        '//@version=6\nindicator("b")\nimport "a" as a\ng() => 1\n', encoding="utf-8"
    )
    circular = '//@version=6\nstrategy("circular")\nimport "a" as a\nx = a.f()\n'
    with pytest.raises(BacktestValidationError, match="circular Pine library import"):
        BacktestEngine(library_root=library_root).run(
            circular, make_candles([1, 1]), name="circular"
        )
    with pytest.raises(BacktestValidationError, match="library_root"):
        BacktestEngine().run(
            '//@version=6\nstrategy("missing")\nimport "missing" as m\nx = m.f()\n',
            make_candles([1, 1]),
        )
    with pytest.raises(BacktestValidationError, match="library"):
        BacktestEngine(library_root=library_root).run(
            '//@version=6\nstrategy("escape")\nimport "../outside" as m\nx = m.f()\n',
            make_candles([1, 1]),
        )


def test_common_extended_ta_indicators_evaluate_without_runtime_errors() -> None:
    source = dedent(
        """
        //@version=6
        strategy("extended indicators", overlay=true)
        momentum_value = ta.momentum(close, 3)
        roc_value = ta.roc(close, 3)
        midpoint_value = ta.midpoint(close, 4)
        range_value = ta.range(high, low)
        bars_value = ta.highestbars(close, 4)
        regression = ta.linreg(close, 4)
        center_of_gravity = ta.cog(close, 4)
        adx_value = ta.adx(14)
        plus_value = ta.plusdi(14)
        minus_value = ta.minusdi(14)
        aroon_value = ta.aroon(14)
        sar_value = ta.sar(0.02, 0.2)
        obv_value = ta.obv
        bands = ta.bb(close, 4, 2)
        anchored = ta.anchored_vwap(close, 0)
        """
    )
    report = BacktestEngine(BacktestConfig()).run(source, make_candles(list(range(100, 140))))
    assert report.bars == 40
    assert "ta.sar" in report.approximations


def test_stop_exit_is_recorded_after_entry() -> None:
    source = dedent(
        """
        //@version=6
        strategy("stop exit", overlay=true)
        if bar_index == 0
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            strategy.exit("Risk", stop=99)
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        (
            Candle(datetime(2024, 1, 1, tzinfo=UTC), 100, 101, 99, 100),
            Candle(datetime(2024, 1, 2, tzinfo=UTC), 100, 100, 98, 99),
            Candle(datetime(2024, 1, 3, tzinfo=UTC), 99, 100, 98, 99),
        ),
    )

    assert len(report) == 1
    assert report[0].reason == "stop"


def test_strategy_declaration_sets_initial_capital_and_pyramiding() -> None:
    source = dedent(
        """
        //@version=6
        strategy("declared settings", overlay=true, initial_capital=1000,
                 pyramiding=2, default_qty_type=strategy.percent_of_equity,
                 default_qty_value=50)
        if bar_index == 0
            strategy.entry("Long", strategy.long)
        if bar_index == 2
            strategy.close("Long")
        """
    )
    report = BacktestEngine(BacktestConfig(initial_cash=10_000)).run(
        source,
        make_candles([100, 100, 101, 102]),
    )

    assert report.initial_cash == 1_000
    assert len(report) == 1
    assert report[0].quantity == 5


def test_pyramiding_combines_same_side_entries() -> None:
    source = dedent(
        """
        //@version=6
        strategy("pyramiding", overlay=true)
        if bar_index == 0
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 3
            strategy.close("Long")
        """
    )
    candles = make_candles([100, 101, 102, 103])
    report = BacktestEngine(BacktestConfig(pyramiding=2, close_at_end=True)).run(source, candles)

    assert len(report) == 1
    assert report[0].quantity == 2
    assert report[0].entry_price == 100.5


def test_runtime_wall_clock_limit_stops_slow_strategy() -> None:
    source = dedent(
        """
        //@version=6
        strategy("slow", overlay=true)
        if bar_index == 0
            while true
                value = close
        """
    )

    with pytest.raises(BacktestExecutionTimeoutError, match="execution time limit"):
        BacktestEngine(
            BacktestConfig(max_execution_steps=1_000_000, max_execution_seconds=1e-9)
        ).run(source, make_candles([100, 101]))


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

    with pytest.raises(BacktestExecutionLimitError, match="execution step limit") as raised:
        BacktestEngine(BacktestConfig(max_execution_steps=20)).run(
            source,
            make_candles([100, 101]),
        )

    assert raised.value.partial_report is not None
    assert raised.value.partial_report.partial is True
    assert raised.value.partial_report.bars == 0


def test_interrupted_runtime_preserves_completed_bars_and_batch_json(tmp_path: Path) -> None:
    strategy_dir = tmp_path / "Strategies"
    strategy_dir.mkdir()
    source = dedent(
        """
        //@version=6
        strategy("partial", overlay=true)
        if bar_index == 0
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            while true
                value = close
        """
    )
    strategy = strategy_dir / "partial.pine"
    strategy.write_text(source, encoding="utf-8")
    candles = make_candles([100, 101, 102, 101, 100])
    report = run_strategy_batch(
        [strategy],
        candles,
        exchange="synthetic",
        symbol="TEST/USDT",
        timeframe="1d",
        config=BacktestConfig(max_execution_steps=8, close_at_end=True),
        max_workers=1,
        show_progress=False,
    )

    result = report.by_name["partial"]
    assert result.status == "execution_limit"
    assert result.partial_report is not None
    assert result.partial_report.bars == 1
    assert result.partial_report.partial is True
    assert result.partial_report.final_equity == pytest.approx(9899.9)
    assert result.partial_report.open_position is not None
    assert result.partial_report.open_position["side"] == "long"
    assert result.execution_steps == result.partial_report.execution_steps
    assert result.approximations == result.partial_report.approximations
    assert report.summary["partial_results"] == 1
    loaded = type(report).from_json(report.write_json(tmp_path / "partial.json"))
    restored = loaded.by_name["partial"].partial_report
    assert restored is not None
    assert restored.bars == 1
    assert restored.to_dict() == result.partial_report.to_dict()
    assert "Interrupted-run snapshots" in build_overall_markdown(report)


def test_user_defined_type_constructors_and_mutating_methods_execute() -> None:
    source = dedent(
        """
        //@version=6
        strategy("objects", overlay=true)
        type State
            int count = 0
            method increment(State this, int amount) =>
                this.count := this.count + amount
                this
        var state = State.new()
        if bar_index == 0
            state := state.increment(2)
        if bar_index == 1
            strategy.entry("Long", strategy.long, qty=state.count)
        if bar_index == 3
            strategy.close("Long")
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        make_candles([100, 101, 102, 101, 100]),
    )

    assert len(report) == 1
    assert report[0].quantity == 2


def test_global_methods_and_maps_support_object_composition() -> None:
    source = dedent(
        """
        //@version=6
        strategy("composition", overlay=true)
        method increment(int value) => value + 1
        values = map.new<string, int>()
        values.put("answer", 41)
        answer = values.get("answer").increment()
        if bar_index == 0 and answer == 42
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            strategy.close("Long")
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        make_candles([100, 101, 102]),
    )

    assert len(report) == 1


def test_drawing_handles_keep_nonessential_methods_explicitly_approximated() -> None:
    source = dedent(
        """
        //@version=6
        strategy("drawing", overlay=true)
        method slope(line handle) => handle.get_y2() - handle.get_y1()
        handle = line.new(0, 1, 2, 3)
        if bar_index == 0 and handle.slope() == 2
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            strategy.close("Long")
        """
    )
    report = BacktestEngine(BacktestConfig(close_at_end=True)).run(
        source,
        make_candles([100, 101, 102]),
    )

    assert len(report) == 1
    assert "drawing.handles" in report.approximations


def test_nontrading_builtins_are_explicitly_approximated() -> None:
    source = dedent(
        """
        //@version=6
        strategy("nontrading builtins", overlay=true)
        text = tostring(bar_index)
        seconds = timeframe.in_seconds()
        ha = heikinashi()
        risingTwo = rising(close, 2)
        plotarrow(risingTwo, close)
        log.info(text)
        if bar_index == 0 and seconds == 86400
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            strategy.close("Long")
        """
    )
    report = BacktestEngine(
        BacktestConfig(close_at_end=True),
    ).run(source, make_candles([100, 101, 102]), timeframe="1d")

    assert len(report) == 1
    assert "plotting.non_trading" in report.approximations
    assert "heikinashi.ohlc" in report.approximations


def test_imported_user_defined_types_construct_and_dispatch_methods(tmp_path: Path) -> None:
    library_root = tmp_path / "Libraries"
    library_root.mkdir()
    (library_root / "objects.pine").write_text(
        dedent(
            """
            //@version=6
            export type Point
                int value
                method add(Point this, int amount) =>
                    this.value := this.value + amount
                    this.value
            """
        ),
        encoding="utf-8",
    )
    source = dedent(
        """
        //@version=6
        strategy("imported objects", overlay=true)
        import "objects" as objects
        point = objects.Point.new(2)
        if bar_index == 0 and point.add(3) == 5
            strategy.entry("Long", strategy.long, qty=1)
        if bar_index == 1
            strategy.close("Long")
        """
    )
    report = BacktestEngine(
        BacktestConfig(close_at_end=True),
        library_root=library_root,
    ).run(source, make_candles([100, 101, 102]))

    assert len(report) == 1
