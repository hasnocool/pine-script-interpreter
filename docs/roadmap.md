# Roadmap

## Phase 1 — Foundation

- Package and CLI scaffold
- Diagnostics with line and column information
- Token definitions
- Indentation-aware lexer
- Recursive-descent parser and AST
- Scoped interpreter with tests

## Phase 2 — Core Pine semantics

- Newline and indentation edge cases
- Strong type checks (`int`, `float`, `bool`, `string`, `color`)
- Type qualifiers: `const`, `simple`, `series`
- Declarations: `var`, `varip`, and `input`
- User-defined functions
- Method calls and built-in function registries

## Phase 3 — Time-series runtime

- Global `bar_index` and execution once per bar
- Historical series and `[]` history-referencing operator
- `na` behavior and propagation
- NaN, infinity, and overflow handling
- Tuple and user-defined object values
- Per-script-instance state isolation

## Phase 4 — Standard language library

- `math.*`
- `ta.*`
- `request.*`
- `strategy.*`
- `indicator.*`
- `plot.*`
- `alert.*`
- `input.*`
- Collections, matrices, maps, and arrays where practical

## Phase 5 — Indicators and strategies

- Plot, fill, bgcolor, and shape outputs
- Alert-condition evaluation
- Broker emulator
- Orders, fills, positions, and accounting
- Commission, slippage, and execution assumptions

## Phase 6 — Tooling and compatibility

- Rich error formatting and snippets
- Incremental parsing for an editor or language server
- Golden-file compatibility tests against TradingView behavior
- Performance benchmarks
- Packaging and documentation release

## Near-term milestones

1. Expand parser tests for indentation and multiline calls.
2. Add a semantic analyzer and type system.
3. Add a per-bar execution engine.
4. Seed built-ins with `open`, `high`, `low`, `close`, `volume`, and `time`.
5. Add a JSON result model for plots and script outputs.
