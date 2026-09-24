# Recommendations

## Purpose

This document describes the features and abilities that should be added next to
turn the archive-wide baseline into a useful Pine research platform.

## Implementation status (runtime 0.3.1)

The first implementation pass is now in the repository. The original counts
below are retained as the historical pre-runtime baseline; rerun the batch
after changing the runtime before comparing counts.

Implemented in the current pass:

- stable validation/runtime error categories, source hashes, locations,
  execution-step counts, feature inventories, and approximation markers
- SHA-256 candle snapshots with provenance, quality warnings, legacy-cache
  loading, and CCXT pagination for long requests
- common `ta.*` indicators, constants/namespaces, controlled approximate
  `request.*` handling, and local Pine library imports with path/circular
  checks
- market/stop/limit orders, explicit intrabar policies, short entries,
  pyramiding, percent/cash sizing, partial exits, fees, spread, slippage, and
  configurable per-bar funding
- semantic analysis and `--validate-only` batch mode
- wall-clock and step safety limits with parallel workers; interrupted runs
  persist completed-bar partial snapshots and slowest-execution profiling
- dictionary-backed user-defined types, constructors, mutating instance/global
  methods, object arrays/maps, imported types, and drawing-handle approximations
- named input defaults, common string/map helpers, and richer collection/object
  dispatch
- legacy v2/v3 builtin aliases, tuple-valued indicator propagation, dynamic
  `na` handling, and marked standard-library stubs for common TradingView TA
  imports
- non-mutating rejection of unaffordable/zero-quantity orders, with explicit
  approximation markers
- indexed JSON/CSV reports, plain-English Markdown, source/data/runtime
  provenance, and a SQLite result index/terminal explorer
- chronological splits, walk-forward/OOS runs, parameter sweeps, market
  comparisons, risk metrics, and reproducible benchmark suites

The latest local baseline (runtime `0.3.1`, cached Binance BTC/USDT `1h`
snapshot, 500 candles, 6,081 strategies) measured:

- **1,962** strategies with completed trades, up from 862 in runtime 0.3.0
- **3,861** no-order results
- **131** validation errors, down from 2,569 in runtime 0.3.0
- **127** execution-limit stops and **0** wall-clock timeout stops
- **127** partial diagnostic snapshots
- **0** runtime errors and **0** parse errors

This is a coverage report, not a profitability claim. The compatibility delta
is detailed in `reports/compatibility-delta.md`; it records 1,116 newly eligible
strategies and a net gain of 1,100 completed-trade strategies. The target archive
is `Pine/TradingView`: 20,479 Pine files parse successfully, including the 6,081
strategy files used for the baseline. The result includes feature inventories,
source hashes, the candle content hash, and explicit approximation markers.
Re-run the batch after any runtime change rather than comparing these counts to
the historical baseline.

The highest-value remaining work is compatibility depth (complete exchange
order/margin accounting, session/timeframe semantics, and more builtins),
broader compatibility fixtures, and a tick- or trade-level data adapter.
Partial-timeout artifacts and a first user-defined-object runtime are now
implemented; these should be expanded only with explicit policies and
regression measurements; see the detailed milestones below.

The recommendations below retain an earlier pre-0.3 coverage snapshot for
comparison with the current baseline:

- **6,081** strategies in the archive's populated strategy directory were attempted.
- **273** produced at least one completed trade.
- **4,474** ran without a completed order.
- **1,260** hit validation errors.
- **38** were stopped by the execution-step safety limit.
- **36** hit runtime errors.
- **0** failed parsing.

The earlier snapshot's largest recorded evaluation blockers were:

| Blocker | Count | What it tells us |
| --- | ---: | --- |
| Unknown Pine identifiers or constants | 611 | The standard-library namespace and constant registry needs to be much broader. |
| Required numeric value unavailable | 545 | More indicators and better `na`/type propagation are needed. |
| Invalid technical-analysis length | 63 | Inputs and dynamic lengths need validation and safer defaults. |
| Execution-limit stops | 38 | Loops and expensive scripts need isolation, profiling, and better limits. |
| Missing objects or values | 32 | Object, tuple, and `request.*` semantics are incomplete. |
| Cash or order-sizing failures | 25 | The broker needs a fuller position-sizing and margin model. |

A strategy appearing high in the current 0.3.1 report is not automatically a
good investment. The current report is a technical baseline on one market, one
timeframe, and one relatively short data sample.

## Guiding principles

1. **Correctness before coverage.** A clearly reported unsupported feature is
   better than a plausible-looking but incorrect trade.
