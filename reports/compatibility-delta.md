# Runtime 0.3.1 compatibility delta

This is a coverage comparison, not a profitability claim. Rankings remain
screening results on one short BTC/USDT 1h window.

## Outcome delta

| Outcome | Runtime 0.3.0 | Runtime 0.3.1 | Change |
| --- | ---: | ---: | ---: |
| Completed-trade strategies | 862 | 2,374 | +1,512 |
| No-order strategies | 2,583 | 3,462 | +879 |
| Validation errors | 2,569 | 91 | -2,478 |
| Execution-limit stops | 49 | 154 | +105 |
| Wall-clock timeout stops | 18 | 0 | -18 |
| Runtime errors | 0 | 0 | +0 |
| Parse errors | 0 | 0 | +0 |
| Partial snapshots | 67 | 154 | +87 |

Newly completed-trade strategies: **1,522**. 10 strategies that completed trades under 0.3.0 became no-order diagnostics or execution-limit stops under 0.3.1, for a net gain of **1,512** completed-trade strategies.

## Metadata verification note

The approximation markers in the final report are part of the runtime result metadata; trade counts, returns, drawdown, and broker state are not promoted when an unsupported branch is encountered.

## Newly eligible examples

| Strategy | Trades | Return | Max drawdown | Approximation markers |
| --- | ---: | ---: | ---: | --- |
| Pump Smart Shorting Strategy__rvIzBoho | 17 | 2477665495.26% | 2770936440.38% | `drawing.handles` |
| Moving Stop Loss mechanism + alerts to MT4 MT5__GRTIMXzJ | 35 | 20924800.00% | 865334117.10% | `order.rejected_or_ignored` |
| [EURUSD60] BB Expansion Strategy__7yPV3OPU | 3 | 20296971.60% | 797981086.00% | `order.rejected_or_ignored`<br>`request.security` |
| RSI Divergence Strategy__ASVRhqFM | 2 | 39289.01% | 47164.19% | `drawing.handles`<br>`order.rejected_or_ignored` |
| Random ATR Strategy Bybit__xB5JtK12 | 15 | 4270.38% | 4270.23% | `order.rejected_or_ignored` |
| GOLD EMA Crossover Strategy__FM0NqXZL | 21 | 2570.32% | 2595.23% | `order.rejected_or_ignored` |
| 5M RSI Strategy__z61K5bxD | 53 | 1425.73% | 1838.37% | `order.rejected_or_ignored`<br>`request.security` |
| MA cross strategy__yL42H3Gp | 7 | 1349.63% | 84468.38% | `order.rejected_or_ignored` |
| My Strategy__HjlpiM3j | 7 | 1349.63% | 84468.38% | `order.rejected_or_ignored` |
| [DS]Entry Exit TRADE.V01 Strategy__RO4rB2HF | 16 | 868.48% | 848.84% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`ta.legacy_default_source` |
| QQE Channel Strategy__c7V3XVUC | 8 | 854.02% | 800.44% | `order.rejected_or_ignored` |
| GoldFinger .007__SItNup0r | 11 | 799.74% | 873.05% | `order.rejected_or_ignored` |
| ChopFlow ATR Scalp Strategy__TgcbEl6W | 27 | 753.45% | 926.39% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`session.time` |
| DCA Strategy with Hedging__qgghEAli | 60 | 752.50% | 884.70% | `drawing.handles`<br>`order.rejected_or_ignored` |
| EMA Cross Strategy v5 (30 lots) (15 min candle only) safe flip__aglHJmZc | 23 | 448.94% | 51847.36% | `order.rejected_or_ignored` |
| R19 STRATEGY__2LOPRpQH | 2 | 108.98% | 96.52% | `request.security` |
| Institution Accumulation Distribution__mLGPDwr9 | 5 | 103.77% | 102.58% | `order.rejected_or_ignored` |
| VWAP Stdev Bands Reversal Strategy__X1tap85S | 7 | 101.75% | 286.35% | `order.rejected_or_ignored`<br>`timeframe.change` |
| Ichimoku Long and Short Strategy__7G3ds7ih | 2 | 101.54% | 7945.64% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`ta.legacy_default_source` |
| BASELINE2)__2zFQot2h | 5 | 100.81% | 101.13% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`request.security` |
| Smart Trend Strategy julzALGO__Sg2qyDDe | 1 | 99.90% | 100.65% | `drawing.handles`<br>`legacy.datetime_numeric`<br>`order.rejected_or_ignored` |
| 8 30 SMA Pullback + ATR Exits (Crypto)__TlCUJRcP | 22 | 99.90% | 100.38% | `order.rejected_or_ignored` |
| LinReg Slope + Acceleration Filter__XhztV83u | 8 | 99.41% | 103.29% | `drawing.handles`<br>`order.rejected_or_ignored` |
| ETH BB + 2 Candles__nUmHknsY | 9 | 98.82% | 104.00% | `order.rejected_or_ignored`<br>`request.security` |
| Bollinger Band Breakout Positional Strategy BN 15M__KulH3L3V | 2 | 98.30% | 101.34% | `order.rejected_or_ignored` |

## Interpretation

- The compatibility pass converts validation failures into completed runs only when the runtime can preserve the order semantics or mark the approximation explicitly.
- Unaffordable orders are rejected without mutating broker state; zero or rejected orders do not become fabricated fills.
- External publisher libraries, unsupported drawing/data features, and execution-limit strategies remain diagnostics and are not promoted as validated implementations.
- Review the companion `overall-baseline.md` and `top-100-strategies.md` reports for the complete configuration, candle fingerprint, and blockers.
