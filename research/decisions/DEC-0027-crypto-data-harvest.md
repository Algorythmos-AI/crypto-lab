# DEC-0027: Crypto desk, a data harvest that trades around the clock for the learning data

- **Status: ACCEPTED on 2026-10-10.** The owner accepts it by merging the pull request that carries this
  status line, having approved in chat on 2026-10-10 a plan that names every part below. **Nothing is built
  or run before that merge.**
- **Depends on:** DEC-0012 (the desk), DEC-0015 (the registered rules), DEC-0016 (signals and outcomes),
  DEC-0019 (desk limits).
- **Does not amend** any of them. The baseline, the tournament, its limits, its gates and the model in force
  are untouched by this record.
- **Date drafted:** 2026-10-10.

## Why

The desk is healthy and nearly idle. On 2026-10-10 the dashboard showed every bar cycle run (96 of 96 a day)
and every job `ok`. The baseline had 0 signals in 1,524 bars since 2026-10-04. The three registered rules
had fired 13 times since 2026-10-06, and 5 of those became trades. The model in shadow has 0 of the 60
finished signals its first checkpoint needs.

The tournament is built to be selective, because it asks whether a rule has an edge. The owner's goal for
the paper desk is different: as many labelled paper trades as the desk can make, around the clock, as
training data. Whether those trades make money does not matter to that goal.

This record adds that as a separate thing, so the tournament's question is not changed by it.

## Decision

### 1. What the harvest is
- **Its own paper books** (US$10,000 each), state and hash-chained journal under `var/crypto/harvest/`.
  The journal is one of the crypto desk's ledgers: the nightly backup checks and anchors it.
- **Not evidence.** No gate, tournament result, hypothesis test or decision counts a harvest trade. Every
  harvest row carries `stage: harvest` and `decision: DEC-0027`.
- **The registered rules, numbers unchanged** (`sleeves.trend`, `.break`, `.dip`), run by the same engine as
  the tournament (`wt.crypto.sleeves`), so a rule cannot mean one thing there and another here.

### 2. What differs from the tournament
- **Coins:** the 30 training pairs of the learning charter (`learning.training_pairs`), not 8.
- **Limits** (`config/risk.yaml`, key `CH`): 0.5% risked per trade, at most 5% of the book in one position,
  up to 30 positions. A signal is then almost never refused because the book is full.
- **No desk-wide limits.** DEC-0019 belongs to the tournament; harvest books neither count toward it nor
  are limited by it.
- **Bar lengths** (`harvest.timeframes`): 4-hour first; 1-hour copies of the same rules are added by
  section 4.

### 3. What stays the same
- **Paper only.** Kraken's public market data, simulated fills, no credentials, no private endpoint, no
  order route.
- **The desk's switches stop it.** The kill switch, the evidence-chain flag and the shadow role refuse harvest
  entries as they refuse the tournament's. The owner also has a harvest-only switch
  (`make crypto-harvest-off` / `-on`). No switch ever stops an exit.
- **It runs last in the bar cycle**, after the baseline and the tournament, on its own budget of public calls,
  and a fault in it is reported and leaves their results as they were.

### 4. Built in three steps, each its own pull request under this record
1. The harvest books on 30 coins, the limits above, the switch, the journal.
2. 1-hour copies of the three rules, and an **exploration book**: one paper entry an hour in a coin chosen at
   random, with a volatility stop and target and a time exit. A model learns little from a sample of only
   the moments a rule liked; the exploration book is the sample of ordinary moments.
3. Outcomes of harvest signals labelled every hour, a feature row for every coin on every 15-minute bar
   whether traded or not, and a Data harvest panel on the dashboard.

### 5. What this record does not do
- **It does not feed the model in force.** DEC-0018, item 7, stands: the weekly training, its inputs and the
  checkpoint tests are unchanged. The harvest's labelled rows are stored for research; using them to train
  or test the model in force needs its own record.
- **It does not retire the baseline.** The baseline keeps running and recording. The dashboard may show it
  less prominently; that is presentation, not a decision about the rule.
- **It is not an edge.** A rising or falling harvest book means nothing about a rule; the dashboard says so
  wherever it shows one.

## Expected volume (an estimate, not a measurement)

From the tournament's 13 signals in about 4.5 days on 8 coins: roughly 10 to 15 signals a day after step 1,
and roughly 80 to 100 a day after step 2. Step 3's panel measures the real number.
