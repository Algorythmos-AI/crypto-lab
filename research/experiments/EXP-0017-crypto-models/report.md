# EXP-0017: crypto desk, the first models

- Decision: DEC-0016. Training set: 6986 signals from 23 pairs, 2024-10-07 to 2026-10-07 (data hash `a11f03e110d3e187`).
- Win rate of all signals after costs: 34.0%. Mean R: -0.108.
- Validation: 5 purged walk-forward folds, embargo 30 days. Inputs used: 15.

## Comparison on signals the model never saw

| Model | Settings | Log-loss | Kept | Mean R kept | Skipped | Mean R skipped |
|---|---|---|---|---|---|---|
| M0 take everything | - | 0.5564 | 5843 | -0.167 | 0 | - |
| M1 logistic regression (best of its kind) | C=0.1 | 0.6316 | 3692 | -0.176 | 2151 | -0.150 |
| M1 logistic regression | C=1 | 0.6449 | 3680 | -0.174 | 2163 | -0.155 |
| M1 logistic regression | C=10 | 0.6498 | 3685 | -0.172 | 2158 | -0.157 |
| M2 boosted trees (best of its kind) | num_leaves=4, n_estimators=100 | 0.6078 | 3719 | -0.117 | 2124 | -0.254 |
| M2 boosted trees | num_leaves=4, n_estimators=300 | 0.6638 | 3832 | -0.116 | 2011 | -0.262 |
| M2 boosted trees | num_leaves=8, n_estimators=100 | 0.6289 | 3749 | -0.115 | 2094 | -0.259 |
| M2 boosted trees | num_leaves=8, n_estimators=300 | 0.7062 | 3905 | -0.140 | 1938 | -0.219 |

## Choice

**No candidate.** No model beat taking every signal on unseen data, so none is registered.

## Notes

- Each signal is followed on its own with its sleeve's exits and costs (`wt.crypto.signals.outcome`).
- A registered model only scores signals in shadow. It acts on nothing until it passes the checkpoint
  test in DEC-0016, section 4, on signals that finish after it was trained.
