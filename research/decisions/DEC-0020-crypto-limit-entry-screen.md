# DEC-0020: Crypto desk, a screen for resting-limit entries, and the fees it is judged at

- **Status: ACCEPTED on 2026-10-08.** The owner accepts it by merging the pull request that carries this
  status line, having approved in chat a plan that names this experiment.
- **Depends on:** DEC-0015 (the three sleeves and gate C1), DEC-0016 (signals and their outcomes), DEC-0019.
- **Date drafted:** 2026-10-08, after EXP-0016, EXP-0019 and EXP-0020 and before EXP-0021. The method below
  was committed before EXP-0021 was run.

## Why

EXP-0016's finding is costs. A sleeve buys at the market when its bar closes and pays the taker fee and
slippage on both sides: about 0.9% of the position against stops of 1% to 2%. An order that rests in the book
pays the maker fee and no slippage, and a target is a resting order by nature.

A resting buy is filled only if the price comes back to it. For a breakout, the trades that run away are the
ones it misses, and the ones it catches are disproportionately the failures. Whether the saving outweighs
that can only be measured, and it has to be measured with fills that are not flattered.

Building resting orders into the desk (orders that live across cycles, expire and are journalled) is a large
change to the trading path. This record fixes a cheap screen first. The desk is changed only if the screen
is passed.

## What was found about fees while drafting this

The desk's costs (`config/crypto.yaml`: 0.40% taker each way, 5 bps slippage) were entered as "assumptions
until C1 confirms them against the venue's schedule". They were never confirmed, and the venue's public
endpoint no longer returns its fee tiers. The schedule the venue publishes on its website, read on
2026-10-08, lists for spot:

| 30-day volume | Maker | Taker |
|---|---|---|
| from US$0 | 0.40% | 0.80% |
| from US$2,500 | 0.30% | 0.60% |
| from US$10,000 | 0.22% | 0.38% |
| from US$50,000 | 0.15% | 0.30% |

The page carries more than one table and was read by a script, so the owner should check these figures. If
they are right, the desk's 0.40% is the third tier's taker fee. An account trading as the three books do
would be in that tier or a better one; a new account's first US$10,000 would cost double. **This record does
not change the desk's costs.** It only fixes the fees EXP-0021 is judged at.

## Decision

### 1. The screen (EXP-0021)

For every signal of the three registered rules on the eight traded pairs, over EXP-0016's two years:

1. **The order.** A buy limit at the signal bar's close. No slippage is added.
2. **The fill.** The order is filled at its price by the first hourly bar, within the next strategy bar
   (4 hours), whose low is **below** the limit. A bar that only touches the price does not fill it. If no bar
   does, the signal is **missed**: it is counted and it has no result.
3. **Levels.** The stop and the target are set from the fill price with the sleeve's own rule and the signal's
   ATR; a signal whose stop distance fails the sleeve's limits is skipped, as on the desk.
4. **The fill bar.** If that same bar's low reaches the stop, the trade is stopped out in it. A target is not
   credited in the fill bar: its high may have come before the fill.
5. **After the fill** the trade is followed by `wt.crypto.signals.outcome`, the function that labels every
   signal: the same exits, stop first when a bar touches both.
6. **Fees.** Entry: maker. Exit at the target: maker, no slippage. Every other exit: taker, with slippage.
   - Primary: maker 0.22%, taker 0.40% (the desk's assumed taker fee and the maker fee of the same tier).
   - Stress: slippage at 1.5 times.
   - Reported beside them, not used for the verdict: maker 0.40%, taker 0.80% (the first tier), for the
     limit entries and for the market entries of the same signals.
7. **The comparison** is the same signals entered at the market as the desk does, each followed on its own.
   This is a comparison signal by signal; no book, sizing or limit is involved, on either side.

### 2. The verdict, per sleeve

A sleeve **passes the screen** when, for its filled trades:
- there are at least 30;
- mean R is above zero with the lower end of its 95% interval (bootstrap over days) above zero, at the
  primary fees **and** at the stress.

Passing is not gate C1. For a sleeve that passes:
1. it is confirmed once on the two years before the span (the pairs that have that history), by the same
   test at the primary fees. That history is used for nothing else;
2. only if confirmed are resting orders built into the desk, under a new record, and the sleeve with them is
   put through gate C1 as DEC-0015 defines it.

A sleeve that fails is not tried again with another limit price, waiting time or fill rule.

### 3. Trials

Three configurations are evaluated (one per sleeve). **Family C's trial count is 11** from this record on.

## Not decided here

- The desk's costs, and any change to how the desk or a challenger enters or exits.
- Any model, input or setting (DEC-0018, item 7).
