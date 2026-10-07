# EXP-0021: crypto desk, resting-limit entries screened on the registered rules' signals

- Decision: DEC-0020. Span: 2024-10-07 to 2026-10-07, 8 pairs (data hash `3a556ccdfce9996a`).
- A buy limit at the signal bar's close, filled only by an hourly bar that trades below it within 4 hours. Signal by signal; no book or sizing on either side.
- Mean R is per trade, with a 95% interval from a bootstrap over days.

## Primary fees: maker 0.22%, taker 0.40%, 5 bps slippage

| Sleeve | Signals | Filled | Missed | Limit entry, mean R | Market entry, mean R | Market R of the filled | Market R of the missed |
|---|---|---|---|---|---|---|---|
| TREND | 1274 | 1253 | 21 | -0.028 (-0.213 to +0.155) | -0.080 (-0.261 to +0.104) | -0.091 (-0.280 to +0.091) | +0.568 (+0.075 to +1.194) |
| BREAK | 1252 | 1229 | 23 | -0.024 (-0.212 to +0.158) | -0.088 (-0.269 to +0.094) | -0.099 (-0.279 to +0.075) | +0.488 (-0.163 to +1.137) |
| DIP | 127 | 126 | 1 | -0.179 (-0.530 to +0.172) | -0.249 (-0.596 to +0.097) | -0.262 (-0.613 to +0.087) | +1.347 |

## Stress: the same fees, slippage at 1.5 times

| Sleeve | Signals | Filled | Missed | Limit entry, mean R | Market entry, mean R | Market R of the filled | Market R of the missed |
|---|---|---|---|---|---|---|---|
| TREND | 1274 | 1253 | 21 | -0.035 (-0.219 to +0.148) | -0.096 (-0.277 to +0.089) | -0.107 (-0.295 to +0.074) | +0.554 (+0.061 to +1.181) |
| BREAK | 1252 | 1229 | 23 | -0.028 (-0.216 to +0.154) | -0.096 (-0.277 to +0.086) | -0.107 (-0.292 to +0.076) | +0.485 (-0.167 to +1.136) |
| DIP | 127 | 126 | 1 | -0.183 (-0.535 to +0.169) | -0.254 (-0.601 to +0.110) | -0.266 (-0.616 to +0.097) | +1.347 |

## Reported only: the venue's first tier, maker 0.40%, taker 0.80%

| Sleeve | Signals | Filled | Missed | Limit entry, mean R | Market entry, mean R | Market R of the filled | Market R of the missed |
|---|---|---|---|---|---|---|---|
| TREND | 1274 | 1253 | 21 | -0.185 (-0.372 to -0.000) | -0.295 (-0.482 to -0.108) | -0.306 (-0.498 to -0.122) | +0.343 (-0.154 to +0.988) |
| BREAK | 1252 | 1229 | 23 | -0.152 (-0.345 to +0.032) | -0.296 (-0.476 to -0.114) | -0.307 (-0.488 to -0.134) | +0.285 (-0.362 to +0.936) |
| DIP | 127 | 126 | 1 | -0.307 (-0.665 to +0.050) | -0.454 (-0.801 to -0.110) | -0.468 (-0.817 to -0.119) | +1.193 |

## Verdict (DEC-0020, section 2)

- **TREND: fails** (ci_not_above_zero_primary, ci_not_above_zero_stress)
- **BREAK: fails** (ci_not_above_zero_primary, ci_not_above_zero_stress)
- **DIP: fails** (ci_not_above_zero_primary, ci_not_above_zero_stress)

## Reading it

- The last two columns are the same signals bought at the market, split by whether the resting order would have been filled. They show what a resting order selects for.
- Passing is not gate C1, and a sleeve that fails is not tried again with another fill rule.
