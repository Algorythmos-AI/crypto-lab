# Crypto desk

The crypto desk (ADR 0005, DEC-0012) runs on the trading host next to the stocks desk. It reads Kraken's public
market data, books simulated fills on its own paper book, and publishes its own snapshot. It holds no venue
credentials and cannot place a real order.

## What runs

| Unit | When (UTC) | What |
|---|---|---|
| `wt-crypto.timer` | every 15 minutes, 10 s after the bar closes | one bar cycle: data, quality, exits, entries, evidence |
| `wt-dashboard-crypto.timer` | every 15 minutes, 1 min after the bar closes | publish the desk's snapshot |
| `wt-backup.timer` | every day 19:30 New York | verifies and anchors the crypto journal with the other ledgers |

State lives in `var/crypto/`: `book.json` (the book and the cycle's bookkeeping), `crypto_journal.jsonl`
(hash-chained decisions), `observations/` and `bars/`.

## First install (owner)

1. healthchecks.io: create the check `wt-crypto` (see [dead-man switches](dead-man-switches.md)).
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
| Turn all learning off or on (the model's filter and the challengers; DEC-0016, 6) | `make crypto-learning-off` / `make crypto-learning-on` (arrives with the learning job) |
| Clear the daily-loss latch | `make reset-crypto-latch` |
| Stop the desk entirely | owner: `sudo systemctl disable --now wt-crypto.timer wt-dashboard-crypto.timer` |

`unkill` and `reset-latch` refuse while a cycle is running and write a `control` line to the journal. Stopping
the timers makes the off-host watchdog page once ("Crypto desk: ... stopped"), which is correct.

## Pages

| Page | Meaning | First step |
|---|---|---|
| Crypto desk: dashboard late / stopped | no snapshot for 35 / 90 minutes | `make status`; `journalctl -u wt-dashboard-crypto -n 50` |
| `wt-crypto` (healthchecks) | no bar cycle finished for 45 minutes | `journalctl -u wt-crypto -n 50`; is the host up? |
| Crypto: no market data | three cycles in a row read nothing from Kraken | check status.kraken.com; nothing to do while it is down. An open position is not being watched: note the day as an incident |
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
