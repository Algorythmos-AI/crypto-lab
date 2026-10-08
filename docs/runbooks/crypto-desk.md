# Crypto desk

The crypto desk (ADR 0005, DEC-0012) runs on the trading host next to the stocks desk. It reads Kraken's public
market data, books simulated fills on its own paper book, and publishes its own snapshot. It holds no venue
credentials and cannot place a real order.

## What runs

| Unit | When (UTC) | What |
|---|---|---|
| `wt-crypto.timer` | every 15 minutes, 10 s after the bar closes | one bar cycle: data, quality, exits, entries, evidence |
| `wt-dashboard-crypto.timer` | every 15 minutes, 1 min after the bar closes | publish the desk's snapshot |
| `wt-crypto-challengers.timer` | every day 03:30 New York | the challengers: retire, draw at most two a week, register, backtest, admit |
| `wt-crypto-learn.timer` | every day 04:30 New York | signal outcomes, the model's promotion tests, and once a week the retraining |
| `wt-backup.timer` | every day 19:30 New York | verifies and anchors the crypto journal with the other ledgers |

State lives in `var/crypto/`: `book.json` (the book and the cycle's bookkeeping), `crypto_journal.jsonl`
(hash-chained decisions), `observations/` and `bars/`.

## First install (owner)

1. healthchecks.io: create the checks `wt-crypto`, `wt-crypto-challengers` and `wt-crypto-learn` (see
   [dead-man switches](dead-man-switches.md)).
2. After the deploy that carries the jobs: `sudo wt-install-units` (gate open).
3. Nothing else. The first cycle creates `var/crypto/` **with the kill switch on**: the desk records bars and
   publishes, and opens nothing.

Check: `systemctl list-timers 'wt-*crypto*'`, then `make status` after 20 minutes (both crypto jobs `ok`, cadence
lines `[ok]`), then the Crypto tab of the dashboard.

## Controls

| Action | Command (on the host, as `wt`) |
|---|---|
| Stop new entries | `make kill DESK=crypto REASON="..."` |
| Allow entries (the owner's decision; before gate C1 the trades are incubation, DEC-0014) | `make unkill DESK=crypto` |
| Turn all learning off or on (the model's filter and the challengers; DEC-0016, 6) | `make crypto-learning-off` / `make crypto-learning-on` |
| See the challengers: who was drawn, the backtest verdicts, who is live | `make crypto-challengers` |
| Clear the daily-loss latch | `make reset-crypto-latch` |
| Stop the desk entirely | owner: `sudo systemctl disable --now wt-crypto.timer wt-dashboard-crypto.timer` |

`unkill`, `reset-latch` and the learning switch refuse while a cycle is running and write a `control` line to the
journal. With learning off, no challenger is drawn, backtested or admitted and live challengers open nothing;
their open positions are still managed to their exits, and the baseline and the three registered sleeves trade on. Stopping
the timers makes the off-host watchdog page once ("Crypto desk: ... stopped"), which is correct.

## Challengers

`wt-crypto-challengers` runs once a day at 03:30 New York time (DEC-0016, 5). Most runs only check the live
challengers against the retirement rules and take seconds. The first run of an ISO week draws at most two new
ones, writes their exact rules to the journal, and only then backtests each on the registered sleeves' own
two-year span (about 200 runs, several minutes). One that passes gate C1 trades its own US$10,000 paper book
from the next bar cycle and appears on the tournament board; one that fails never trades. The first run fetches
the hourly history from the second public exchange into `data/crypto/history` and reuses it after that.

A run that fails is retried by the next day's: the journal is the record, and a challenger registered but not
yet judged is judged first. The timer is new with this job, so it needs one `sudo wt-install-units`.

A challenger that passes gate C1 is then run once on the two years before that history (DEC-0021) and is admitted
only if it closed at least 15 trades there and made money after costs. One that does not is recorded as failed
with `not_confirmed_on_earlier_history`. If the earlier history cannot be fetched it stays registered, one notice
is sent, and the next day's run judges it.

## Limits across the desk

`config/risk.yaml`, key `CD` (DEC-0019), applies to the registered sleeves and the live challengers together; the
baseline is outside it. A sleeve's signal is refused with `desk_coin` when another book holds the coin, and with
`desk_risk` when open risk at the stops plus the new trade's would pass the cap. Both only refuse entries, and a
refused signal is still recorded and followed. Changing either number needs a decision record.

## The model

`wt-crypto-learn` runs once a day at 04:30 New York time (DEC-0016, 2 to 4):

1. **Outcomes.** Every recorded signal is followed to its result with its sleeve's own exits and costs, bought or
   not, and written to the journal as an `outcome` row. Hourly history comes from the second public exchange.
2. **The tests.** After every 60 finished signals the model in force is looked at: promoted (it then skips or
   halves weak signals of the three registered sleeves), held, or demoted. A drift in its inputs suspends it.
   Every change is a `model` row in the journal and one notice.
3. **Retraining, once a week.** `.venv-ml/bin/python -m wt.ml.train --register` as its own process with a
   75-minute limit. If no model beats taking every signal, none is in force and nothing is scored.

State is in `var/crypto/models/`: `current.json` (the model in force), `promotion.json` (shadow, acting, demoted
or suspended, and the checkpoints used), `last_train.json` (the last comparison). A model problem never stops a
trade: no answer in 20 seconds, an error, or a model older than 14 days means the signal is traded as its rule
says. `make crypto-learning-off` takes the model out at once.

## Pages

| Page | Meaning | First step |
|---|---|---|
| Crypto desk: dashboard late / stopped | no snapshot for 35 / 90 minutes | `make status`; `journalctl -u wt-dashboard-crypto -n 50` |
| `wt-crypto` (healthchecks) | no bar cycle finished for 45 minutes | `journalctl -u wt-crypto -n 50`; is the host up? |
| Crypto: no market data | three cycles in a row read nothing from Kraken | check status.kraken.com; nothing to do while it is down. An open position is not being watched: note the day as an incident |
| Crypto: the challengers did not run | the bar cycle could not read the challengers' record | the registered sleeves ran; `make crypto-challengers`, and `journalctl -u wt-crypto -n 50` for the fault. A challenger's open position is not watched meanwhile |
| Crypto: challenger ... joins the tournament / retired | information: the daily run admitted or retired one | nothing to do; `make crypto-learning-off` stops all of it |
| Crypto: the model did not score this cycle's signals | the scorer timed out, failed or its model is stale | nothing was skipped: every signal traded as its rule says. `journalctl -u wt-crypto -n 50`; a stale model means the weekly training has not registered one for 14 days |
| Crypto: the weekly model training failed | the trainer exited, timed out or wrote no summary | the model in force is unchanged; `journalctl -u wt-crypto-learn -n 80`. It is tried again the next day |
| Crypto: the model promoted / demoted / suspended | information: the daily tests changed what the model may do | nothing to do; the Learning panel shows the test |
| Crypto: daily loss limit reached | realised loss hit the latch | review the day's exits on the Strategy page; `make reset-crypto-latch` when satisfied |
| Crypto: an open position has an unobserved gap | more than 12 hours without 1-minute data while a position was open | the day is an incident for gate C2; the position is still managed |
| Evidence chain broken: crypto entries off | the nightly check found a break in `crypto_journal.jsonl` | do not edit the file; compare with the last anchor and the restic snapshot ([backups](backups-and-lease.md)) |

## Deploys

The crypto jobs are not trading jobs for the equity deploy gate. A deploy waits up to two minutes for a cycle in
flight and holds the jobs' locks while it changes the checkout; a cycle that fires meanwhile waits for the deploy.
A cycle missed during a deploy is made up by the next one: exits are resolved from 1-minute bars since the last
check.

## Changing the rules

Anything under `strategy`, `costs`, `quality` or `pairs` in `config/crypto.yaml`, and any limit under `C` in
`config/risk.yaml`, is frozen per gate. A change is a new trial in family C and needs a decision record first.
