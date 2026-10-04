# DEC-0012: Crypto desk, pre-registration

- **Status: ACCEPTED on 2026-10-04.** The owner accepted it by merging the pull request that carries this
  status line, after choosing USD pairs in chat.
- **Date drafted:** 2026-10-04, before any backtest of the rebuilt strategy.
- **Amended 2026-10-04, before acceptance:** the pairs are BTC/USD, ETH/USD and SOL/USD (section "Pairs").
- **Source:** the owner's decision of 2026-10-04 to bring `kraken-bot` into the lab (ADR 0005).

## Why

The bot ran outside the lab's rules. Its record, read from its own journal:

- 7 days of observation (2026-04-19 to 2026-04-26), about 672 observations, **0 entries**.
- A synthetic baseline (every bar treated as an entry) that lost on all three pairs.
- Two parameters loosened on 2026-04-26 after seeing that week.
- No exits in the code, labels without fees, and no test of any kind.

Two facts measured on 2026-10-04 bear on the design:

- **Liquidity.** SOL/AUD returns many 15-minute bars with no trades at all. Indicators on such bars are
  undefined, and a simulated fill at the quote is not evidence.
  Over the 7.5 days the venue serves (720 bars each, read 2026-10-04):

  | Pair | Bars with a trade | Median trades per bar | Spread at the time |
  |---|---|---|---|
  | BTC/AUD | 88.6% | 5 | 0.008% |
  | ETH/AUD | 59.2% | 1 | 0.277% |
  | SOL/AUD | 70.3% | 2 | 0.362% |
  | BTC/USD | 100% | 1,144 | 0.0001% |
  | ETH/USD | 100% | 447 | 0.0004% |
  | SOL/USD | 100% | 358 | 0.008% |
- **Fees.** At Kraken's published base tier (0.40% taker, assumed until confirmed) a round trip costs about
  0.8%. With the current 1.0% target and 0.5% stop, a win nets about +0.2% and a loss about −1.3%.

## Decision (proposed)

1. **Trial family C.** Crypto trials are counted in their own family for the Deflated Sharpe Ratio; they do
   not change the equity count (85 in force). The family starts at **4**: the original parameters, the
   2026-04-26 loosening, HYP-0020 as registered here, and the change of pairs from AUD to USD
   (`config/trial_registry.yaml`, `families.C`).

   **Pairs.** BTC/USD, ETH/USD and SOL/USD. The original AUD pairs would fail gate C0 on the figures above, so
   the desk never records on them. The paper book and its limits are therefore in US dollars. No P&L on either
   set of pairs had been computed when this was decided.

2. **HYP-0020 (C-PULLBACK)** is the one registered hypothesis. Its rules and numbers are `config/crypto.yaml`
   at the commit named in the experiment's manifest.

3. **Gates.**
   - **C0, data quality.** Per pair, over at least 14 recorded days: share of 15-minute bars with trades,
     median and 95th-percentile spread, and gaps. A pair is eligible only if at least 90% of bars traded and
     the median spread is at most 0.15%. A pair that fails is dropped; replacing it needs its own decision
     record and counts as a trial.
   - **C1, backtest after costs.** `scripts/crypto_backtest.py`, fees and slippage included, with the
     falsification rules in HYP-0020. Run once per frozen config.
   - **C2, paper evidence.** 50 closed paper trades over at least 30 days with no incident day, on the
     config that passed C1.

4. **Order of work.** The desk is deployed with its kill switch on and records data from the first day. C0 is
   reported from that recording. C1 runs only after C0. The kill switch comes off only after C1 passes and the
   owner says so.

5. **Limits** (`config/risk.yaml`, key `C`, in US$ on a US$10,000 paper account): 50 per entry, 500 open
   exposure, 3 entries per UTC day, entries latched off after a realised loss of 20 in a UTC day. Frozen per
   gate, as for B.

6. **The old week of observations** is kept as private history. It is not evidence for any gate: it has no
   fees, no exits and a parameter change in the middle.

7. **ML** stays in shadow: predictions are logged and never change an order. Promotion needs its own record.

## Not decided here

- Any pair outside BTC/USD, ETH/USD and SOL/USD.
- Any change to target, stop or threshold. Each is a new trial in family C.
