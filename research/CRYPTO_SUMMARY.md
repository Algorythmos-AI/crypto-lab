# Crypto desk summary (2026-10-09)

**Question:** does any rule on the crypto desk show a statistically robust edge after costs, on paper, on public
market data?

**Answer so far: no.** Every registered rule and every variant tried has failed gate C1. The desk trades on
paper as incubation and is useful as a test bench, not as a strategy.

## What was tested

Eight Kraken USD pairs, 4-hour bars, long only, two years to 2026-10-07 (a span in which each coin rose 90% to
575% and then fell 54% to 89%; holding all eight in equal weight returned +11.8% with a 73% drawdown). Costs
assumed: 0.40% taker each way and 5 bps slippage.

| Trial | Record | Experiment | Trades | Mean R after costs | Verdict |
|---|---|---|---|---|---|
| TREND (new high in an uptrend, trailed stop) | DEC-0015 | EXP-0016 | 360 | −0.163 | fails C1; beats random entries (p = 0.01) |
| BREAK (new high on volume, fixed target) | DEC-0015 | EXP-0016 | 346 | −0.147 | fails C1 |
| DIP (pullback in a daily uptrend) | DEC-0015 | EXP-0016 | 87 | −0.336 | fails C1 |
| The three under desk-wide limits | DEC-0019 | EXP-0020 | 663 | −0.138 (desk) | not a gate; drawdown −45.5% to −37.7% |
| Resting-limit entries, per sleeve | DEC-0020 | EXP-0021 | signal level | −0.03, −0.02, −0.18 | all three fail the screen |
| TREND with ranked entries | DEC-0025 | EXP-0022 | 366 | −0.154 | fails C1; +0.009R against TREND (−0.03 to +0.05) |
| Challenger: BREAK, trailed stop, volume filter | DEC-0016 | job, 2026-10-08 | 357 | −0.087 | fails C1 |
| Challenger: TREND, 4 ATR stop, 3 ATR trail | DEC-0016 | job, 2026-10-08 | 257 | −0.044 | fails C1 |

Family C stands at 12 registered trials plus the challengers (2 of 60 used).

## What the trades say

- **A trade costs about a quarter of an R** (0.25R mean; the median stop is 3.9% of price). DEC-0022.
- **TREND and BREAK earn about +0.09R a trade before costs**, with standard errors of 0.06 and 0.08: not
  separable from zero. DIP loses before costs.
- **The book, not the rule, limits trades.** The rules fire about 1,270 times each in two years; about 72% of
  those signals are refused because the book is full or already holds the coin. Choosing which signals fill
  the book made no measurable difference (EXP-0022).
- **Wider stops lose less.** The challenger with a 4 ATR stop lost 0.044R a trade against TREND's 0.163R. That
  is the direction the remaining challengers explore.
- **The interval test can confirm only large edges on this sample:** about 0.16R (TREND), 0.19R (BREAK) and
  0.30R (DIP). A smaller true edge fails more often than it passes.

## Machine learning

| Attempt | Record | Experiment | Result |
|---|---|---|---|
| 1 | DEC-0016 | EXP-0017 | no candidate; superseded (wrong sample weights) |
| 2 calibration added | DEC-0017 | EXP-0018 | logistic model chosen; superseded (wrong sample weights) |
| 3 corrections | DEC-0018 | EXP-0019 | no candidate |
| 4 a margin over the baseline | DEC-0026 | weekly job | no candidate on both data sets seen so far |

No model beats taking every signal by more than its own standard error. One was registered in shadow on
2026-10-08 under the earlier rule, ahead of the baseline by a fifth of its standard error; it acts on nothing
and the next weekly training withdraws it if it finds no candidate. Models, inputs, settings and selection
stay frozen until a rule passes gate C1 (DEC-0018, item 7, as DEC-0026 amends it).

## What a pass would still not show

The pairs were chosen with hindsight; there is one two-year span; stops resolve on hourly bars in history;
costs are assumptions and nothing has been filled on a venue. The venue's published taker fee is 0.80% for a
new account's first US$2,500 of 30-day volume and 0.38% from US$10,000. A challenger that passes C1 must
also hold up on the two years before (DEC-0021), which nothing has touched.

## What happens next without anyone doing anything

- The challenger job draws up to two variants a week until 60 are used, or the owner stops it.
- The learning job follows every signal to its outcome daily and retrains weekly under DEC-0026.
- Nothing here counts toward gate C2. That clock starts the day something passes C1.

## What would change the picture

Each needs the owner's decision and its own record: a short side; a rule of a different kind from "buy
strength"; volatility-scaled sizing; a universe that includes coins that were later delisted.
