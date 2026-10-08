# EXP-0022: crypto desk, TREND with ranked entries (HYP-0024), gate C1

- Decision: DEC-0025. Family C trial count used for the deflated Sharpe: 12.
- Span: 2024-10-07 to 2026-10-07, 8 pairs (data hash `3a556ccdfce9996a`), costs as EXP-0016; the stress run uses 1.5x slippage.
- Random-entry control: 100 runs, same exits, sizing, costs and ordering.
- The registered TREND rerun beside it reproduces EXP-0016: **yes**.

## Trades per month (reported first)

366 trades from 1276 signals: 15.26 a month.

## Result

| | Ranked (HYP-0024) | Registered TREND |
|---|---|---|
| Trades | 366 | 360 |
| Win rate | 29.0% | 28.1% |
| Mean R after costs | -0.154 | -0.163 |
| CI95 at 1.5x slippage | [-0.317, +0.004] | - |
| Profit factor | 0.709 | 0.692 |
| Deflated Sharpe | 0.000 | - |
| Random-entry control p | 0.0099 | - |
| Largest drawdown | -50.87% | -52.88% |

**Verdict (DEC-0015): FAILED: ci_not_above_zero_at_1.5x_slippage, deflated_sharpe, profit_factor**

## For reading the result (never part of the verdict)

- Ranked minus registered TREND, mean R: +0.009 (95% interval -0.031 to +0.051).
- Cost per trade: 0.243R mean (0.222R median). Mean R before costs: +0.089 (standard error 0.064).
- Holding the 8 pairs in equal weight over the span, no costs: +11.8%, largest drawdown -73.3%.
- Entries by pair, ranked: ADA/USD 34, AVAX/USD 43, BTC/USD 57, DOGE/USD 34, ETH/USD 52, LINK/USD 56, SOL/USD 53, XRP/USD 37.

## Confirmation on the two years before (DEC-0021)

Not run: only a variant that passes gate C1 touches that history.
