# CRY-DEC-0001: The crypto desk in its own repository, and every way it differs from the desk it was

- **Status: PROPOSED on 2026-10-11.** The owner accepts it by cutting the desk over to this repository
  (`docs/runbooks/cutover-from-monorepo.md`). Until then the desk runs from `trading-lab-research` and nothing
  here runs anywhere.
- **Depends on:** org decision D-026 (three trading platforms, each in its own repository) and the separation
  contract in `Algorythmos-AI/.github-private` `docs/trading-platforms.md`.
- **Does not amend** any strategy, cost, limit, gate or learning rule. DEC-0012 to DEC-0027 stand as written.
  No trial is added to family C: the frozen configuration is byte-identical and pinned
  (`tests/unit/test_config_pins.py`).
- **Extracted from:** `trading-lab-research` at the commit recorded in `tools/from-monorepo/SOURCE.json`.

## Why

Stocks, options and the crypto desk shared one repository, one job runner and one deploy gate. The owner
decided (D-026) that each platform lives in its own repository so that nobody, and no code, confuses one with
another. The crypto desk is live, so the move has one rule above all others: **the desk must behave exactly
as it did**, and where it does not, the difference is deliberate, tested and written down here.

## What did not change

The desk's own code (`src/wt/crypto/`, `src/wt/ml/`), its configuration, its research records, its tests and
its recorded fixtures moved whole, with their history (`tools/from-monorepo/paths.txt`). The package is still
called `wt`. Replayed on the same market data from the same starting state, the bar cycle, the sleeves, the
harvest, the challengers and the snapshot builder write the same journal rows, books and snapshot bodies as
they do in `trading-lab-research`, byte for byte (`tools/parity.sh`). The snapshot's schema id is still
`trading-lab/crypto-snapshot` and its schema is byte-identical. The four jobs keep their names, their
schedules, their memory limits and their healthchecks slugs.

## The differences

Each difference below is intended. "Patch" names the file under `tools/from-monorepo/patches/` when the
difference is a change to a file that moved whole; everything else is in the copied host layer
(`tools/from-monorepo/copies.json` says what was removed from each copy).

### 1. No silent fresh desk

*Before:* a missing `var/crypto` made the bar cycle create a new book (with its kill switch on) and the
publisher send a healthy-looking snapshot of it. The challengers job and the owner's controls could create
the directory too. After a move of the state, an old or a new timer firing against an empty checkout would
have started a second desk without a word.

*Now:* nothing creates a desk by running.

- A desk may run when it has its book and its journal (`book.json` and `crypto_journal.jsonl`), or when it
  carries the mark `python -m wt.ops.init --new-desk` leaves. A directory alone, or half a desk, is refused.
- `wt.ops.jobs` refuses every job on a desk that may not run: exit 0 (so systemd does not retry), heartbeat
  `refused`, one page per job per day, and the job's healthcheck fails.
- Each entry point (`python -m wt.crypto.cycle`, `.snapshot`, `.challengers`, `.learn`, `.control`) refuses the
  same way with exit 2 before it does anything, and `wt.crypto.cycle.run` no longer creates a missing state
  directory. Patch `0001-no-silent-fresh-desk.patch`.
- `python -m wt.ops.init --new-desk` makes a new desk on purpose: the directory, the kill switch with the same
  words the first cycle used to write, and the mark. It refuses when anything is already there. The job runner
  removes the mark after the first bar cycle that leaves a book and a journal.
- `sudo cryptolab-install-units` refuses to enable a timer while the desk has no state. `make kill` no longer
  creates the directory. The backup refuses, pages and fails its check when there is no desk to back up.
- One moved test changed with it (`test_the_desk_is_born_with_its_kill_switch_on`): same name, now asserting
  the refusal and then `init`.

Tests: `tests/unit/test_no_silent_fresh_desk.py`.

### 2. Host identity

The host is shared with `trading-lab-research`, whose installer removes or re-enables any unit in its own list.

| | Before | Now |
|---|---|---|
| Job units | `wt-crypto`, `wt-dashboard-crypto`, `wt-crypto-challengers`, `wt-crypto-learn` | `cryptolab-` + the same four job names |
| Static units and helpers | `wt-alert@`, `wt-backup`, `wt-restore-test`, `wt-install-units`, `wt-deploy`, `wt-alert` | `cryptolab-alert@`, `cryptolab-backup`, `cryptolab-restore-test`, `cryptolab-install-units`, `cryptolab-deploy`, `cryptolab-alert` |
| Checkout | `/home/wt/trading` | `/home/wt/crypto-lab` |
| Bookkeeping | `/etc/wt/installed-units.txt`, `/etc/wt/installed.sha256` | `/etc/cryptolab/installed-units.txt`, `/etc/cryptolab/installed.sha256` |
| Runtime state | `var/` of that checkout, shared with the stocks jobs (`alerts`, `heartbeats`, `locks`, `deploy`) | `var/` of this checkout, its own |
| Healthchecks slugs | `wt-crypto`, `wt-crypto-challengers`, `wt-crypto-learn` | the same |
| Secrets | `/run/wt-secrets/env` via `wt-secrets.service` | the same file and unit, read and never written (its own secrets file is a recorded follow-up) |
| Paging file of the alert helper | `/etc/wt/paging` | the same file, read only |