2. **Every result must be reproducible.** Record the source, data snapshot,
   configuration, runtime version, and execution assumptions.
3. **Optimize for research usefulness.** Support searching, comparing,
   deduplicating, and rerunning strategies rather than only producing one large
   table.
4. **Keep unsupported behavior explicit.** Never silently turn an unknown
   function into a successful trade.
5. **Test against the archive continuously.** Every new builtin should reduce a
   measured blocker category without breaking the existing corpus.

## Current milestone mapping

- **Milestone 1 — implemented:** formal categories, snapshot metadata/hashes,
  runtime/config provenance, semantic validation, and execution limits.
- **Milestone 2 — partially implemented:** the common `ta.*` surface,
  strategy accounting/order controls, approximate `request.*`, namespace
  constants, and dictionary-backed user-defined objects are present; deeper
  qualifier typing and exchange-specific semantics remain.
- **Milestone 3 — implemented as a research toolkit:** OOS/walk-forward,
  multi-market runs, risk-adjusted rankings, duplicate hashes, and benchmark
  helpers are available. OOS labels and benchmark fields are not yet merged
  into every historical batch artifact.
- **Milestone 4 — partially implemented:** SQLite indexing, terminal explorer,
  bounded sweeps, parsed-source caching, partial-result persistence, and
  slowest-execution profiling are available. A browser UI, incremental cache
  database, and language-server integration remain future work.
## Priority 0 — Make results trustworthy

These should be completed before treating the top-100 list as a meaningful
ranking.

### 1. Expand the execution model

Add support for the order behavior that most often changes results:

- Long and short entries, exits, reversals, and pyramiding
- `strategy.order`, `strategy.entry`, `strategy.exit`, `strategy.close`, and
  `strategy.close_all` with complete argument handling
- Market, stop, and limit orders
- Explicit intrabar order assumptions: stop before limit, limit before stop, or
  an optimistic/pessimistic policy selected by the user
- Quantity expressions, percent-of-equity sizing, fixed cash sizing, and
  position caps
- Commission models, spread, slippage, funding, and configurable margin
- Open-position handling at the end of a test
- Partial exits and trade-level attribution

**Done when:** a small reference strategy produces the same fills and balances
under each documented execution policy, and the policy is stored in every
report.

### 2. Create reproducible data snapshots

The current report uses a cached CCXT response. Improve this with:

- Exchange, symbol, timeframe, start/end timestamps, and candle count metadata
- A content hash for every candle snapshot
- Pagination for limits larger than one exchange request
- Validation for missing, duplicate, out-of-order, or malformed candles
- Timezone normalization and daylight-saving handling
- Data-quality warnings in the report
- A standard snapshot format that can be committed, cached, or downloaded

A report should be reproducible even if the exchange later changes or removes
historical data.

### 3. Add a formal error and feature registry

Replace free-form exception messages with stable categories such as:

- `unsupported_namespace`
- `unknown_builtin`
- `type_mismatch`
- `missing_value`
- `invalid_order`
- `insufficient_data`
- `execution_limit`
- `data_error`

Each result should include a category, a human-readable explanation, source
location when available, and a link or identifier for the relevant feature
request. This makes the 1,260 validation errors actionable instead of treating
them as one undifferentiated group.

### 4. Add a semantic analyzer

Before executing a strategy, detect problems such as:

- Unknown identifiers and namespaces
- Invalid function arguments
- Invalid types and qualifiers
- Assignments to read-only values
- Duplicate declarations
- Invalid history offsets
- Invalid or dynamic indicator lengths
- Order calls with impossible arguments
- Unreachable or potentially unbounded loops

The analyzer should provide line and column information and a plain-English
explanation. It should also support a `--validate-only` batch mode.

### 5. Isolate and profile strategy execution

The current step limit prevents some infinite loops, but a stronger design
should:

- Run each strategy in an isolated worker process
- Apply both an execution-step limit and a wall-clock timeout
- Kill and replace timed-out workers safely
- Track peak memory and approximate CPU time
- Save a partial result when a strategy times out or hits the execution limit
- Display the slowest strategies in the batch summary

**Current implementation:** wall-clock time, execution steps, completed-bar
partial snapshots, and slowest-strategy tables are persisted. Peak memory and
worker replacement are still future work.

This is essential when scaling from thousands of strategies to the full
archive or to many parameter combinations.

## Priority 1 — Expand high-value Pine compatibility

The largest immediate opportunity is reducing the 611 unknown-identifier and
545 numeric-value failures.

