# Overall Pine Backtest Baseline

## Executive summary

This run attempted 6,081 archived Pine strategies against 500 binance BTC/USDT 1h candles. It is intended to establish a broad technical baseline, not to certify strategies as profitable or safe.

- **Run time:** 4m 28s
- **Runtime version:** `0.2.0`
- **Strategies attempted:** 6,081
- **Produced completed trades:** 815
- **Ran without completed orders:** 2,435
- **Could not be evaluated:** 2,831
- **Used an explicit approximation:** 571
- **Used a local Pine library:** 0
- **Duplicate source groups:** 0
- **Detailed shortlist:** See the companion `top-100-strategies.md` report.
- **Candle period:** 2026-09-03T02:00:00+00:00 through 2026-09-23T21:00:00+00:00
- **Candle fingerprint:** `c080a786dc02f88178034059126e08a622a2d17042b2131e23d5da6c5b728292`
- **Candle source:** `converted-legacy-list-cache`

## Results at a glance

| Outcome | Count | What it means |
| --- | ---: | --- |
| Backtested with trades | 815 | The runtime ran and closed at least one trade. |
| No orders | 2,435 | The runtime ran but did not close a trade on this data. |
| Validation errors | 2,771 | The current runtime could not evaluate part of the strategy. |
| Execution-limit stops | 43 | The strategy exceeded the safety step budget. |
| Runtime errors | 0 | An unexpected runtime problem stopped the strategy. |
| Parse errors | 0 | The source could not be parsed. |

## Performance snapshot

- **257** strategies with trades finished positive; **406** finished negative; **152** were approximately flat.
- The median return among trade-producing strategies was **0.00%**; the average was **-23.89%**.
- The median maximum drawdown was **1.71%**.
- The strongest eligible result was **Tomukas Scale In V2\_\_14l4u5gJ** at **35735.39%**.
- The weakest trade-producing result was **sonu1997\_\_FGELufUr** at **-41194.67%**.

## Top candidates

The table below uses raw total return, highest first; lower drawdown breaks ties and requires at least 1 completed trade(s).

| Rank | Strategy | Trades | Return | Max drawdown |
| ---: | --- | ---: | ---: | ---: |
| 1 | Tomukas Scale In V2__14l4u5gJ | 4 | 35735.39% | 31005.17% |
| 2 | WJ Strategy A__Oh8vgFgC | 1 | 70.07% | 7.15% |
| 3 | WJ Strategy B__aw6frVKI | 1 | 59.10% | 6.95% |
| 4 | multiple time frame strategy jiahejuzhen__6clgb3x4 | 42 | 53.01% | 127.90% |
| 5 | SuperTrend Strategy with Trend Based Exits__1F66QJ5V | 499 | 50.59% | 124.41% |
| 6 | Volatility Pulse with Dynamic Exit__03HpjkVN | 28 | 19.13% | 55.90% |
| 7 | Donchian Breakout Strategy__laT8fTXp | 13 | 10.02% | 20.56% |
| 8 | DRACO TOMAS EMA Trend Follower__Rf2N01q7 | 40 | 9.72% | 21.32% |
| 9 | Gold Breakout PRO (Daily Filter)__PU4lrqHR | 26 | 9.61% | 20.41% |
| 10 | Albtrader NQ BTC Gold Multi TF Signal Bot v2__ASHXoH1O | 33 | 9.56% | 21.75% |

## Main evaluation blockers

These are the most common reasons a strategy could not be evaluated. They describe coverage gaps in the compact runtime, not necessarily bad Pine source.

| Blocker | Strategies | Share of recorded errors |
| --- | ---: | ---: |
| Required numeric value was unavailable | 1,096 | 38.71% |
| unknown Pine builtin | 511 | 18.05% |
| Order exceeded available cash | 437 | 15.44% |
| Unknown Pine identifier or constant | 406 | 14.34% |
| imported Pine library was not found | 129 | 4.56% |
| unknown strategy member | 95 | 3.36% |
| Runaway loop or excessive runtime steps | 43 | 1.52% |
| Invalid technical-analysis length | 41 | 1.45% |
| order quantity must be positive and finite | 20 | 0.71% |
| execution time limit exceeded (30s) | 17 | 0.60% |
| strategy initial_capital must be positive | 10 | 0.35% |
| Unsupported call target | 9 | 0.32% |

## Important interpretation notes

1. This is one market, one timeframe, and one historical sample. It does not establish out-of-sample performance.
2. The baseline uses the compact Pine execution model documented in `docs/backtesting.md`; unsupported TradingView features are recorded rather than silently treated as trading logic.
3. Orders are modeled at candle close with the configured fee and slippage assumptions. Real fills can be worse, especially during fast markets.
4. A strategy with few trades can rank highly by chance. Review trade count, drawdown, and robustness together.
5. The top list is a screening step. Do not deploy candidates without longer-history testing, multiple markets, walk-forward testing, and out-of-sample validation.

## Recommended next step

Take the top candidates from `top-100-strategies.md`, remove duplicate implementations, and rerun them on a longer, non-overlapping period and at least one additional market. Compare return, drawdown, trade count, and stability rather than return alone.

## Recorded execution assumptions

- Starting cash: 10,000.00
- Fee rate: 0.10%
- Slippage: 0 basis points
- Default quantity: 0.001
- Pyramiding entries: 1
- Short entries: allowed
- Close open position at end: no
- Execution-step limit: 250,000
- Execution-time limit: 30s
- Intrabar policy: stop_first

## Benchmark comparison

These baselines use the same candle window and broker assumptions as the strategy run.

| Benchmark | Completed trades | Return | Max drawdown | Final equity |
| --- | ---: | ---: | ---: | ---: |
| No Trade | 0 | 0.00% | 0.00% | 10,000.00 |
| Random | 250 | -0.37% | 1.22% | 9,962.95 |
| Ma Crossover | 8 | 0.04% | 0.84% | 10,004.35 |
| Buy And Hold | 1 | 8.25% | 103.03% | 10,824.88 |
