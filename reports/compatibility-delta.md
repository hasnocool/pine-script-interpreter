# Runtime 0.3.1 compatibility delta

This is a coverage comparison, not a profitability claim. Rankings remain
screening results on one short BTC/USDT 1h window.

## Outcome delta

| Outcome | Runtime 0.3.0 | Runtime 0.3.1 | Change |
| --- | ---: | ---: | ---: |
| Completed-trade strategies | 862 | 1,607 | +745 |
| No-order strategies | 2,583 | 4,054 | +1,471 |
| Validation errors | 2,569 | 322 | -2,247 |
| Execution-limit stops | 49 | 95 | +46 |
| Wall-clock timeout stops | 18 | 0 | -18 |
| Runtime errors | 0 | 3 | +3 |
| Parse errors | 0 | 0 | +0 |
| Partial snapshots | 67 | 95 | +28 |

Newly completed-trade strategies: **758**. Thirteen strategies that completed
trades under 0.3.0 became no-order diagnostics or execution-limit stops under
0.3.1, for a net gain of **745** completed-trade strategies.

### Metadata verification note

After the full run, approximation-only markers were tightened for 420 source
files that could reach the affected compatibility branches. Those files were
rerun with the identical 500-candle configuration. The merge was allowed only
after status, trade count, equity, return, drawdown, error, source hash, and
execution-step accounting remained identical; only approximation metadata was
updated. Rankings and trade totals are unchanged by this verification pass.

## Newly eligible examples

| Strategy | Trades | Return | Max drawdown | Approximation markers |
| --- | ---: | ---: | ---: | --- |
| No Nonsense NNFX VP Strategy for Back Testing Baseline jh__WyA5pZCd | 21 | 6523.00% | 8115394.68% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`plotting.non_trading` |
| Random ATR Strategy Bybit__xB5JtK12 | 15 | 4270.38% | 4270.23% | `order.rejected_or_ignored` |
| GOLD EMA Crossover Strategy__FM0NqXZL | 21 | 2570.32% | 2595.23% | `order.rejected_or_ignored` |
| 5M RSI Strategy__z61K5bxD | 53 | 1425.73% | 1838.37% | `order.rejected_or_ignored`<br>`request.security` |
| MA cross strategy__yL42H3Gp | 7 | 1349.63% | 84468.38% | `order.rejected_or_ignored` |
| My Strategy__HjlpiM3j | 7 | 1349.63% | 84468.38% | `order.rejected_or_ignored` |
| [DS]Entry Exit TRADE.V01 Strategy__RO4rB2HF | 16 | 868.48% | 848.84% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`ta.legacy_default_source` |
| QQE Channel Strategy__c7V3XVUC | 8 | 854.02% | 800.44% | `order.rejected_or_ignored` |
| DCA Strategy with Hedging__qgghEAli | 60 | 752.50% | 884.70% | `drawing.handles`<br>`order.rejected_or_ignored` |
| Daily Breakout + Daily Shadow By Rouro__gHF19g7k | 92 | 700.95% | 949.94% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`request.security` |
| EMA Cross Strategy v5 (30 lots) (15 min candle only) safe flip__aglHJmZc | 23 | 448.94% | 51847.36% | `order.rejected_or_ignored` |
| Institution Accumulation Distribution__mLGPDwr9 | 5 | 103.77% | 102.58% | `order.rejected_or_ignored` |
| Ichimoku Long and Short Strategy__7G3ds7ih | 2 | 101.54% | 7945.64% | `drawing.handles`<br>`order.rejected_or_ignored`<br>`ta.legacy_default_source` |
| BTC Intraday Advanced Spot PRO V6__k0yPTqHH | 3 | 100.00% | 103.79% | `order.rejected_or_ignored` |
| 8 30 SMA Pullback + ATR Exits (Crypto)__TlCUJRcP | 22 | 99.90% | 100.38% | `order.rejected_or_ignored` |
| ETH BB + 2 Candles__nUmHknsY | 9 | 98.82% | 104.00% | `order.rejected_or_ignored`<br>`request.security` |
| Bollinger Band Breakout Positional Strategy BN 15M__KulH3L3V | 2 | 98.30% | 101.34% | `order.rejected_or_ignored` |
| Jomy's Gyroscopic Bands__SNEfv33f | 280 | 96.33% | 200.98% | `order.invalid_quantity_ignored`<br>`order.rejected_or_ignored` |
| WMX Keltner Channels strategy__OQ7jRpHl | 4 | 94.84% | 103.34% | `order.rejected_or_ignored` |
| STRATEGY R18 F BTC__hblG106q | 3 | 94.22% | 97.29% | `request.security` |
| Noro's Multima v1.0__kgHNqZB2 | 6 | 88.47% | 106.59% | `order.rejected_or_ignored` |
| DI+ Cross Strategy with ATR SL and 2% TP__3kFxb6wt | 17 | 87.11% | 106.21% | `order.rejected_or_ignored` |
| Donchian BO with SAR & Fixed Target__Ce3leWWr | 7 | 86.87% | 107.77% | `order.rejected_or_ignored` |
| EMA 5 13 Strategy__N18VdyZl | 31 | 83.39% | 108.95% | `order.rejected_or_ignored` |
| Fast Scalper with Stops__Mcwal9oA | 31 | 82.95% | 109.17% | `order.rejected_or_ignored` |

## Interpretation

- The compatibility pass converts validation failures into completed runs only when the runtime can preserve the order semantics or mark the approximation explicitly.
- Unaffordable orders are rejected without mutating broker state; zero or rejected orders do not become fabricated fills.
- External publisher libraries, unsupported drawing/data features, and execution-limit strategies remain diagnostics and are not promoted as validated implementations.
- Review the companion `overall-baseline.md` and `top-100-strategies.md` reports for the complete configuration, candle fingerprint, and blockers.
