# Overall Pine Backtest Baseline

## Executive summary

This run attempted 6,081 archived Pine strategies against 500 binance BTC/USDT 1h candles. It is intended to establish a broad technical baseline, not to certify strategies as profitable or safe.

- **Run time:** 2m 33s
- **Strategies attempted:** 6,081
- **Produced completed trades:** 273
- **Ran without completed orders:** 4,474
- **Could not be evaluated:** 1,334
- **Detailed shortlist:** See the companion `top-100-strategies.md` report.
- **Candle period:** 2026-09-03T02:00:00+00:00 through 2026-09-23T21:00:00+00:00

## Results at a glance

| Outcome | Count | What it means |
| --- | ---: | --- |
| Backtested with trades | 273 | The runtime ran and closed at least one trade. |
| No orders | 4,474 | The runtime ran but did not close a trade on this data. |
| Validation errors | 1,260 | The current runtime could not evaluate part of the strategy. |
| Execution-limit stops | 38 | The strategy exceeded the safety step budget. |
| Runtime errors | 36 | An unexpected runtime problem stopped the strategy. |
| Parse errors | 0 | The source could not be parsed. |

## Performance snapshot

- **126** strategies with trades finished positive; **147** finished negative; **0** were approximately flat.
- The median return among trade-producing strategies was **-0.01%**; the average was **0.04%**.
- The median maximum drawdown was **1.61%**.
- The strongest eligible result was **anh Manh dep trai\_\_x8rkJOjn** at **25.48%**.
- The weakest trade-producing result was **Turtle Trading Strategy with ATR Stop + Pyramiding\_\_HYco13Su** at **-25.29%**.

## Top candidates

The table below uses raw total return, highest first; lower drawdown breaks ties and requires at least 1 completed trade(s).

| Rank | Strategy | Trades | Return | Max drawdown |
| ---: | --- | ---: | ---: | ---: |
| 1 | anh Manh dep trai__x8rkJOjn | 32 | 25.48% | 0.87% |
| 2 | CNPS3 SMA20 200__WCsZYm59 | 83 | 10.11% | 17.56% |
| 3 | Simple Breakout Strategy santana__pX2ZFAxS | 238 | 5.85% | 1.71% |
| 4 | Decoded Volatility Expansion [Ahtisham]__qgLu0K0v | 451 | 5.11% | 0.16% |
| 5 | Momentum Long + Short Strategy (BTC 3H)__qloyhLBl | 208 | 3.92% | 0.85% |
| 6 | DNSE VN301!, SMA 34 89 Dual Slope Strategy__VFP7QkqV | 91 | 3.68% | 19.35% |
| 7 | Autonomous 5 Minute Robot__NPpsEOFb | 119 | 3.25% | 1.63% |
| 8 | Up Dn Volume Sentiment Strategy__6GReiiLS | 55 | 1.96% | 1.62% |
| 9 | EMA Crossover Strategy with Take Profit and Candle Highlighting__B7umqU7W | 20 | 1.04% | 1.63% |
| 10 | Pramod’s Intraday Strategy__kAckvOaP | 17 | 1.00% | 1.64% |

## Main evaluation blockers

These are the most common reasons a strategy could not be evaluated. They describe coverage gaps in the compact runtime, not necessarily bad Pine source.

| Blocker | Strategies | Share of recorded errors |
| --- | ---: | ---: |
| Unknown Pine identifier or constant | 611 | 45.80% |
| Required numeric value was unavailable | 545 | 40.85% |
| Invalid technical-analysis length | 63 | 4.72% |
| Runaway loop or excessive runtime steps | 38 | 2.85% |
| Unexpected missing object or value | 32 | 2.40% |
| Order exceeded available cash | 25 | 1.87% |
| Unsupported call target | 12 | 0.90% |
| Tuple assignment could not be matched | 2 | 0.15% |
| order quantity must be positive and finite | 2 | 0.15% |
| empty separator | 2 | 0.15% |
| maximum recursion depth exceeded | 1 | 0.07% |
| (34, 'Numerical result out of range') | 1 | 0.07% |

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
- Short entries: allowed
- Close open position at end: no
- Execution-step limit: 250,000
