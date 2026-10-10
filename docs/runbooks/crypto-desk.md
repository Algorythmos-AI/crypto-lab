# Crypto desk

The crypto desk (ADR 0005, DEC-0012) runs on the shared host from its own checkout, `/home/wt/crypto-lab`, under
its own systemd units (`cryptolab-*`). It reads Kraken's public market data, books simulated fills on its own
paper book, and publishes its own snapshot. It holds no venue credentials and cannot place a real order.

## What runs

| Unit | When (UTC) | What |
|---|---|---|
| `cryptolab-crypto.timer` | every 15 minutes, 10 s after the bar closes | one bar cycle: data, quality, exits, entries, evidence |
| `cryptolab-dashboard-crypto.timer` | every 15 minutes, 1 min after the bar closes | publish the desk's snapshot |
| `cryptolab-crypto-challengers.timer` | every day 03:30 New York | the challengers: retire, draw at most two a week, register, backtest, admit |
| `cryptolab-crypto-learn.timer` | every day 04:30 New York | signal outcomes, the model's promotion tests, and once a week the retraining |
| `cryptolab-backup.timer` | every day 05:20 | verifies the desk's two journals, backs the state up, anchors the day's heads (`anchors/crypto-lab/`) |
| `cryptolab-restore-test.timer` | Sundays 06:20 | restores the latest backup into a temp directory and verifies it |

