# Pine Script Interpreter

A Python implementation of an interpreter for TradingView Pine Script.

> **Status:** The lexer and parser cover the Pine v1-v6 syntax surface used by the public-script archive. A current archive smoke scan parses all 20,479 scripts. The runtime still executes only the smaller language subset documented in `docs/supported-language.md`.

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

The runtime currently covers a basic expression/statement subset. It does not yet implement the Pine standard library or time-series semantics.

See [docs/supported-language.md](docs/supported-language.md) for the exact subset and [docs/roadmap.md](docs/roadmap.md) for planned language support.

## Backtesting

The optional [`backtest`](docs/backtesting.md) module evaluates common Pine
strategies against normalized OHLCV candles, can fetch public CCXT data, and
returns a printable/indexable `BacktestReport`:

```bash
python -m pip install -e ".[backtest]"
```

The backtest runtime is deliberately separate from the basic expression
runtime and currently targets common moving-average, crossover, and
`strategy.entry`/`strategy.close` workflows.

## Design notes

Pine is not a normal batch scripting language. Each script is compiled first, then evaluated repeatedly as new bars arrive. The runtime therefore needs a global bar counter, historical series access, persistent state, and careful handling of `var`, `varip`, `input`, and series-type qualifiers. Those concerns are kept in `runtime/` instead of being mixed into parsing code.
