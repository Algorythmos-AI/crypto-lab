# DEC-0018: Crypto desk, corrections to the training method (amends DEC-0016 and DEC-0017)

- **Status: PROPOSED.** The owner accepts it by merging the pull request that carries it.
- **Amends:** DEC-0016, section 3, and DEC-0017. Every other part of both stands.
- **Date drafted:** 2026-10-07, after EXP-0017 and EXP-0018 and before the rerun (EXP-0019). The method
  below was committed before EXP-0019 was run.

## Why, stated plainly

The trainer did not do what DEC-0016 says in three places. EXP-0017 and EXP-0018 were both computed with
these defects, so neither result can be used.

1. **Sample weights.** DEC-0016 down-weights overlapping trades by how unique their holding period is. A
   trade that finished inside its first 4-hour bar was given an empty holding period and so full weight. On
   the EXP-0017 data, 169 of 6,997 signals (2.4%, win rate 12%) carried 34% of all weight. Every fit, every
   log-loss and the baseline were dominated by them.
2. **Breadth.** DEC-0016 defines the input as how many of the traded pairs signalled on the bar. Training
   counted signals across all training pairs, and only those whose stop distance passed.
3. **The training universe.** A pair whose download failed was reported as having too short a history, and
   the run went on without it.

**What was seen before this record.** EXP-0017, EXP-0018, and one run of the corrected code without
calibration, made before EXP-0018 was known to exist: 8,858 signals from 30 pairs, no candidate, and for
every setting an interval for kept minus skipped that spans zero. It is not kept as an experiment because
its method (no calibration) is no longer the method.

This is a correction of the code to the registered method, not a new idea. It still changes results after
results were seen, so by DEC-0017's own rule it is the **third attempt** at model selection and is counted
and published as such.

## Decision

1. **Weights.** A trade occupies every bar from its entry bar to the bar its outcome falls in, and never
   fewer than one.
2. **Breadth.** In history as on the desk: the number of traded pairs that met the sleeve's rule on the bar,
   before the stop-distance check. On a training-only pair's own signal it can be 0.
3. **Universe.** A failed download is retried and named. A run without every traded pair, or with fewer
   than 80% of the training pairs, stops.
4. **A fold needs 200 training signals** to be scored. This does not bind on the present data.
5. **Reported, not used for selection:** the effective sample size, the training signals per fold, and the
   difference in mean R between kept and skipped signals with a 95% bootstrap interval over days.
6. **Everything else is unchanged:** the models, their settings, the folds, the embargo, calibration as
   DEC-0017 fixes it, log-loss as the score, the comparison with M0 and the one-standard-error choice.
7. **No fourth attempt.** If EXP-0019 has no candidate, no further change to models, inputs, settings or
   selection is made until a strategy has passed gate C1. If it has one, that model is registered in shadow
   and the checkpoint test of DEC-0016, section 4, decides the rest.

## Not decided here

- Any other model, input or setting.
- Anything about promotion, demotion, drift or challengers.
