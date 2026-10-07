# DEC-0016: Crypto desk, the learning charter

- **Status: ACCEPTED on 2026-10-07.** The owner accepted it by merging the pull request that carries this
  status line, after choosing in chat: the model promotes itself on a preset test; challengers join
  automatically; established ML libraries and a pretrained model are used; and the three sleeves keep trading
  after failing gate C1.
- **Date drafted:** 2026-10-07, after EXP-0016 and before any model was trained or any challenger drawn.
- **Depends on:** DEC-0012 (the desk), DEC-0014 (incubation), DEC-0015 (the three sleeves, gate C1).

## Why

EXP-0016 put the three registered sleeves through two years of history with the desk's own code. All three
failed gate C1: mean R of −0.16 (TREND), −0.15 (BREAK) and −0.34 (DIP) after costs. A round trip costs about
0.9% of a position against stops of 1% to 2%, so each trade pays roughly half an R or more before the market
moves. TREND's entries beat a random entry with the same exits (p = 0.01); the other two did not.

The owner wants the desk to keep trading on paper and to learn from its trades. Learning that changes what
the desk does is exactly where a research process fools itself, so everything automatic is written here, with
its limits, before any of it runs. Nothing in this record can be changed by a result; a change is a new record.

## Decision

### 1. The three sleeves keep trading
TREND, BREAK and DIP continue to open paper trades as incubation although they failed C1 (the owner's record
that DEC-0014, item 3, asks for). They are shown as having failed. Their trades are the desk's source of
labelled signals. Nothing they do is evidence for gate C2.

### 2. What is recorded
- **Every signal** of every sleeve and challenger, with the inputs listed in `config/crypto.yaml`,
  `learning.inputs`, as they stood on the signal bar.
- **Its outcome**, whether or not it was bought. A signal that was refused or skipped is followed with the
  sleeve's own exit rules and costs (a shadow trade).
- **Its label:** which came first, the stop, the target or the time limit, and the result in R after costs.
- The record is a hash-chained ledger, verified and anchored with the desk's journal.

### 3. The models
| Model | Library | Settings tried (and no others) |
|---|---|---|
| M0 | none | takes every signal |
| M1 | scikit-learn, logistic regression, standardised inputs | C in {0.1, 1, 10} |
| M2 | LightGBM | leaves in {4, 8}; trees in {100, 300}; learning rate 0.05; at least 40 samples a leaf; row and column sampling 0.8; L2 5 |
| M3 | M2 with the forecaster's and the regime model's outputs as extra inputs | as M2 |

- **The forecaster** is a pretrained time-series model from Hugging Face (Chronos-Bolt, small, Apache-2.0),
  used without further training, on CPU. Its revision and file hash are recorded when it is installed and it
  is never updated without a record. **The regime model** is a three-state Gaussian mixture (scikit-learn) on
  Bitcoin's daily return and volatility.
- **Training data:** every signal of the registered rules over the history of the pairs in
  `learning.training_pairs` (the eight traded pairs and others used for training only), plus every finished
  live and shadow signal. Overlapping trades are down-weighted by how unique their holding period is.
- **Validation:** five expanding walk-forward folds, purged, with an embargo equal to the longest holding
  period. Scored on log-loss.
- **Selection:** the simplest model (M1, then M2, then M3) whose log-loss is within one standard error of the
  best, provided it beats M0's. If none beats M0, there is no candidate and the desk says so.
- **Retraining:** weekly. A model is registered with a hash of its data, code and settings, and a model card.
- Every new signal is scored before its outcome exists, by a model trained only on outcomes that had finished.

### 4. Promotion, and what a promoted model may do
- **Checkpoints:** after every 60 signals that finished after the model in force was trained, at most 6
  checkpoints for one model lineage. No other look counts.
- **The test at a checkpoint:** mean R of the signals scored at or above the cut-off, minus mean R of those
  below it, is above zero by a one-sided bootstrap over days at 0.05 / 6; **and** the model's Brier score is
  below that of the plain win rate.
- **The cut-off** is the 40th percentile of the model's scores on its own training data, fixed at training.
- **A promoted model** skips a signal scored below the cut-off and halves the size of one scored between the
  40th and the 60th percentile. It never adds a trade, enlarges one, moves a stop or a target, or touches an
  open position. It does not apply to the baseline rule (HYP-0020).
- **Demotion:** at each later checkpoint the same difference is measured on the latest 120 finished signals.
  At or below zero, the model stops acting. Skipped signals are still shadow-traded, so the difference is
  always measurable.
- **Drift:** if the population-stability index of the scores, or of three or more inputs, between the
  training data and the latest 60 signals exceeds 0.25, the model stops acting until the next retraining.
- **Fallback:** if the scorer does not answer in 20 seconds, fails, or its model is older than 14 days, the
  signal is traded as its registered rule says and an alert is raised. M3 falls back to M2 when the
  forecaster is unavailable.

### 5. Challengers
- **The search space** is `learning.challengers.dials` and nothing else: one of the three registered rules as
  the base, with a choice of bar length, lookback, stop distance, target or trailing distance, minimum stop,
  a Bitcoin-trend filter, a volume filter, and whether to skip a pair another sleeve holds. The space was set
  after EXP-0016 and leans to wider stops and longer bars on purpose: that experiment's finding is costs.
- **Each week, at most two:** one that differs from the best current sleeve in exactly one dial, and one drawn
  uniformly from the untested space with a seed made from the ISO week. "Best" is by mean R over backtest and
  live trades together.
- **Registered first.** A challenger's exact rules are appended to a hash-chained ledger before it is
  backtested. It is a trial in family C from that moment, pass or fail.
- **Gate C1 for a challenger** is DEC-0015's, on the same history and costs, with the deflated Sharpe taken
  at 7 plus the number of challengers registered so far.
- **Admission:** a challenger that passes joins the tournament on its own US$10,000 paper book under limits
  `CT`, as incubation. One that fails never trades.
- **Caps:** 6 live challengers, 2 new a week, 60 registered in all. At a cap the generator stops.
- **Retirement:** after 30 closed trades with the upper bound of the 95% interval for mean R below zero; or a
  drawdown beyond 10% of its book; or 60 days with fewer than 5 trades. A retired challenger opens nothing
  more; its book and record stay.

### 6. The owner's switch
A file on the host turns all of this off at once: the model stops acting, no challenger is drawn or
admitted, and live challengers stop opening trades. The three registered sleeves and the baseline continue.
Exits are never stopped by it.

### 7. Never automatic
- Any live order. The desk holds no venue credentials.
- A change to the baseline, to the three registered sleeves, to limits `CT`, to the traded pairs, or to
  anything in this record, including its caps and its tests.
- A challenger outside the search space.
- Counting anything toward gate C2. A strategy's C2 evidence starts the day it passes C1, and a model's
  effect is never evidence for a strategy.

### 8. What is published
On the owner's dashboard: the model in force and its card, the comparison of M0 to M3 including whether the
forecaster helped, the checkpoint in progress, kept against skipped signals, drift, and every challenger with
its rules, its backtest verdict and its record. Results stay labelled incubation.

## Not decided here

- News or sentiment inputs: the desk reads public market data only (ADR 0005); another source needs its own
  record.
- Desk-wide exposure or correlation limits across sleeves.
- Short selling, leverage, or any live order.
