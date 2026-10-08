# DEC-0022: Crypto desk, a correction: what a trade costs in R, and what the rules earn before costs

- **Status: ACCEPTED on 2026-10-08.** The owner accepts it by merging the pull request that carries this
  status line, having approved in chat a plan that names this correction.
- **Corrects statements in:** DEC-0016 ("Why"), DEC-0020 ("Why"), and the description of the pull request
  that carried DEC-0020. It changes no rule, limit, cost, gate, test or trial count.
- **Date drafted:** 2026-10-08.

## What was wrong

1. **DEC-0016 and DEC-0020 say** a round trip costs "about 0.9% of a position against stops of 1% to 2%", so
   a trade "pays roughly half an R or more before the market moves". The 1% is the smallest stop the sleeves
   allow, not the stop they use. Nobody had measured the stops.
2. **The pull request that carried DEC-0020 said** the registered rules have "roughly no edge before costs".
   That was read off a result after reduced costs and is not what the figures show.

## What the trades show

Measured on EXP-0016's run (the three sleeves, eight pairs, two years, each book alone), trade by trade:
both fees, slippage on the entry and on every exit except a target, over the risk taken at entry.

| | TREND | BREAK | DIP |
|---|---|---|---|
| Closed trades | 360 | 346 | 87 |
| Stop distance, median | 3.9% | 4.1% | 4.0% |
| Cost per trade, mean (median) | 0.25R (0.23R) | 0.24R (0.22R) | 0.24R (0.22R) |
| Mean R after costs (EXP-0016) | −0.163 | −0.147 | −0.336 |
| Mean R before costs (the two above, added) | +0.09 | +0.09 | −0.10 |
| Standard error of the mean, trade by trade | 0.06 | 0.08 | 0.13 |

So: a trade costs about a quarter of an R, not half. TREND and BREAK earn about +0.09R a trade before costs,
which their standard errors do not separate from zero, and costs make them clearly negative. DIP loses
before costs.

This is arithmetic on a result already recorded. It is not an experiment, it evaluates nothing new, and it
is not evidence for any gate.

## What follows, and what does not

- EXP-0016's verdicts stand. All three sleeves failed gate C1 and remain incubation.
- EXP-0021's verdicts stand. Its reading should have been: cheaper entries recover part of the cost and
  leave the result at about zero after costs.
- The cost that matters is cost in R, and it falls as the stop widens. The challengers' wider stops and
  daily bars (DEC-0016, section 5) are the registered test of that.
- From this record on, a backtest report states the mean cost in R, the mean R before costs with its
  standard error, and the return of holding the traded pairs in equal weight over the same span. They are
  reported so that a result is read correctly. They are never part of a verdict.

## Not decided here

- The desk's cost assumptions. The venue's published schedule, read on 2026-10-08, has a taker fee of 0.80%
  from US$0 of 30-day volume, 0.60% from US$2,500 and 0.38% from US$10,000; the desk's 0.40% stands.
- Any rule, model, input or setting.
