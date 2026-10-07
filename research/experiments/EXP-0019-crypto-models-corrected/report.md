# EXP-0019: crypto desk, the first models

- Decision: DEC-0018 (DEC-0016 as amended). Training set: 8858 signals from 30 pairs, 2024-10-07 to 2026-10-07 (data hash `2da0e7de7d83fcdc`).
- Win rate of all signals after costs: 34.4%. Mean R: -0.094.
- Validation: 5 purged walk-forward folds, embargo 30 days. Inputs used: 15.
- Calibration: platt, 3 inner purged folds (DEC-0017). This is attempt 3 at model selection.
- Overlapping trades are down-weighted: the 8858 signals count as about 1982 independent ones. Training signals per fold: 1238, 2205, 3804, 5416, 7148.

## Comparison on signals the model never saw

Kept minus skipped is the difference in mean R, with a 95% interval from a bootstrap over days.

| Model | Settings | Log-loss | Kept | Mean R kept | Skipped | Mean R skipped | Kept minus skipped |
|---|---|---|---|---|---|---|---|
| M0 take everything | - | 0.5933 | 7408 | -0.147 | 0 | - | - |
| M1 logistic regression (best of its kind) | C=0.1 | 0.6061 | 4656 | -0.120 | 2752 | -0.192 | +0.073 (-0.108 to +0.258) |
| M1 logistic regression | C=1 | 0.6073 | 4682 | -0.120 | 2726 | -0.192 | +0.072 (-0.114 to +0.257) |
| M1 logistic regression | C=10 | 0.6088 | 4686 | -0.119 | 2722 | -0.194 | +0.075 (-0.109 to +0.260) |
| M2 boosted trees (best of its kind) | num_leaves=4, n_estimators=100 | 0.6064 | 4612 | -0.262 | 2796 | +0.043 | -0.304 (-0.547 to -0.066) |
| M2 boosted trees | num_leaves=4, n_estimators=300 | 0.6105 | 4703 | -0.128 | 2705 | -0.180 | +0.053 (-0.137 to +0.240) |
| M2 boosted trees | num_leaves=8, n_estimators=100 | 0.6064 | 4651 | -0.178 | 2757 | -0.094 | -0.083 (-0.344 to +0.153) |
| M2 boosted trees | num_leaves=8, n_estimators=300 | 0.6117 | 4605 | -0.147 | 2803 | -0.146 | -0.002 (-0.181 to +0.196) |

## Choice

**No candidate.** No model beat taking every signal on unseen data, so none is registered.

## Notes

- Each signal is followed on its own with its sleeve's exits and costs (`wt.crypto.signals.outcome`).
- A registered model only scores signals in shadow. It acts on nothing until it passes the checkpoint
  test in DEC-0016, section 4, on signals that finish after it was trained.
