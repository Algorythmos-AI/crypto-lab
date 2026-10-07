# EXP-0018: crypto desk, the first models

- Decision: DEC-0016. Training set: 8856 signals from 30 pairs, 2024-10-07 to 2026-10-07 (data hash `72ab1ee8776860ac`).
- Win rate of all signals after costs: 34.4%. Mean R: -0.094.
- Validation: 5 purged walk-forward folds, embargo 30 days. Inputs used: 15.
- Calibration: platt, 3 inner purged folds (DEC-0017). This is attempt 2 at model selection.

## Comparison on signals the model never saw

| Model | Settings | Log-loss | Kept | Mean R kept | Skipped | Mean R skipped |
|---|---|---|---|---|---|---|
| M0 take everything | - | 0.5531 | 7409 | -0.147 | 0 | - |
| M1 logistic regression | C=0.1 | 0.5475 | 4413 | -0.154 | 2996 | -0.137 |
| M1 logistic regression (best of its kind) | C=1 | 0.5457 | 4483 | -0.153 | 2926 | -0.137 |
| M1 logistic regression | C=10 | 0.5500 | 4497 | -0.153 | 2912 | -0.138 |
| M2 boosted trees | num_leaves=4, n_estimators=100 | 0.5483 | 4250 | -0.094 | 3159 | -0.218 |
| M2 boosted trees | num_leaves=4, n_estimators=300 | 0.5494 | 4343 | -0.102 | 3066 | -0.210 |
| M2 boosted trees | num_leaves=8, n_estimators=100 | 0.5501 | 4278 | -0.116 | 3131 | -0.189 |
| M2 boosted trees (best of its kind) | num_leaves=8, n_estimators=300 | 0.5481 | 4465 | -0.140 | 2944 | -0.158 |

## Choice

**M1** (C=1): the simplest model within one standard error of the best that also beats M0.

What it leans on, strongest first:

- `dist_ema50_pct`: -0.5207
- `breadth`: -0.3044
- `rsi`: 0.2534
- `is_trend`: -0.2321
- `dist_ema20_pct`: 0.2295
- `btc_ret_1d`: -0.205
- `is_break`: 0.1863
- `btc_above_sma50`: 0.1152

## Notes

- Each signal is followed on its own with its sleeve's exits and costs (`wt.crypto.signals.outcome`).
- A registered model only scores signals in shadow. It acts on nothing until it passes the checkpoint
  test in DEC-0016, section 4, on signals that finish after it was trained.