### 1. Complete the common `ta.*` library

Prioritize indicators that are common in the archive:

- RSI, stochastic, CCI, MFI, ROC, momentum, and rate-of-change variants
- ATR and true range
- Bollinger Bands, Keltner Channels, and Donchian Channels
- MACD and other multi-output indicators
- Supertrend, ADX/DMI, SAR, and Ichimoku components
- VWAP and anchored VWAP
- `barssince`, `valuewhen`, `highest`, `lowest`, `change`, `roc`, and correlation
- Pivot points and regression/linear-trend helpers

Every indicator should have:

1. A documented return type
2. Deterministic handling of `na` values
3. A warm-up period
4. Tests against hand-calculated examples
5. A clear policy for unsupported timeframes or parameters

### 2. Implement `request.*` safely

Add controlled support for:

- `request.security`
- `request.security_lower_tf`
- `request.security_syminfo`
- `request.financial`
- `request.dividends`
- `request.earnings`
- `request.quandl` or other providers only when explicitly enabled

These calls need a documented policy for unavailable history. Returning a
plausible value without explaining the approximation would make results
misleading. At minimum, mark the result as approximate in diagnostics and
reports.

### 3. Expand namespaces and constants

Create a centralized registry for constants and members from:

- `color`, `shape`, `size`, `location`, `display`, `format`, and `position`
- `syminfo`, `timeframe`, `barstate`, `session`, and `currency`
- `strategy` order and accounting properties
- `input` types and option groups
- `plot`, `alert`, `table`, `label`, `line`, and `box`

Unknown members should produce a precise diagnostic such as
`unknown strategy member: position_average_price` rather than a generic
identifier error.

### 4. Add Pine's type and qualifier system

The runtime should model:

- `int`, `float`, `bool`, `string`, `color`, `line`, `label`, and `box`
- `series`, `simple`, `const`, and `input` qualifiers
- `array`, `matrix`, `map`, tuple, and user-defined object values
- Method calls and fields on user-defined types
- `var`, `varip`, and persistent state rules
- `na` propagation through arithmetic, comparisons, and function calls

**Current implementation:** constructors, field mutation, global and instance
methods, imported types, object arrays/maps, and explicit dictionary-backed
representation are covered by regression tests. Full qualifier inference,
copy-on-write semantics, and reference identity checks remain future work.

This will reduce false runtime errors and prevent invalid values from reaching
the broker.

### 5. Support libraries and imports

The archive contains a `Libraries/` directory. Add a controlled library system
for:

- Local Pine library imports
- Namespaced function calls
- Dependency discovery
- Circular-import detection
- Per-strategy library isolation
- Library caching without sharing mutable state accidentally

Do not execute arbitrary Python or system code from a Pine source file.

## Priority 1 — Make the research results meaningful

A larger backtest corpus is only useful if the results can be trusted and
compared.

### 1. Add out-of-sample and walk-forward testing

For each shortlisted strategy, support:

- In-sample and out-of-sample date ranges
- Rolling and anchored walk-forward windows
- Parameter training and validation splits
- Multiple independent test periods
- A comparison between in-sample and out-of-sample results

A strategy should be labeled as **unvalidated** until it has completed at
least one out-of-sample test.

### 2. Add multi-market and multi-timeframe runs

Run the same strategy against several public markets, such as:

- BTC/USDT and ETH/USDT
- Multiple spot and futures symbols
- 15m, 1h, 4h, and 1d timeframes
- Different exchange feeds where data quality permits

Store the market and timeframe as first-class report dimensions. Do not merge
results from different data configurations into one ranking.

### 3. Improve ranking metrics

Raw return should remain visible, but it should not be the only ranking method.
Add:

- Return divided by maximum drawdown
- Sharpe and Sortino ratios
- Calmar ratio
- Profit factor and expectancy
- Win rate and average trade
- Trade count and holding period
- Exposure and time-in-market
- Stability across periods and markets
- Parameter sensitivity
- Duplicate or near-duplicate strategy detection

The default report can continue to show raw return, but it should also show a
risk-adjusted shortlist and an out-of-sample shortlist.

### 4. Add parameter sweeps

Allow a bounded, reproducible search over selected inputs:

- Cartesian and one-factor-at-a-time sweeps
- Per-strategy parameter limits
- Parallel execution with progress and ETA
- Early stopping for clearly poor results
- Results stored in a queryable index
- Clear separation between training, validation, and test parameters

Guard against unrestricted parameter searches becoming an overfitting engine.

### 5. Add benchmark comparisons