The installer gained `--no-enable` (install the files, start nothing), refuses any file that does not start
with `cryptolab-`, and never reads or writes `/etc/wt`. The unit text of the four jobs differs from what
`trading-lab-research` renders in exactly the prefix, the checkout path and the product name in
`Description=` (`tools/parity.sh` compares them). Tests: `tests/unit/test_host_identity.py`.

### 3. Deploy gate for an always-open desk

*Before:* one gate for both desks, built on the US equity calendar: closed from 07:00 New York to two hours
after the close, within an hour of a scheduled start, and when the calendar could not be read; a deploy also
checked that the live dashboard's build was not older than the contract code it carried.

*Now:* no calendar and no hour of the day. The gate is closed only while one of the two daily jobs is
running. A deploy waits up to two minutes for a 15-minute job in flight and holds every job's lock while it
changes the checkout, as before. The dashboard is not in this repository, so the build check is replaced by
the contract check: a target whose `contracts/crypto.schema.json` differs from the live checkout's is
refused unless the owner names the new schema's sha256 (`WT_DEPLOY_CONTRACT`). Required checks are `test`
and `ml`. The smoke step runs the tests marked `smoke` (`tests/unit/test_smoke.py`). The state migration,
the Mac's agents and the broker clean-up before a rollback are gone with the stocks desk.
Tests: `tests/unit/test_deploy_gate.py`, `tests/unit/test_contract.py`.

### 4. Backups

| | Before | Now |
|---|---|---|
| Unit | `wt-backup.timer`, every day 19:30 New York, with the stocks ledgers | `cryptolab-backup.timer`, every day 05:20 UTC, the desk's two journals only |
| restic host | the machine's name | the machine's name + `-crypto-lab`, and the tag `crypto-lab` |
| What is backed up | `var`, `data/live`, `logs` of the shared checkout (Fridays also `data`) | `var`, `logs` of this checkout (Fridays also `data`) |
| Anchor | `anchors/crypto/<New York day>.json` | `anchors/crypto-lab/<UTC day>.json`, same bucket, same write-once rule |
| Healthcheck | `wt-backup` | `wt-crypto-backup` (a new check the owner creates) |
| Restore test | `wt-restore-test.timer`, Saturdays | `cryptolab-restore-test.timer`, Sundays 06:20 UTC, this repository's own snapshots |

New: `python -m wt.ops.backup extends <key>` reads any anchor of the desk's ledgers and says whether the
journals on disk still extend it. It is how the cutover shows that the journal that arrived is the one the
last anchor of the old checkout described. Tests: `tests/unit/test_backup.py`.

### 5. What follows from 2, without being a choice

- **Alert, heartbeat and lock state start empty.** They live in `var/` of the checkout. An alert that was
  firing at the cutover fires once more; a once-a-day notice already sent that day can be sent again; the
  snapshot's job history (`jobs.last`, `jobs.runs`) starts empty and fills from the first run here.
- **Job-level alerts are on no dashboard page.** Alert keys without the desk's `crypto:` prefix (`job:crypto`,
  `refuse:crypto:<day>`, `deploy`, `backup`, `anchor`) were listed on the *stocks* snapshot, because its
  publisher took every key no other desk owned. They still page through ntfy; nothing publishes them now.
  The crypto snapshot never carried them and still does not, so its body is unchanged. `desk_of_alert`
  answers `host` for such a key where it used to answer `stocks`.
- **The stocks daily summary loses its "Crypto:" line.** It was added to the stocks forward job's end-of-day
  message by the shared job runner.
- **The job runner no longer asks for the US market calendar.** The shared runner fetched it at the start of
  every job, the crypto ones included, and logged a fallback line when it could not.
- **No host heartbeat from here.** The `wt-host-<id>` check is pinged by the stocks publisher, which stays.
- **The trading environment is smaller.** `requirements.lock.txt` holds the 21 packages the desk imports, each
  with the version and hashes it had; the 15 others (broker SDK, data frames, PDF and websocket libraries)
  are not installed. The ML lockfile moved whole.

### 6. Where the contract lives

`contracts/crypto.schema.json` and `contracts/crypto.v1.json` (they were generated into the dashboard's
tree), with `contracts/SHA256SUMS`. Patch `0002-contract-in-contracts.patch` changes the two paths and
nothing else; both files are byte-identical to the ones in `trading-lab-research`.

### 7. Two smaller patches

- `0003-host-packages.patch`: one moved test read the shared host's first-boot files to see that the ML
  environment's system library is installed; it now reads `deploy/host-packages.txt`.
- `0004-runbook-for-this-repository.patch`: `docs/runbooks/crypto-desk.md` names this repository's units,
  commands, install rule and deploy.

## What this record does not decide

- The cutover itself, which only the owner can do on the host.
- Removing the desk from `trading-lab-research`, and the guard that makes its four crypto jobs do nothing once
  the state has moved. Both are changes in that repository.
- A rename of the `wt` package and a secrets file of this repository's own: recorded follow-ups.

## How it is checked

`make ci` (the moved tests and the tests named above), `make test-ml`, `tools/parity.sh <monorepo clone>`, and
`tools/from-monorepo/drift.py <monorepo clone> <commit>` before any later extraction.
