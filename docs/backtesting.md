# Pine backtesting

The `pine_interpreter.backtest` package is a deterministic, bar-by-bar research
runtime for common Pine strategies. It is separate from the project's original
expression-only interpreter. Unsupported platform behavior is recorded as a
validation/runtime issue or an explicit approximation instead of being silently
converted into a trade.

The runtime is intended for screening and research. A positive result on one
short candle window is not evidence that a strategy is profitable or safe.

## Install

CCXT is optional for parsing and synthetic tests. Install the live-data extra
when using public exchange endpoints:

```bash
python -m pip install -e ".[backtest]"
```

No API key is required for the public `fetch_ohlcv` endpoint. The package's
current runtime version (`0.3.0`) is recorded in `BacktestReport` and batch metadata so
results can be compared across releases.

## Quick start

```python
from pine_interpreter import BacktestConfig, BacktestEngine, CCXTDataFeed

source = """
//@version=6
strategy("SMA crossover", overlay=true)
fast = ta.sma(close, 10)
slow = ta.sma(close, 20)
if ta.crossover(fast, slow)
    strategy.entry("Long", strategy.long)
if ta.crossunder(fast, slow)
    strategy.close("Long")
"""

candles = CCXTDataFeed("binance", "BTC/USDT", "1h").fetch(limit=500)
report = BacktestEngine(
    BacktestConfig(initial_cash=10_000, fee_rate=0.001, close_at_end=True)
).run(
    source,
    candles,
    name="sma-crossover",
    symbol="BTC/USDT",
    timeframe="1h",
    exchange="binance",
)

print(report)
print(report["total_return_pct"])
print(report[0])                 # first completed trade
print(report.metrics)
```

`BacktestEngine.validate(source, candles)` parses source and validates the
candle stream without running orders. `BacktestEngine.validate_source(source)`
is available when only syntax parsing is needed. Parsed source text is kept in
a bounded process-local cache; `parse_cache_info()` and `clear_parse_cache()`
are available for diagnostics.

For a quick terminal run:

```bash
pine-backtest strategy.pine --exchange binance --symbol BTC/USDT --timeframe 1h --limit 500
```

The same command is available as `python -m pine_interpreter.backtest`.

## Execution model

The current model is intentionally small and inspectable:

- Market entries and exits fill at the current candle close.
- Stop and limit entries are queued and can fill on a later candle.
- Long and short entries, reversals, pyramiding, `strategy.entry`, `order`,
  `exit`, `close`, `close_all`, and cancellation are supported.
- Partial exits accept `qty` and `qty_percent`; each completed slice is a
  separately attributed `Trade`.
- `stop_first`, `limit_first`, and `open_first` are explicit intrabar policies
  for ambiguous candles.
- Fees support a proportional `fee_rate` and `commission_per_trade`; optional
  `spread_bps`, `slippage_bps`, and per-bar `funding_rate_bps` assumptions are
  stored in the configuration. Funding is marked as an approximation.
- `pyramiding`, initial cash, default quantity, percent-of-equity sizing, and
  cash sizing can be configured globally and overridden by common strategy
  declaration arguments.
- Step-limit and wall-clock interruptions restore the last completed-bar broker
  state and attach a `partial=True` report. Partial results remain excluded
  from rankings and successful-status counts.

This is not a full exchange margin engine. For example, funding is charged per
bar rather than reconstructed from an exchange's historical funding ledger.
Reports should therefore state their configuration and candle assumptions.

## Supported Pine surface

The runtime covers a useful common subset rather than the complete TradingView
language:

- `open`, `high`, `low`, `close`, `volume`, `time`, `timenow`, `bar_index`,
  `last_bar_index`, and bounded series history such as `close[1]`
- declarations, assignments, `if`/`else`, common `for`/`while`/`switch` forms,
  tuple declarations, user functions, enums, and `var`/`varip` state
- dictionary-backed user-defined types, `Type.new()` constructors, mutating
  instance methods, global methods, arrays/maps of objects, and imported
  user-defined types
- common `math.*`, `array.*`, `map.*`, `str.*`, `input.*`, and Pine namespace constants
- moving averages and statistics, RSI, stochastic, CCI, MFI, WPR/CMO, ATR/TR,
  MACD, Bollinger/Keltner/Donchian, Supertrend, ADX/DMI, Aroon, SAR, VWAP/VWMA,
  momentum/ROC, correlation, linear regression, value-when, and related
  `ta.*` helpers
- approximate `request.*` handling; every affected report carries an
  approximation marker
- local Pine libraries through `import "name" as alias`; calls are resolved in
  an isolated per-runtime namespace with missing-path and circular-import
  checks
- `indicator`, `plot`, `alert`, and other nonessential declaration calls as
  no-ops

Every new builtin should have a deterministic policy and a regression test.
Unknown functions are rejected explicitly; they are never silently converted
into zero-valued signals or successful no-order runs. Nonessential chart/object
construction calls may be explicit no-ops, but their output cannot affect
orders. Invalid members, invalid lengths, and missing values are reported with
stable error categories. See `RECOMMENDATIONS.md` for the compatibility
roadmap and explicit remaining gaps.

## Reproducible candle snapshots

`CCXTDataFeed.fetch_snapshot()` stores exchange, symbol, timeframe, fetch time,
source, a SHA-256 content hash, quality warnings, and normalized rows. The
loader accepts the current object format and the legacy list-only cache format.
Requests over 1,000 candles are paged when the exchange supports the required
`since`/`until` behavior.

