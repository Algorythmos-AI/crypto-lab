# ADR 0005: Desks

- **Status:** accepted, 2026-10-04
- **Context:** the owner's decision of 2026-10-04 to bring the Kraken crypto bot into the lab as a second,
  separately viewable desk

## Context

Until now the lab ran one thing: strategy B on one Alpaca paper account, in US equity hours, published as one
snapshot. That assumption is written into about 25 places: one `KILL` file, one journal, one lease key, one
runner lock, one snapshot slot, a watchdog that sleeps outside New York session windows, a backup that runs on
weekdays, and a deploy gate built on the equity calendar.

A crypto desk differs on every one of those points. It trades around the clock, it sizes in fractions, its
venue has no paper endpoint, and an outage at 03:00 on a Saturday matters.

The bot being migrated (`kraken-bot`, TypeScript) is a prototype: no exits, a daily-loss limit that could not
trigger, no tests, state lost on redeploy. It is rebuilt here, not copied.

## Decision

1. **A desk is a named record** (`wt.core.desk.Desk`): its state directory, kill file, journal, lease key, lock
   names, session model (`us_equity` or `always_open`), alert-key prefix and snapshot schema id.
   - `stocks` points at today's paths exactly. Nothing moves and no unit text changes.
   - `crypto` lives under `var/crypto/`.

2. **A desk owns** its state, kill switch, limits (`config/risk.yaml`, one top-level key per desk strategy),
   journal, snapshot and watchdog state. **The lab shares** the host, the job runner, deploys, alert delivery,
   backups, CI and the dashboard site.

3. **Nothing crosses desks.**
   - Alert keys carry the desk's prefix; each snapshot and each health banner shows only its own.
   - A broken evidence chain switches off entries on its own desk only.
   - Each desk has its own snapshot slot. Ingest picks the slot from the signed body's `schema`, never from a
     header.

4. **The crypto desk is paper by construction.**
   - Kraken has no spot sandbox, so paper means the lab's own simulator (`wt.crypto.book`) on live public
     quotes.
   - `wt.crypto` uses public market-data endpoints only. The host holds no Kraken credentials, and
     `tests/unit/test_broker_isolation.py` fails on any private endpoint, auth header or key variable.
   - `AGENTS.md` rule 1 names `wt.crypto` as the only other place that may book a (simulated) order.

5. **A round-the-clock desk runs as an interval job, not a daemon.** One run per 15-minute bar through
   `wt.ops.jobs.run_job`, so it takes the job lock, waits out a deploy, runs preflight and writes a heartbeat
   like every other job. A deploy takes the interval jobs' locks before it changes the checkout. The equity
   deploy gate is unchanged.

   A desk may run several **sleeves**: one registered strategy and one paper book each, with its own limits
   and loss latch (DEC-0014, DEC-0015). Sleeves share the desk's kill switch, its job and its one journal,
   where each row names its sleeve. Their results are reported per sleeve and never pooled.

6. **Off-host monitoring follows the desk's session model.** The crypto watchdog window is always open; a desk
   that has never published is "new" and does not page.

7. **A desk can leave, and then runs nothing here** (added 2026-10-11, for the crypto desk's move to its own
   repository). While the file `<state directory>.MOVED` exists (`var/crypto.MOVED`), `wt.ops.jobs.run_job`
   returns before it takes a lock or starts the job: no child process, no heartbeat, no check-in ping, no
   state. The desk also stops counting as installed, so the backup and the cadence check leave it alone.
   - Why it is needed with the timers disabled: installing the units enables every timer this code still
     renders, and a run without the desk's state would create an empty desk and publish it as healthy.
   - The marker is beside the state directory, not inside it, because the directory is moved away.
   - It covers jobs started by the scheduler. A module run by hand (`python -m wt.crypto.cycle`) does not
     pass through `run_job`.
   - Removing the marker brings the desk back: that is the rollback.

## Consequences

- A third desk (FX) is a new `Desk` record, a venue package and a snapshot schema; the shared layers do not
  change again.
- The stock order types stay integer-share; the crypto book has its own `Decimal` types. Unifying them is a
  separate decision.
- The nightly backup becomes daily, because evidence is now written on weekends.
- Two snapshots mean two publishers, two freshness pills and two watchdog states to keep green.