Every report should compare strategies against simple baselines such as:

- Buy and hold
- A random-entry baseline with the same trade frequency
- A moving-average crossover baseline
- A no-trade baseline

This helps distinguish genuine strategy behavior from the effect of the market
period or the broker assumptions.

## Priority 2 — Improve performance and usability

### 1. Add a searchable result index

For large corpora, store results in SQLite, Parquet, or another queryable
format while continuing to support JSON and CSV exports. Index:

- Strategy name and source path
- Content hash
- Market, exchange, timeframe, and date range
- Status and error category
- Return, drawdown, trade count, and risk metrics
- Runtime duration and execution-step count
- Library dependencies

The current name/path index should remain available as a simple interface.

### 2. Add a report explorer

A lightweight local web or command-line explorer should support:

- Search by name, source hash, status, or error category
- Sort by return, drawdown, trade count, or runtime
- Filter for completed trades, validation errors, or execution limits
- Compare two or more strategies
- Show source and data metadata
- Export a selected subset to Markdown, CSV, or JSON

### 3. Cache parsing and compilation

The batch runner currently parses each source independently. Add:

- Source-content hashes
- Parsed AST caching
- Compiled runtime representation caching
- Reuse of unchanged strategies across runs
- Profiling to identify whether parsing, evaluation, or reporting is the bottleneck

### 4. Add incremental and editor-friendly APIs

Support:

- Parse-on-keystroke or language-server diagnostics
- Source spans for unknown identifiers and type errors
- Quick fixes for common issues
- A stable syntax tree API
- Deterministic error codes suitable for editors and CI

## Priority 2 — Testing and release quality

### 1. Build a compatibility test corpus

Turn a representative subset of archived strategies into fixtures. For each
fixture, store:

- Expected parse result
- Expected normalized AST or feature list
- Expected supported/unsupported feature list
- Expected validation diagnostics
- Deterministic backtest metrics when execution is supported

Do not use the entire archive as a golden-output test if small engine changes
make that too brittle. Use targeted fixtures plus aggregate coverage reports.

### 2. Add property-based and differential tests

Useful properties include:

- The same source and data always produce the same report.
- Changing only fees or slippage cannot improve net performance without a
  corresponding fill change.
- A strategy with no orders leaves equity unchanged except for documented
  costs.
- History access never reads future bars.
- Batch execution with one worker and multiple workers produces identical
  results.

Where possible, compare simple strategies against manually calculated or
known-good reference results.

### 3. Version the execution model

Include a semantic/runtime version in every report. When order behavior,
indicator behavior, or data normalization changes, users should be able to tell
why two reports differ.

## Suggested implementation order

### Milestone 1 — Trustworthy baseline

- Formal error categories
- Data snapshot metadata and hashes
- Config/version provenance
- Semantic validation mode
- Execution-step and wall-clock isolation

### Milestone 2 — Broad simple-strategy coverage

- Common `ta.*` indicators
- `strategy.*` accounting and order properties
- `request.security` with explicit approximation policy
- Common constants and namespaces
- Better `na` and dynamic-length handling

### Milestone 3 — Research validity

- Out-of-sample runner
- Walk-forward runner
- Multi-market/timeframe comparisons
- Risk-adjusted rankings
- Benchmark baselines
- Duplicate detection

### Milestone 4 — Research platform

- Searchable SQLite/Parquet index
- Report explorer
- Parameter sweeps
- Cached compilation
- Language-server and editor integration

## Things to avoid

- **Do not call a strategy profitable because it has a positive return on one
  short in-sample period.**
- **Do not silently convert unknown builtins into zero or `no_orders`.** Keep
  the error visible and improve the implementation deliberately.
- **Do not optimize the archive around raw return alone.** This rewards
  overfitting, lucky periods, and strategies with very few trades.
- **Do not implement every charting function before fixing order semantics.**
  Chart calls usually have less effect on strategy results than fills,
  position sizing, and `na` handling.
- **Do not allow unrestricted parameter searches or arbitrary code execution.**
  Both can be expensive and unsafe.
- **Do not publish generated results without their data snapshot and runtime
  configuration.**

## Definition of a strong next release

A strong next release should be able to say:

1. The source and data snapshot are exactly identified.
2. The strategy passed semantic validation or has a precise unsupported-feature
   reason.
3. The order model and costs are documented.
4. The result is deterministic and reproducible.
5. The strategy has been tested out of sample.
6. Its rank can be explained by return, risk, trade count, and stability.
7. The result can be searched, compared, and exported without rerunning the
   entire archive.
