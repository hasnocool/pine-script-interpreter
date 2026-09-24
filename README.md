# Pine Script Interpreter

A Python implementation of an interpreter for TradingView Pine Script.

> **Status:** The lexer and parser cover the Pine v1-v6 syntax surface used by the public-script archive. A current archive smoke scan parses all 20,479 scripts. The separate backtest/research runtime now covers a practical common-strategy subset, with explicit diagnostics and approximations documented in `docs/backtesting.md`.

## Project goals

- Parse Pine-style source into a clear syntax tree.
- Execute scripts incrementally, one market bar at a time.
- Model Pine's stateful variables, time series, built-ins, and declarations.
- Support indicators and strategies without executing untrusted system code.
- Provide precise source locations and useful diagnostics.

## Directory layout

```text
pine-script-interpreter/
├── src/
│   └── pine_interpreter/
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       ├── diagnostics.py
│       ├── interpreter.py
│       ├── lexer/
│       │   ├── token.py
│       │   └── lexer.py
│       ├── parser/
│       │   ├── ast_nodes.py
│       │   └── parser.py
│       └── runtime/
│           ├── context.py
│           ├── series.py
│           └── values.py
├── tests/
│   ├── test_interpreter.py
│   ├── test_lexer.py
│   └── test_parser.py
├── docs/
│   ├── architecture.md
│   ├── roadmap.md
│   └── supported-language.md
├── examples/
│   └── 01_basics.pine
├── .gitignore
├── pyproject.toml
└── README.md
```

## Quick start

Python 3.11 or newer is required.

```bash
cd pine-script-interpreter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
python -m pine_interpreter examples/01_basics.pine --execute
```

Without `--execute`, the CLI only parses and validates the syntax tree.

## Current scope

The syntax frontend covers:

- Pine v1-v6 indentation, wrapped calls, legacy semicolons/quotes, and modern scripts
- Primitive, color, `na`, array/tuple, history, namespace, call, and ternary expressions
- Typed and storage-qualified declarations, reassignment, tuple destructuring, and collections
- `if`, loops, `switch`, functions, methods, imports, enums, and user-defined types
- Source-aware diagnostics for lexer and parser errors

The basic expression runtime remains intentionally small. The separate optional backtest runtime adds a practical common `ta.*`/`strategy.*` surface, snapshots, research helpers, and archive-wide reporting; it is not a complete TradingView emulator.

See [docs/supported-language.md](docs/supported-language.md) for the exact subset and [docs/roadmap.md](docs/roadmap.md) for planned language support.

## Backtesting

The optional [`backtest`](docs/backtesting.md) module evaluates common Pine
strategies against normalized OHLCV candles, can fetch public CCXT data, and
returns a printable/indexable `BacktestReport`:

```bash
python -m pip install -e ".[backtest]"
```

The backtest runtime is deliberately separate from the basic expression runtime. It supports common indicators, long/short orders, stop/limit fills, pyramiding, partial exits, local Pine libraries, semantic validation, reproducible candle snapshots, and OOS/walk-forward research. For a fast archive-wide baseline, install the extra and run:

```bash
pine-backtest-batch \
  --root /path/to/PineScripts_All \
  --cache .cache/btc-usdt-1h.json \
  --output reports/baseline-btcusdt-1h.json \
  --csv reports/baseline-btcusdt-1h.csv
```

The batch command fetches public Binance BTC/USDT 1h data once, processes the
archive's `Strategies/` directory in parallel with a progress bar/ETA, and
writes an indexed JSON/CSV report. A plain-English Markdown summary and
best-100 list can be generated from that JSON with:

```bash
pine-backtest-report \
  --input reports/baseline-btcusdt-1h.json \
  --cache reports/.cache-btcusdt-1h.json \
  --output-dir reports
```

See [docs/backtesting.md](docs/backtesting.md) for supported built-ins,
execution limits, report loading/indexing, and ranking options. The prioritized
feature roadmap is in [RECOMMENDATIONS.md](RECOMMENDATIONS.md).

## Design notes

Pine is not a normal batch scripting language. Each script is compiled first, then evaluated repeatedly as new bars arrive. The runtime therefore needs a global bar counter, historical series access, persistent state, and careful handling of `var`, `varip`, `input`, and series-type qualifiers. Those concerns are kept in `runtime/` instead of being mixed into parsing code.
