# DEC-0015: Crypto desk, three more strategies, pre-registered

- **Status: ACCEPTED on 2026-10-06.** The owner accepted it by merging the pull request that carries this
  status line, after choosing the tournament and 1% risk per trade in chat.
- **Date drafted:** 2026-10-06, before any backtest or paper result of these strategies. No price history
  was examined to choose the numbers below; they are conventional values written down once.
- **Depends on:** DEC-0012 (the desk and HYP-0020), DEC-0014 (incubation).

## Why

HYP-0020 aims for +1.0% against a round-trip cost of about 0.9% (0.40% taker fee each way plus slippage). A
win nets about +0.1% and a loss about −1.4%, so it needs to win about 93% of the time. It is kept as the
registered baseline and left exactly as it is.

A long-only spot strategy can only clear those costs if its typical move is several times larger than they
are. The three strategies below therefore work on 4-hour bars and size their stop and target from recent
range (ATR), so a target is normally 5% to 10% away.

## Decision

1. **Family C goes from 4 to 7 trials** (`config/trial_registry.yaml`, `families.C`): HYP-0021, HYP-0022 and
   HYP-0023. The Deflated Sharpe Ratio in C1 uses 7.

2. **Common rules** for all three (the numbers live in `config/crypto.yaml`, `sleeves`):
   - Long only, Kraken spot, closed 4-hour bars on UTC boundaries. Indicators use closed bars only.
   - **Pairs:** BTC/USD, ETH/USD, SOL/USD, XRP/USD, ADA/USD, DOGE/USD, LINK/USD, AVAX/USD. A pair is traded
     on a bar only if that bar traded and the quote passes the desk's existing quality checks.
   - **Entry fill:** at the ask on the first cycle after the signal bar closes, plus 5 bps slippage, 0.40%
     taker fee. The same cost model as HYP-0020.
   - **Stop:** entry price minus 2 × ATR(14) of the signal bar. A signal whose stop would be closer than
     1.0% or further than 12% from the entry is skipped.
   - **Stops and targets** are resolved on 1-minute bars; if one bar touches both, the stop is taken. A stop
     that is gapped through fills at the bar's open. A target fills only when traded through.
   - **Exits decided on a closed 4-hour bar** (trailing stop, trend exit, time stop) are sold at the bid on
     the next cycle, less slippage.
   - One position per pair per sleeve. After an exit, the same pair needs a new signal bar.

3. **The strategies.**

   | Hypothesis | Sleeve | Entry, on the signal bar | Exit |
   |---|---|---|---|
   | HYP-0021 | TREND | EMA-20 above EMA-50, close above EMA-20, close above the highest high of the previous 20 bars | Stop as above, raised to (highest high since entry − 2 × ATR at entry) whenever that is higher; out on a close below EMA-20; time stop 180 bars (30 days) |
   | HYP-0022 | BREAK | Close above the highest high of the previous 30 bars, and volume above the mean volume of the previous 20 bars | Stop as above; target entry + 4 × ATR; time stop 60 bars (10 days) |
   | HYP-0023 | DIP | Last closed daily bar above its 50-day simple average; RSI-14 below 35 on at least one of the previous 6 bars; close above EMA-8 with the previous close at or below it | Stop as above; target entry + 3 × ATR; time stop 42 bars (7 days) |

4. **Limits** (`config/risk.yaml`, key `CT`), per sleeve, on a US$10,000 paper book each:
   - 1% of the book's equity risked per trade; quantity is risk divided by stop distance.
   - No position above 30% of equity, so a tight stop is capped by size and risks less than 1%.
   - Open exposure at most 100% of equity; at most 3 open positions.
   - At most 4 entries and 20 orders per UTC day.
   - Entries latched off for the sleeve after a realised loss of 3% of equity in a UTC day, until the owner
     resets it.
   - HYP-0020's limits (key `C`) are unchanged.

5. **Gate C1 for each strategy**, in `scripts/crypto_backtest.py`, run once per frozen config:
   - **History:** about two years of hourly bars per pair from a second public exchange, aggregated to
     4-hour and daily bars on the same UTC boundaries. Kraken's public endpoint serves only its last 720
     bars (120 days of 4-hour bars). Over those 120 days the two sources are compared and the difference is
     reported with the result.
   - **Same code as the desk:** the same strategy functions, exit rules and cost model.
   - **Falsified if** any of these holds: CI95 lower bound of mean R at or below 0 after fees at 1.5×
     slippage; deflated Sharpe at or below 0.95 at 7 trials; random-entry control p at or above 0.05; profit
     factor below 1.2; fewer than 30 trades.
   - Trades per month is reported first, before any return figure.

6. **Gate C0** (data quality) remains as DEC-0012 defines it, on 15-minute bars, for HYP-0020's three pairs.
   For the five added pairs the same two measures are recorded and published; they are information, and each
   bar is still checked before any entry.

7. **Paper trading** of these three starts as incubation under DEC-0014.

## Not decided here

- Any change to a number above after a result has been seen. Each is a new trial in family C.
- Short selling, leverage, or any pair outside the eight named.
- Any live order.
