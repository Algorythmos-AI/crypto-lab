# DEC-0026: Crypto desk, a model must beat taking every signal by more than its own standard error

- **Status: ACCEPTED on 2026-10-08.** The owner accepts it by merging the pull request that carries this
  status line, having asked in chat for a record that tightens model selection.
- **Amends:** DEC-0016, section 3 ("Selection"), and DEC-0018, item 7, to the extent of this one change.
  Every other part of both stands, including that no other model, input, setting or selection change is
  made until a strategy has passed gate C1.
- **Date drafted:** 2026-10-08, after the desk's first weekly training.

## Why, stated plainly

DEC-0016 chooses the simplest model within one standard error of the best, "provided it beats M0's"
log-loss. Any margin counts. The comparison is rerun every week.

- EXP-0019 (data to 2026-10-07 04:00 UTC): M0 0.5933, best M1 0.6061. No candidate.
- The desk's first weekly training (data to 2026-10-08 08:00 UTC, 28 hours later, 8,857 signals):
  M0 0.5925, M1 0.5869. M1 was chosen and registered in shadow.

The difference that registered it is 0.0056. M1's standard error across the five folds is 0.0253. Its
calibration has a slope of 0.10, which flattens its scores to nearly a constant: it is, to within noise, the
baseline. Its kept-minus-skipped difference in mean R is +0.06 with a 95% interval of −0.14 to +0.27.

A rule that accepts any margin, asked every week about a model that is the baseline plus noise, registers
one about half the time. Nothing it registers can act without passing the checkpoint test (DEC-0016,
section 4), so no trade was at risk. But "a model is in force" then means nothing, and the checkpoint budget
of a lineage is spent on chance.

**This is a change of method made after results were seen.** By DEC-0017's rule it is the fourth attempt at
model selection and is counted and published as such. DEC-0018, item 7, said there would be no fourth until
a strategy passed gate C1; the owner amends that for this one change because it can only make selection
stricter. Under it, neither result above has a candidate.

## Decision

1. **The margin.** A model is a candidate only if its out-of-sample log-loss **plus its own standard error
   across the folds** is below M0's log-loss. With fewer than two scored folds there is no standard error and
   no candidate.
2. **Everything else in the choice is unchanged:** among candidates, the simplest (M1, then M2) whose
   log-loss is within one standard error of the best model's.
3. **It applies to every training from the merge of this record on,** the weekly one included.
4. **The model registered on 2026-10-08** stays in shadow, where it changes nothing, until the next weekly
   training. If that training has no candidate, the model is withdrawn, as the learning job already does
   when a retraining finds none.
5. **Considered and not chosen:** a paired test on the fold-by-fold differences. It is the sharper test, but
   on five folds it rests on four degrees of freedom, and the standard error above is the quantity the
   charter's choice already uses. One definition is kept.

## Not decided here

- Any model, input, setting, fold, embargo, calibration or score.
- Promotion, demotion, drift or challengers.
- How often the model is retrained.
