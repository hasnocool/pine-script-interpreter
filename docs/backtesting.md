# Pine backtesting

The `pine_interpreter.backtest` package provides a small, deterministic
backtest path for common Pine strategies. It parses Pine source and evaluates
it once per OHLCV bar. It is separate from the basic expression-only runtime,
so unsupported platform features are rejected or ignored explicitly rather
than silently changing the order model.

## Install

CCXT is optional for parsing and synthetic tests. Install the live-data extra
when using public exchange endpoints:

```bash
python -m pip install -e ".[backtest]"
```

No API key is required for the public `fetch_ohlcv` endpoint.

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

`BacktestEngine.validate(source, candles)` parses the source and validates the
candle stream without running the strategy. The `validate_source` helper is
available when only syntax validation is needed.

For a quick terminal run:

```bash
pine-backtest strategy.pine --exchange binance --symbol BTC/USDT --timeframe 1h --limit 500
```

The same command is available as `python -m pine_interpreter.backtest`.

## Supported strategy surface

The first backtest runtime supports:

- `open`, `high`, `low`, `close`, `volume`, `time`, `timenow`, and `bar_index`
- series history such as `close[1]`
- `if`/`else`, common loops, assignments, tuple declarations, and user
  functions
- `ta.sma`, `ema`, `rma`, `wma`, `hma`, `highest`, `lowest`, `change`, `roc`,
  `valuewhen`, `percentrank`, `crossover`, and `crossunder`
- common `math.*`, `array.*`, `str.*`, and `input.*` helpers
- common color/shape/position constants, `hl2`/`hlc3`/`ohlc4`, and `tr`
- `strategy.entry`, `order`, `close`, `close_all`, `exit`, and cancel calls
- `indicator`, `plot`, `alert`, and other nonessential declaration calls as
  no-ops

Orders fill at the current bar close, with configurable fee and slippage
assumptions. This is intentionally a compact validation/backtest surface,
not a complete TradingView emulator. Add a new builtin with an explicit
`_call_named` branch and a deterministic regression test.

## Reports

`BacktestReport` supports both name and integer indexing:

```python
report["final_equity"]
report["trade_count"]
report["trades"]
report[0]
len(report)
for trade in report:
    print(trade)
```

Reports include total return, maximum drawdown, win rate, profit factor,
completed trades, and an indexed equity curve. `print_report(report)` is a
short alias for `report.print()`.

## Full strategy baseline

The batch runner discovers every file in an archive's `Strategies/` directory,
fetches the selected market once, and runs independent files in parallel:

```bash
pine-backtest-batch \
  --root /path/to/PineScripts_All \
  --exchange binance \
  --symbol BTC/USDT \
  --timeframe 1h \
  --limit 500 \
  --workers 8 \
  --max-steps 250000 \
  --cache .cache/btc-usdt-1h.json \
  --output reports/baseline-btcusdt-1h.json \
  --csv reports/baseline-btcusdt-1h.csv
```

The command shows a live progress bar, rate, and ETA on stderr. The JSON report
contains a summary, a name/path index, and one normalized result per strategy;
unsupported, invalid, and execution-limit scripts are recorded rather than
stopping the batch. The batch CLI allows short entries by default; pass
`--long-only` to reject them. `--max-steps` bounds pathological loops so one
script cannot stall the complete baseline.

## Plain-English Markdown reports

The batch JSON can be turned into a readable overall report and a ranked
best-strategy report:

```bash
pine-backtest-report \
  --input reports/baseline-btcusdt-1h.json \
  --cache reports/.cache-btcusdt-1h.json \
  --output-dir reports \
  --top-count 100 \
  --ranking return
```

This creates:

- `reports/top-100-strategies.md` — the strongest eligible strategies, with
  return, drawdown, trade count, final equity, and source file
- `reports/overall-baseline.md` — a plain-English explanation of the entire
  run, status totals, performance distribution, evaluation blockers, and
  limitations

Only strategies with at least one completed trade are eligible for the
ranking. The default `return` ranking is raw return, with lower drawdown used
as a tie-breaker. Use `--ranking risk-adjusted` for return divided by maximum
drawdown, or `--ranking drawdown` to favor the lowest drawdown. The reports
are a screening baseline and should not be treated as investment advice or
out-of-sample validation.
