# DEC-0017: Crypto desk, probability calibration for the models (amends DEC-0016)

- **Status: ACCEPTED on 2026-10-07.** The owner accepted it by merging the pull request that carries this
  status line, after saying in chat to write the amendment and rerun.
- **Amends:** DEC-0016, section 3 only. Every other part of DEC-0016 stands.
- **Date drafted:** 2026-10-07, after EXP-0017 and before the rerun (EXP-0018). The method below was committed
  before EXP-0018 was run.

## Why, stated plainly

EXP-0017 was the first model comparison. Neither model beat taking every signal (M0) on out-of-sample
log-loss, so nothing was registered. One model, the boosted trees, did rank signals: the ones it would skip
averaged −0.25R against −0.12R for the ones it would keep, on every setting tried. Its log-loss was still worse
than M0's, because its probabilities were too confident for a 34% win rate that drifts between periods.

DEC-0016's method had no calibration step. That was an omission: the plan the owner approved listed
calibration as standard practice, and the record and the trainer left it out. This record adds it.

**This is a change of method made after a result was seen.** It is therefore the second attempt at model
selection, and is counted and published as such. It is one specific, standard step, fixed here in full. A
further change after EXP-0018 needs another record and would be a third attempt.

## Decision

1. **Calibration.** Every model's probabilities are passed through Platt scaling: `p' = sigmoid(a * logit(p) + b)`.
   - `a` and `b` are fitted on predictions the model made for signals it was not trained on: three inner
     walk-forward folds inside the training data, purged and embargoed exactly as the outer folds are.
   - With fewer than 100 such predictions there is no calibration (`a = 1`, `b = 0`).
   - The calibration is part of the model: it is fitted inside each outer training fold, never on a test block,
     and it is registered and applied with the model.
2. **Everything else is unchanged:** the models, their allowed settings, the folds, the embargo, log-loss as the
   score, the comparison with M0, and the one-standard-error choice.
3. **The data** for the rerun is the same two years, with every training pair the history source answers for.
   EXP-0017 ran on 23 of the 30 pairs because seven did not download; the rerun's pair count is reported.
4. **The cut-offs** (the 40th and 60th percentiles of training scores) are taken from calibrated scores.
   Calibration is monotone, so which signals fall below a cut-off does not change.

## Not decided here

- Any other model, input or setting.
- Anything about promotion, demotion, drift or challengers.
