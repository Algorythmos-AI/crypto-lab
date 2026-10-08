# DEC-0025: Crypto desk, one variant that chooses which signals fill a full book (HYP-0024)

- **Status: ACCEPTED on 2026-10-08.** The owner accepts it by merging the pull request that carries this
  status line, having approved in chat a plan that names this variant. **Nothing is built or run before
  that merge.**
- **Depends on:** DEC-0015 (TREND and gate C1), DEC-0016 (signals), DEC-0019 (desk limits), DEC-0021
  (confirmation), DEC-0022 (the measurements quoted below).
- **Date drafted:** 2026-10-08.

## Why

Over EXP-0016's two years the TREND rule fired 1,276 times and its book took 362 of them. Of the rest, 761
were refused because the book already held its three positions and 464 because it already held the coin.
The book is the filter, not the rule.

Which signals get in is decided by nothing in particular. Pairs are looked at in the order the config lists
them, and each is entered as soon as it is looked at. When several fire on one bar, the first listed win:
BTC, ETH and SOL took 68, 59 and 59 of TREND's entries; the other five took 23 to 48 each.

That order was never a decision. This record tests one deliberate order in its place.

## What has already been seen, stated so the choice below cannot be tuned to it

- EXP-0016's mean R per pair for TREND (BTC −0.30, ETH −0.14, SOL −0.23, XRP −0.04, ADA +0.06, DOGE +0.24,
  LINK −0.24, AVAX −0.30), on 23 to 68 trades each. These are too few to rank coins by and are not used.
- The entry counts and refusal counts above.
- That TREND's entries beat random entries with the same exits (p = 0.01), and BREAK's and DIP's did not.

The measure below is the textbook one for this situation, relative strength, and was chosen for that reason.
It is disclosed that the coins with the better results above are also the more volatile ones, which a
return-based ranking will tend to favour. The test below is on the same two years, so that overlap is a
reason to read a pass with caution; the confirmation on earlier history (DEC-0021) is the guard.

## Decision

### 1. The variant (trial C-TREND-R)
- **The rule, the stop, the trail and the exits are TREND's, unchanged** (`config/crypto.yaml`,
  `sleeves.trend`), on the eight traded pairs, on its own US$10,000 paper book under limits `CT`.
- **The only difference:** on each 4-hour close every pair is looked at first. The signals that fired are
  then tried in order of the pair's **30-bar return on the signal bar** (the input `ret_30`), highest first.
  A signal with no such value comes after those that have one, in the listed order. Each is entered or
  refused by the book's own limits exactly as now.
- It is a fixed rule on one number the desk already records. It is not a model, and DEC-0018, item 7, is
  not touched.
- **Only TREND.** BREAK fires on most of the same bars and its entries did not beat random ones. A second
  variant would add a trial without adding a question.

### 2. The test
- **Gate C1 as DEC-0015 defines it**, on EXP-0016's span, data and costs, the variant alone on its book, with
  the random-entry control ordered by the same measure.
- **Reported beside it, not part of the verdict:** the registered TREND on the same span (EXP-0016), the
  difference in mean R between the two with a 95% bootstrap interval over days, and the lines DEC-0022 asks
  for (cost in R, R before costs, holding the pairs).
- **If it passes C1** it is run once on the two years before (DEC-0021). Only if confirmed does it join the
  tournament, as incubation, beside TREND and not in its place.
- **If it fails,** no other ordering, lookback or measure is tried under this record.

### 3. Building it
- Entries that wait until every pair has been looked at already exist for a model that is acting
  (`Pending` and `settle` in `wt.crypto.sleeves`). The variant uses that path.
- A test must show that the three registered sleeves make exactly the trades they make today, live and in
  the backtest, with the variant present and absent.

### 4. Trials
One configuration is evaluated. **Family C's trial count is 12** from this record on.

## Not decided here

- Any change to TREND, BREAK, DIP, their limits, the challengers' search space or the desk-wide limits.
- Any model, input or setting.
- Ranking by anything else, or ranking for any other sleeve.
