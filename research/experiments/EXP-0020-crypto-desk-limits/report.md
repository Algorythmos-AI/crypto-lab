# EXP-0020: crypto tournament, the desk-wide limits

- Decision: DEC-0019. Span: 2024-10-07 to 2026-10-07, 8 pairs (data hash `3a556ccdfce9996a`), costs as EXP-0016.
- Limits: one position per coin across the books; open risk at most 3.0% of their combined equity.
- The run without limits reproduces EXP-0016's trades: **yes**.

## The desk (three books together)

| | Each book alone | Under the limits |
|---|---|---|
| Entries | 797 | 665 |
| Refused: coin already held by another book | - | 860 |
| Refused: open risk | - | 0 |
| Closed trades | 793 | 663 |
| Mean R (95% interval over days) | -0.175 (-0.311 to -0.035) | -0.138 (-0.278 to +0.005) |
| Profit and loss, US$ | -10,234 | -7,405 |
| Largest drawdown | -45.49% | -37.66% |
| Worst UTC day | -2.49% | -2.83% |
| Share of bars with one coin in two books | 26.0% | 0.0% |

## Per sleeve

| Sleeve | Trades alone | Mean R alone | Trades limited | Mean R limited |
|---|---|---|---|---|
| TREND | 360 | -0.163 (-0.319 to +0.000) | 328 | -0.161 (-0.311 to +0.007) |
| BREAK | 346 | -0.147 (-0.337 to +0.046) | 248 | -0.040 (-0.264 to +0.184) |
| DIP | 87 | -0.336 (-0.638 to -0.024) | 87 | -0.336 (-0.638 to -0.024) |

## Reading it

- The limits are a risk control adopted by DEC-0019; this result does not decide them.
- It changes no gate C1 verdict. The sleeves failed C1 (EXP-0016) and remain incubation.
- Sleeves are evaluated in a fixed order, so the first to fire on a coin takes it.