```python
from pine_interpreter import CCXTDataFeed, load_snapshot, save_snapshot

feed = CCXTDataFeed("binance", "BTC/USDT", "1h")
snapshot = feed.fetch_snapshot(limit=500)
save_snapshot("reports/.cache-btcusdt-1h.json", snapshot)
same_snapshot = load_snapshot("reports/.cache-btcusdt-1h.json")
assert same_snapshot.content_hash == snapshot.content_hash
```

Quality checks report duplicate timestamps, out-of-order candles, zero-volume
bars, and likely timeframe gaps. A report should retain the snapshot used for
the run, or at least its content hash and provenance.

## Full strategy baseline

The batch runner discovers every `.pine` file in a flat archive's `Strategies/`
directory or the populated source group in a grouped download tree, fetches
the selected market once, and runs independent files in parallel:

```bash
pine-backtest-batch \
  --root /path/to/Pine \
  --libraries /path/to/Pine/TradingView/Libraries \
  --exchange binance \
  --symbol BTC/USDT \
  --timeframe 1h \
  --limit 500 \
  --workers 8 \
  --cash 10000 \
  --fee 0.001 \
  --qty 0.001 \
  --max-steps 250000 \
  --timeout-seconds 30 \
  --intrabar-policy stop_first \
  --cache reports/.cache-btcusdt-1h.json \
  --output reports/baseline-btcusdt-1h.json \
  --csv reports/baseline-btcusdt-1h.csv
```

The command shows progress, rate, and ETA on stderr. The JSON report contains
source hashes, stable status/error categories, validation issues, feature
inventories, approximation markers, execution-step counts, data metadata,
duplicate-source groups, partial interrupted-run snapshots (including completed
bars, completed trades, equity, and any open-position snapshot), slowest-execution
profiling, and one normalized result per strategy. The CLI can automatically
use the matching `<root>/Libraries` directory in either layout; use
`--libraries` to override it.
`--validate-only` parses and performs semantic checks without fetching candles
or executing orders.

The batch API exposes the same controls:

```python
from pine_interpreter import BacktestConfig, discover_strategy_files, run_strategy_batch

report = run_strategy_batch(
    discover_strategy_files("/path/to/Pine"),
    candles,
    exchange="binance",
    symbol="BTC/USDT",
    timeframe="1h",
    config=BacktestConfig(default_qty=0.001, allow_short=True),
    max_workers=8,
    library_root="/path/to/Pine/TradingView/Libraries",
)
report.write_json("baseline.json")
report.write_csv("baseline.csv")
```

## Reports and search

`BacktestReport` supports name and integer indexing:

```python
report["final_equity"]
report["trade_count"]
report["trades"]
report[0]
len(report)
for trade in report:
    print(trade)
```

Reports include return, drawdown, win rate, profit factor, completed trades,
execution steps, an indexed equity curve, and explicit `partial` provenance for
interrupted runs. `risk_metrics(report)` adds
annualized return/volatility, Sharpe, Sortino, Calmar, expectancy, profit
factor, average holding period, and time in market.

Turn a batch JSON file into readable reports:

```bash
pine-backtest-report \
  --input reports/baseline-btcusdt-1h.json \
  --cache reports/.cache-btcusdt-1h.json \
  --output-dir reports \
  --top-count 100 \
  --ranking return
```

This creates `top-100-strategies.md` and `overall-baseline.md`. Only strategies
with at least one completed trade and status `backtested` are eligible. The
default `return` ranking uses lower drawdown as a tie-breaker; alternatives are
`risk-adjusted` and `drawdown`.

For a searchable local index and a small terminal explorer:

```bash
pine-backtest-index \
  --input reports/baseline-btcusdt-1h.json \
  --database reports/results.sqlite

pine-backtest-index \
  --database reports/results.sqlite \
  --search crossover \
  --sort-by drawdown \
  --format markdown \
  --output reports/explorer.md
```

The SQLite index supports status, category, source hash/name/path, return,
drawdown, trade count, and runtime queries without rerunning the archive.

## Research workflows

Chronological splits, rolling/anchored walk-forward windows, and an explicit
out-of-sample report are available from the public API and
`pine-backtest-research`:

```bash
pine-backtest-research \
  --source strategy.pine \
  --cache reports/.cache-btcusdt-1h.json \
  --mode out-of-sample \
  --output research-report.json
```

`split_candles`, `walk_forward_windows`, `run_walk_forward`, and
`run_out_of_sample` keep train/test data separate. `run_parameter_sweep`
accepts a bounded, explicit list of input overrides. `compare_markets` keeps
exchange, symbol, and timeframe as first-class dimensions.
`benchmark_reports` provides no-trade, deterministic random-entry,
moving-average-crossover, and buy-and-hold comparisons under the same broker
assumptions.

A strategy should remain labeled unvalidated until it has completed an
out-of-sample test. `WalkForwardReport.validation_status` and its serialized
summary expose that state. A parameter sweep is a research aid, not an
unrestricted optimization engine.

## Limitations and safe use

- The runtime does not execute arbitrary Python or system code from Pine files.
- Historical `request.*`, charting handles, exchange margin, funding ledgers,
  and some Pine type/qualifier rules remain partial. User-defined objects are
  intentionally represented by inspectable dictionaries rather than a full
  reference-type runtime.
- Pending orders use a candle-level model, not tick-level replay.
- Results can differ from TradingView because of data, fill, cost, and session
  assumptions. Compare the stored runtime version and configuration first.
- Never publish a generated ranking without its data provenance and execution
  assumptions. Use the Markdown files for human-readable summaries and the
  JSON/CSV/SQLite artifacts for analysis.