State lives in `var/crypto/`: `book.json` (the book and the cycle's bookkeeping), `crypto_journal.jsonl`
(hash-chained decisions), `observations/` and `bars/`.

## Install (owner)

The desk came to this host inside `trading-lab-research`. Moving it here, with its state, is
[the cutover runbook](cutover-from-monorepo.md); nothing below replaces it.

**A job never makes a desk** (CRY-DEC-0001). Without `var/crypto/` and the book and journal in it, every job
refuses to run, says why, alerts once a day and fails its healthcheck. `make desk-check` says whether the desk
has state. `sudo cryptolab-install-units` refuses to enable a timer while it has none.

Only for a desk that has never existed anywhere (never during a move):

1. healthchecks.io: the checks `wt-crypto`, `wt-crypto-challengers` and `wt-crypto-learn` (the slugs the jobs
   have always pinged), and `wt-crypto-backup` for the backup.
2. `make init` as `wt`: makes `var/crypto/` **with the kill switch on**.
3. `sudo cryptolab-install-units` (gate open). The desk records bars and publishes, and opens nothing.

Check: `systemctl list-timers 'cryptolab-*'`, then `make status` after 20 minutes (both 15-minute jobs `ok`,
cadence lines `[ok]`), then the Crypto pages of the dashboard.

## Controls

| Action | Command (on the host, as `wt`) |
|---|---|
| Stop new entries | `make kill REASON="..."` |
| Allow entries (the owner's decision; before gate C1 the trades are incubation, DEC-0014) | `make unkill` |
| Turn all learning off or on (the model's filter and the challengers; DEC-0016, 6) | `make crypto-learning-off` / `make crypto-learning-on` |
| See the challengers: who was drawn, the backtest verdicts, who is live | `make crypto-challengers` |
| Stop or allow the data-harvest books' entries (DEC-0027; exits are still managed) | `make crypto-harvest-off` / `make crypto-harvest-on` |
| See the data-harvest books | `make crypto-harvest` |
| Clear the daily-loss latch | `make reset-crypto-latch` |
| Stop the desk entirely | owner: `sudo systemctl disable --now cryptolab-crypto.timer cryptolab-dashboard-crypto.timer cryptolab-crypto-challengers.timer cryptolab-crypto-learn.timer` |

`unkill`, `reset-latch` and the learning switch refuse while a cycle is running and write a `control` line to the
journal. With learning off, no challenger is drawn, backtested or admitted and live challengers open nothing;
their open positions are still managed to their exits, and the baseline and the three registered sleeves trade on. Stopping
the timers makes the off-host watchdog page once ("Crypto desk: ... stopped"), which is correct.

## Challengers

`cryptolab-crypto-challengers` runs once a day at 03:30 New York time (DEC-0016, 5). Most runs only check the live
challengers against the retirement rules and take seconds. The first run of an ISO week draws at most two new
ones, writes their exact rules to the journal, and only then backtests each on the registered sleeves' own
two-year span (about 200 runs, several minutes). One that passes gate C1 trades its own US$10,000 paper book
from the next bar cycle and appears on the tournament board; one that fails never trades. The first run fetches
the hourly history from the second public exchange into `data/crypto/history` and reuses it after that.

A run that fails is retried by the next day's: the journal is the record, and a challenger registered but not
yet judged is judged first.

A challenger that passes gate C1 is then run once on the two years before that history (DEC-0021) and is admitted
only if it closed at least 15 trades there and made money after costs. One that does not is recorded as failed
with `not_confirmed_on_earlier_history`. If the earlier history cannot be fetched it stays registered, one notice
is sent, and the next day's run judges it.

## Data harvest

DEC-0027. Paper books that trade often to collect learning data: the three registered rules, numbers
unchanged, on 4-hour and on 1-hour bars (`h-trend`, `h-trend-60m`, ...), and the exploration book
`h-explore`, which enters one coin drawn at random on every hourly bar with a 3-ATR stop and target and a
one-day time exit. All on the 30 training pairs, under limits `CH` in `config/risk.yaml` (small positions,
room for many). They run at the end of every bar cycle on their own budget of public calls, after the baseline and
the sleeves, and cannot change either. State and journal are in `var/crypto/harvest/`
(`harvest_journal.jsonl`, backed up and anchored with the desk's other ledger). **Not evidence**: no gate or
tournament result counts a harvest trade, and losing harvest trades are expected.

Once an hour the first bar cycle also labels every finished harvest signal (an `outcome` row in the harvest
journal, worked out as the tournament's are, from the desk's stored hourly bars) and writes one feature row
per coin per closed hour to `var/crypto/harvest/features/hourly-<day>.jsonl`, traded or not. Neither calls
the venue. The model in force does not read any of it (DEC-0027, 5). The Overview's Data harvest panel shows
the counts.

The desk's kill switch and chain flag stop harvest entries too. `make crypto-harvest-off` stops only
the harvest's. `make reset-crypto-latch` also clears a harvest book's latch.

## Limits across the desk

`config/risk.yaml`, key `CD` (DEC-0019), applies to the registered sleeves and the live challengers together; the
baseline is outside it. A sleeve's signal is refused with `desk_coin` when another book holds the coin, and with
`desk_risk` when open risk at the stops plus the new trade's would pass the cap. Both only refuse entries, and a
refused signal is still recorded and followed. Changing either number needs a decision record.

## The model

`cryptolab-crypto-learn` runs once a day at 04:30 New York time (DEC-0016, 2 to 4):

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
| Crypto desk: dashboard late / stopped | no snapshot for 35 / 90 minutes | `make status`; `journalctl -u cryptolab-dashboard-crypto -n 50` |
| `wt-crypto` (healthchecks) | no bar cycle finished for 45 minutes | `journalctl -u cryptolab-crypto -n 50`; is the host up? |
| `<job>` refused to run: the desk has no state / its state is incomplete | `var/crypto/` is missing, or its book or journal is | **Do not run `make init`.** Nothing was created and nothing was published. Find the state: was it moved away, was a move left half done? `make desk-check`, then [the cutover runbook](cutover-from-monorepo.md) |
| Crypto: no market data | three cycles in a row read nothing from Kraken | check status.kraken.com; nothing to do while it is down. An open position is not being watched: note the day as an incident |
| Crypto: the challengers did not run | the bar cycle could not read the challengers' record | the registered sleeves ran; `make crypto-challengers`, and `journalctl -u cryptolab-crypto -n 50` for the fault. A challenger's open position is not watched meanwhile |
| Crypto: challenger ... joins the tournament / retired | information: the daily run admitted or retired one | nothing to do; `make crypto-learning-off` stops all of it |
| Crypto: the model did not score this cycle's signals | the scorer timed out, failed or its model is stale | nothing was skipped: every signal traded as its rule says. `journalctl -u cryptolab-crypto -n 50`; a stale model means the weekly training has not registered one for 14 days |
| Crypto: the weekly model training failed | the trainer exited, timed out or wrote no summary | the model in force is unchanged; `journalctl -u cryptolab-crypto-learn -n 80`. It is tried again the next day |
| Crypto: the model promoted / demoted / suspended | information: the daily tests changed what the model may do | nothing to do; the Learning panel shows the test |
| Crypto: the data harvest did not run | the harvest's part of the bar cycle raised | the baseline and the sleeves ran. `journalctl -u cryptolab-crypto -n 50`; a harvest position is not watched meanwhile. `make crypto-harvest-off` stops its entries |
| Crypto: daily loss limit reached | realised loss hit the latch | review the day's exits on the Strategy page; `make reset-crypto-latch` when satisfied |
| Crypto: an open position has an unobserved gap | more than 12 hours without 1-minute data while a position was open | the day is an incident for gate C2; the position is still managed |
| Evidence chain broken: crypto entries off | the daily check found a break in `crypto_journal.jsonl` or `harvest_journal.jsonl` | do not edit the file; compare with the last anchor (`python -m wt.ops.backup extends anchors/crypto-lab/<day>.json`) and the restic snapshots of host `<hostname>-crypto-lab`. Once explained and repaired, the owner removes `var/crypto/chain-broken` |
| Crypto Lab: backup failed / daily evidence anchor failed | restic or the anchor write failed; "DIFFERENT heads" means history was rewritten after anchoring | `journalctl -u cryptolab-backup -n 80`; the next day's run retries |

## Deploys

```
sudo -u wt nohup /usr/local/bin/cryptolab-deploy deploy > /tmp/cryptolab-deploy.log 2>&1 &
tail -f /tmp/cryptolab-deploy.log
```

The desk is always open, so the gate knows no market hours: it is closed only while one of the two daily jobs is
running (`cryptolab-deploy gate` says). A deploy needs the commit's `test` and `ml` checks green, runs the full
suite in a throwaway worktree first, waits up to two minutes for a cycle in flight and holds the jobs' locks while
it changes the checkout; a cycle that fires meanwhile waits for the deploy. A cycle missed during a deploy is made
up by the next one: exits are resolved from 1-minute bars since the last check. A failed smoke test rolls the
checkout back by itself.

A commit that changes the snapshot contract (`contracts/crypto.schema.json`) is refused until the dashboard has
the new schema: see [the contract](../../contracts/README.md). If the deploy ends with "units changed", run
`sudo cryptolab-install-units`.

To go back: `sudo -u wt /usr/local/bin/cryptolab-deploy "rollback runtime-<tag>"` (the tag is printed by the
deploy that is being undone).

## Changing the rules

Anything under `strategy`, `costs`, `quality` or `pairs` in `config/crypto.yaml`, and any limit under `C` in
`config/risk.yaml`, is frozen per gate. A change is a new trial in family C and needs a decision record first.
