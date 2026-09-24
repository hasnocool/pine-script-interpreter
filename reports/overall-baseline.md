# Overall Pine Backtest Baseline

## Executive summary

This run attempted 6,081 archived Pine strategies against 500 binance BTC/USDT 1h candles. It is intended to establish a broad technical baseline, not to certify strategies as profitable or safe.

- **Run time:** 6m 7s
- **Runtime version:** `0.3.0`
- **Strategies attempted:** 6,081
- **Produced completed trades:** 862
- **Ran without completed orders:** 2,583
- **Could not be evaluated:** 2,636
- **Saved partial interrupted results:** 67
- **Used an explicit approximation:** 935
- **Used a local Pine library:** 0
- **Duplicate source groups:** 0
- **Detailed shortlist:** See the companion `top-100-strategies.md` report.
- **Candle period:** 2026-09-03T02:00:00+00:00 through 2026-09-23T21:00:00+00:00
- **Candle fingerprint:** `c080a786dc02f88178034059126e08a622a2d17042b2131e23d5da6c5b728292`
- **Candle source:** `converted-legacy-list-cache`

## Results at a glance

| Outcome | Count | What it means |
| --- | ---: | --- |
| Backtested with trades | 862 | The runtime ran and closed at least one trade. |
| No orders | 2,583 | The runtime ran but did not close a trade on this data. |
| Validation errors | 2,569 | The current runtime could not evaluate part of the strategy. |
| Execution-limit stops | 49 | The strategy exceeded the safety step budget. |
| Wall-clock timeout stops | 18 | The strategy exceeded the wall-clock budget; a partial snapshot may be saved. |
| Runtime errors | 0 | An unexpected runtime problem stopped the strategy. |
| Parse errors | 0 | The source could not be parsed. |

## Performance snapshot

- **269** strategies with trades finished positive; **429** finished negative; **164** were approximately flat.
- The median return among trade-producing strategies was **0.00%**; the average was **-22.65%**.
- The median maximum drawdown was **1.72%**.
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
| Required numeric value was unavailable | 1,071 | 40.63% |
| Order exceeded available cash | 470 | 17.83% |
| Unknown Pine identifier or constant | 433 | 16.43% |
| unknown Pine builtin | 250 | 9.48% |
| imported Pine library was not found | 129 | 4.89% |
| unknown strategy member | 93 | 3.53% |
| Runaway loop or excessive runtime steps | 49 | 1.86% |
| Invalid technical-analysis length | 44 | 1.67% |
| order quantity must be positive and finite | 23 | 0.87% |
| execution time limit exceeded (30s) | 18 | 0.68% |
| Tuple assignment could not be matched | 13 | 0.49% |
| strategy initial_capital must be positive | 10 | 0.38% |

## Interrupted-run snapshots

These rows preserve accounting through the last completed bar. They are diagnostics only and are never eligible for the ranking.

| Strategy | Stop reason | Completed bars | Completed trades | Final equity | Open side |
| --- | --- | ---: | ---: | ---: | --- |
| BTC Future Gamma Weighted Momentum Model (BGMM)__ksy34iRO | execution_limit | 480 | 0 | 10,000.00 | flat |
| CleanTradeQuantum v7.2 OB FVG INTEGRATED__JeRCEqWU | execution_limit | 443 | 0 | 1,000.00 | flat |
| Ehlers Combo Strategy__4v6Z04vI | execution_limit | 438 | 0 | 1,000.00 | flat |
| TASC 2024.08 Volume Confirmation For A Trend System__K3s5dmdv | execution_timeout | 435 | 0 | 10,000.00 | flat |
| Renko Strategy__bl1J6Tfj | execution_limit | 429 | 0 | 1,000.00 | flat |
| BUY SELL on the levels only__okTkQZnL | execution_limit | 427 | 0 | 5,000.00 | flat |
| Buy Sell on the levels__DPx9VRI9 | execution_limit | 427 | 0 | 5,000.00 | flat |
| Rob Booker ADX Breakout updated to pinescript V5__qoaam9SI | execution_timeout | 420 | 0 | 100,000.00 | flat |
| FluxGate Daily Swing Strategy__DMyXoeuY | execution_limit | 413 | 0 | 25,000.00 | flat |
| meta capitulation__t7RRJhPX | execution_limit | 409 | 0 | 10,000.00 | flat |

## Slowest executions

These wall-clock measurements are profiling signals, not performance results.

| Strategy | Status | Elapsed | Execution steps | Partial bars | Partial trades |
| --- | --- | ---: | ---: | ---: | ---: |
| Volatility Traders Minds Strategy (VTM Strategy)__nDPVOul3 | execution_timeout | 30.384s | 18280 | 358 | 0 |
| Turtle Strategy Triple EMA Trend with ADX and ATR__vzXlqRCT | execution_timeout | 30.264s | 16692 | 387 | 0 |
| NSDT HAMA Candles STRAT__B4dBAINf | execution_timeout | 30.256s | 48825 | 361 | 0 |
| Average Directional Index v2__7Ijtq4XE | execution_timeout | 30.174s | 5502 | 392 | 0 |
| Glory Hole with SMA + ADX Strategy__4RfzhZy0 | execution_timeout | 30.163s | 5398 | 357 | 9 |
| Build A Bot Hull Trigger__mDTcC8j8 | execution_timeout | 30.162s | 13288 | 379 | 0 |
| Ultimate Balance Strategy__WTmh8KER | execution_timeout | 30.128s | 15681 | 382 | 0 |
| Chande Kroll Stop + ADX filter strategy__vOZX6yfP | execution_timeout | 30.126s | 6981 | 385 | 11 |
| Build A Bot__hEefxAIm | execution_timeout | 30.125s | 13262 | 378 | 3 |
| R19 STRATEGY__2LOPRpQH | execution_timeout | 30.116s | 31819 | 407 | 0 |

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
