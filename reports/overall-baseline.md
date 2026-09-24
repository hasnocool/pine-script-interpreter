# Overall Pine Backtest Baseline

## Executive summary

This run attempted 6,081 archived Pine strategies against 500 binance BTC/USDT 1h candles. It is intended to establish a broad technical baseline, not to certify strategies as profitable or safe.

- **Run time:** 1h 17m 0s
- **Runtime version:** `0.3.1`
- **Strategies attempted:** 6,081
- **Produced completed trades:** 1,962
- **Ran without completed orders:** 3,859
- **Could not be evaluated:** 260
- **Saved partial interrupted results:** 127
- **Used an explicit approximation:** 3,777
- **Used a local Pine library:** 70
- **Duplicate source groups:** 0
- **Detailed shortlist:** See the companion `top-100-strategies.md` report.
- **Candle period:** 2026-09-03T02:00:00+00:00 through 2026-09-23T21:00:00+00:00
- **Candle fingerprint:** `c080a786dc02f88178034059126e08a622a2d17042b2131e23d5da6c5b728292`
- **Candle source:** `converted-legacy-list-cache`

## Results at a glance

| Outcome | Count | What it means |
| --- | ---: | --- |
| Backtested with trades | 1,962 | The runtime ran and closed at least one trade. |
| No orders | 3,859 | The runtime ran but did not close a trade on this data. |
| Validation errors | 133 | The current runtime could not evaluate part of the strategy. |
| Execution-limit stops | 127 | The strategy exceeded the safety step budget. |
| Wall-clock timeout stops | 0 | The strategy exceeded the wall-clock budget; a partial snapshot may be saved. |
| Runtime errors | 0 | An unexpected runtime problem stopped the strategy. |
| Parse errors | 0 | The source could not be parsed. |

## Performance snapshot

- **561** strategies with trades finished positive; **903** finished negative; **498** were approximately flat.
- The median return among trade-producing strategies was **0.00%**; the average was **1228130.76%**.
- The median maximum drawdown was **1.81%**.
- The strongest eligible result was **Pump Smart Shorting Strategy\_\_rvIzBoho** at **2477665495.26%**.
- The weakest trade-producing result was **4H CCI Strategy 1.4\_\_27DWrT5B** at **-29430038.20%**.

## Top candidates

The table below uses raw total return, highest first; lower drawdown breaks ties and requires at least 1 completed trade(s).

| Rank | Strategy | Trades | Return | Max drawdown |
| ---: | --- | ---: | ---: | ---: |
| 1 | Pump Smart Shorting Strategy__rvIzBoho | 17 | 2477665495.26% | 2770936440.38% |
| 2 | [EURUSD60] BB Expansion Strategy__7yPV3OPU | 3 | 20296971.60% | 797981086.00% |
| 3 | Tomukas Scale In V2__14l4u5gJ | 4 | 35735.39% | 31005.17% |
| 4 | No Nonsense NNFX VP Strategy for Back Testing Baseline jh__WyA5pZCd | 21 | 6523.00% | 8115394.68% |
| 5 | Random ATR Strategy Bybit__xB5JtK12 | 15 | 4270.38% | 4270.23% |
| 6 | GOLD EMA Crossover Strategy__FM0NqXZL | 21 | 2570.32% | 2595.23% |
| 7 | Triple Quad Frosty v4.5__O3qQrueT | 149 | 2436.57% | 4037.52% |
| 8 | 5M RSI Strategy__z61K5bxD | 53 | 1425.73% | 1838.37% |
| 9 | MA cross strategy__yL42H3Gp | 7 | 1349.63% | 84468.38% |
| 10 | My Strategy__HjlpiM3j | 7 | 1349.63% | 84468.38% |

## Main evaluation blockers

These are the most common reasons a strategy could not be evaluated. They describe coverage gaps in the compact runtime, not necessarily bad Pine source.

