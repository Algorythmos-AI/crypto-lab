# DEC-0019: Crypto desk, limits across the whole desk, and what a model lineage is (amends DEC-0015 and DEC-0016)

- **Status: ACCEPTED on 2026-10-08.** The owner accepts it by merging the pull request that carries this
  status line, having approved in chat a plan that names both parts.
- **Amends:** DEC-0015 (how the sleeves' limits apply) and DEC-0016, section 4 (how checkpoints are counted).
  Every other part of both stands.
- **Date drafted:** 2026-10-08, after EXP-0016 and EXP-0019 and before EXP-0020. The limits and the measures
  below were committed before EXP-0020 was run.

## Why

**The desk makes one bet several times.** Each sleeve has its own paper book and its own limits (`CT`), and no
sleeve knows what the others hold. The first two live trades were TREND and BREAK buying the same coin on
the same bar. Three books at 1% risk each, three positions each, can put 9% of the desk at risk on coins that
move together. Nothing limits the desk as a whole.

**Checkpoints cannot be reached as DEC-0016 words them.** Section 4 counts "signals that finished after the
model in force was trained". The model is retrained weekly and the eight pairs produce about 26 signals a
week, so the count returns to zero before it reaches 60. The section already speaks of "one model lineage"
without saying what one is.

## Decision

### 1. Limits across the desk (`config/risk.yaml`, key `CD`)

They apply to every book of the tournament together: the three registered sleeves and every live challenger.
The baseline (HYP-0020) is not counted and not limited by them.

1. **One position per coin.** A sleeve may not open a coin that any other book of the tournament holds.
2. **Open risk.** An entry is refused when the risk already open across the tournament's books, plus the new
   trade's risk, would exceed **3%** of their combined equity. Open risk is the loss at each position's stop as
   it stands (never below zero); the new trade's risk is its quantity times the distance to its stop.
3. **Order.** Sleeves are evaluated in their fixed order (TREND, BREAK, DIP, then challengers in the order they
   were admitted). When two fire on the same coin on the same bar, the first takes it.
4. **They only refuse entries.** No exit, stop or target is touched. A refused signal is recorded with the
   reason (`desk_coin`, `desk_risk`) and followed to its outcome as a shadow trade, like any refused signal.
5. **Why 3%.** It is three trades at the sleeves' own 1%: the most one sleeve may hold alone under `CT`. The
   desk as a whole is held to what one book was already allowed.

No correlation estimate is used. The eight coins move together closely enough that grouping them by a rolling
correlation would put most of them in one group most of the time, and it would add a calculation that can
fail inside the trading cycle. The two rules above bound the same thing with nothing to estimate.

### 2. What EXP-0020 measures, and what it does not decide

EXP-0020 runs the three registered sleeves over EXP-0016's history twice through the desk's code, once
without and once with the limits, and reports for each:

- trades, and entries refused by each of the two rules;
- mean R per trade with a 95% bootstrap interval, per sleeve and for the desk;
- the desk's largest drawdown and worst UTC day, on the three books' combined equity;
- the share of 4-hour bars on which two or more books held the same coin.

The limits are a risk control and are adopted by this record, not by that result. EXP-0020 is reported so that
their cost and effect are known. It changes no gate C1 verdict: the sleeves failed C1 and remain incubation.

The tournament under these limits is one more configuration evaluated on this history. **Family C's trial
count is 8** from this record on.

### 3. A model lineage (DEC-0016, section 4)

- A **lineage** is the series of weekly retrainings of one model kind with one set of settings.
- Every signal is scored once, when it fires, by the version in force at that moment, and that score and
  that version's cut-off are what count for it. A later version never rescores it. Every score is therefore
  made by a model that had not seen the signal's outcome.
- **Checkpoints** are counted over the finished signals scored by any version of the lineage: the first at
  60, then every 60, at most six. The test at each is section 4's, unchanged.
- **The demotion window** is the latest 120 finished signals scored by the lineage.
- A **new lineage** starts only when the weekly selection chooses a different kind or different settings. Its
  count starts at zero. The number of lineages started is published with the model.

## Not decided here

- How entries are made or what they cost.
- Any model, input or setting (DEC-0018, item 7, stands).
- Expected shortfall or any other figure on the dashboard.
