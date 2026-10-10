# Research registry of the crypto desk

Every hypothesis, experiment, decision and lesson is a file here, in git. Nothing about a strategy's parameters,
the costs it is tested with or the rules of the learning loop changes without an entry. This is also the trial
registry of family C for the Deflated Sharpe calculation: every configuration evaluated counts
(`config/trial_registry.yaml`).

Where the desk stands, in one page: [`CRYPTO_SUMMARY.md`](CRYPTO_SUMMARY.md).

```
research/
 ├── hypotheses/        HYP-NNNN-<slug>.yaml   what we believe and why
 ├── experiments/       EXP-NNNN-<slug>/       report.md + result.json (immutable once run)
 ├── decisions/         DEC-NNNN-<slug>.md     who decided what, when, based on which experiments
 └── lessons_learned/   LL-NNNN-<slug>.md      what a mistake taught
```

## Ids

The desk was built inside `trading-lab-research`, whose registry numbers every platform's records in one
sequence. The records that are the desk's moved here with their history and **keep their ids**:

| Kind | Ids that moved |
|---|---|
| Hypotheses | HYP-0020, HYP-0021, HYP-0022, HYP-0023, HYP-0024 |
| Decisions | DEC-0012, DEC-0014 to DEC-0022, DEC-0025, DEC-0026, DEC-0027 |
| Experiments | EXP-0016 to EXP-0022 |
| Lessons | LL-0003 to LL-0006 |

Those numbers are never reused, here or there. The gaps in the sequences are the other platform's records, which
did not move. No moved record, and nothing in the desk's code or configuration, cites one of them
(`tests/unit/test_research_registry.py` checks it); a reference to one would be written
`trading-lab-research:<id>`.

**New records written here start `CRY-`**: `CRY-HYP-0001`, `CRY-EXP-0001`, `CRY-DEC-0001`, `CRY-LL-0001`, each
its own sequence. A reference to another repository's record is written `repo:ID`.

## Rules

1. Ids are sequential and never reused. Experiments are append-only: a re-run is a new id.
2. A change to anything frozen (`config/crypto.yaml`, the limits in `config/risk.yaml`) needs a hypothesis, at
   least one experiment and a decision that cites them, written before any affected result is looked at.
3. A hold-out is logged as its own experiment and happens once per frozen configuration.
4. Owner approvals are recorded as decisions.
5. The daily challengers job reads `experiments/EXP-0016-*/result.json` at run time
   (`wt.crypto.challengers.experiment`): that folder must stay where it is.