| Blocker | Strategies | Share of recorded errors |
| --- | ---: | ---: |
| Runaway loop or excessive runtime steps | 127 | 48.85% |
| unknown Pine builtin | 38 | 14.62% |
| imported Pine library was not found | 33 | 12.69% |
| Unknown Pine identifier or constant | 18 | 6.92% |
| Required numeric value was unavailable | 17 | 6.54% |
| unknown strategy member | 4 | 1.54% |
| circular Pine library import | 2 | 0.77% |
| long stop must be below limit | 2 | 0.77% |
| Unsupported call target | 2 | 0.77% |
| Chart timeframe must be BELOW the signal timeframe. Recommended | 1 | 0.38% |
| The size of the real (array x) and imaginary (array y) parts of the input does n | 1 | 0.38% |
| This script only works on the daily timeframe (D). | 1 | 0.38% |

## Interrupted-run snapshots

These rows preserve accounting through the last completed bar. They are diagnostics only and are never eligible for the ranking.

| Strategy | Stop reason | Completed bars | Completed trades | Final equity | Open side |
| --- | --- | ---: | ---: | ---: | --- |
| Scalp Signal Bot 5 min v3.0.1__1BmQAGQi | execution_limit | 494 | 43 | 9,993.15 | flat |
| VWAP Strategy__4HZ8MOod | execution_limit | 489 | 0 | 10,000.00 | flat |
| VWAP Strategy__Nxo4ELTq | execution_limit | 489 | 0 | 10,000.00 | flat |
| VWAP Strategy__v5lj2Zyc | execution_limit | 489 | 0 | 10,000.00 | flat |
| VWAP Strategy__WhqvxciX | execution_limit | 489 | 0 | 10,000.00 | flat |
| supertrend advance__Ld0TePxg | execution_limit | 486 | 0 | 10,000.00 | flat |
| Liquidity Sweep Tracker Smart Money Stop Hunts__On7JaUut | execution_limit | 482 | 0 | 100,000.00 | flat |
| BTC Future Gamma Weighted Momentum Model (BGMM)__ksy34iRO | execution_limit | 480 | 0 | 10,000.00 | flat |
| Daily Bias 5 Min by sam86@live.com__kZzDTCDd | execution_limit | 465 | 4 | 10,000.00 | flat |
| PMax Explorer STRATEGY & SCREENER__nHGK4Qtp | execution_limit | 449 | 0 | 10,000.00 | flat |

## Slowest executions

These wall-clock measurements are profiling signals, not performance results.

| Strategy | Status | Elapsed | Execution steps | Partial bars | Partial trades |
| --- | --- | ---: | ---: | ---: | ---: |
| trend Screener List1__7vCW7aKc | no_orders | 15.991s | 163660 | n/a | n/a |
| trend Screener downtrend__4BFsvEy2 | no_orders | 14.448s | 163740 | n/a | n/a |
| Smooth Moving Average Ribbon [STRATEGY] @PuppyTherapy__x9Qk7yMI | execution_limit | 11.394s | 250001 | 105 | 0 |
| Smooth Moving Average [STRATEGY] @PuppyTherapy__nSjqOv9m | execution_limit | 10.342s | 250001 | 251 | 0 |
| CleanTradeQuantum v7.2 OB FVG INTEGRATED__JeRCEqWU | execution_limit | 9.346s | 250001 | 442 | 0 |
| RSI Overbought Oversold Divergence Strategy w Buy Sell Signals__d92hvFQx | execution_limit | 9.073s | 250001 | 390 | 0 |
| Oscillating Market Case Study__N9bH0tbl | execution_limit | 8.775s | 250001 | 391 | 1 |
| APEX Tester Buy Sell Strategies Basic BACKTESTER__hRcO3u2d | execution_limit | 8.769s | 250001 | 135 | 0 |
| MASU+ v8.2 (DC+NW+ELM+VIX) [1H]__eEMttmMz | execution_limit | 8.534s | 250001 | 104 | 0 |
| Fine Tune Inputs Fourier Smoothed Hybrid Volume Spread Analysis__CHhNM62j | no_orders | 8.151s | 90745 | n/a | n/a |

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
