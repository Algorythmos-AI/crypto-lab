# EXP-0016: crypto tournament sleeves, gate C1

- Decision: DEC-0015. Family C trial count used for the deflated Sharpe: 7.
- Span: 2024-10-07 to 2026-10-07, 8 pairs, hourly history (data hash `3a556ccdfce9996a`).
- Costs: 0.4% taker each way, 5 bps slippage; the stress run uses 1.5x slippage.
- Random-entry control: 100 runs per sleeve, same exits, sizing and costs.

## Trades per month (reported first)

| Sleeve | Signals | Trades | Per month |
|---|---|---|---|
| trend | 1276 | 360 | 15.01 |
| break | 1256 | 346 | 14.43 |
| dip | 131 | 87 | 3.63 |

## Result

| Sleeve | Win rate | Mean R | CI95 (1.5x slip) | Profit factor | Deflated Sharpe | Control p | Max DD | Verdict |
|---|---|---|---|---|---|---|---|---|
| trend | 28.1% | -0.163 | [-0.333, -0.013] | 0.692 | 0.000 | 0.0099 | -52.88% | FAILED: ci_not_above_zero_at_1.5x_slippage, deflated_sharpe, profit_factor |
| break | 36.7% | -0.147 | [-0.342, +0.040] | 0.808 | 0.001 | 0.1386 | -54.79% | FAILED: ci_not_above_zero_at_1.5x_slippage, deflated_sharpe, no_better_than_random_entry, profit_factor |
| dip | 35.6% | -0.336 | [-0.643, -0.037] | 0.563 | 0.000 | 0.8911 | -30.51% | FAILED: ci_not_above_zero_at_1.5x_slippage, deflated_sharpe, no_better_than_random_entry, profit_factor |

## Agreement between the history and Kraken (4-hour closes)

| Pair | Bars compared | Median | 95th percentile | Largest |
|---|---|---|---|---|
| BTC/USD | 720 | 0.0078% | 0.0308% | 0.0639% |
| ETH/USD | 720 | 0.0095% | 0.0389% | 0.1158% |
| SOL/USD | 720 | 0.0132% | 0.052% | 0.1818% |
| XRP/USD | 720 | 0.0119% | 0.0502% | 0.1589% |
| ADA/USD | 720 | 0.0382% | 0.1461% | 0.447% |
| DOGE/USD | 720 | 0.0252% | 0.0933% | 0.2397% |
| LINK/USD | 720 | 0.0328% | 0.1131% | 0.3502% |
| AVAX/USD | 720 | 0.0457% | 0.1525% | 0.3957% |

## Notes

- The backtest calls the desk's own `sleeves.step_pair`: entry rules, levels, sizing, limits and exit
  resolution are the code that trades. Stops and targets resolve on hourly bars, stop first.
- The daily-loss latch is cleared at the next UTC day; on the desk the owner clears it.
- A sleeve that fails stops opening paper trades unless the owner records otherwise (DEC-0014).
