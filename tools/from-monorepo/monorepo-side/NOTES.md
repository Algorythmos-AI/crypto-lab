# What `trading-lab-research` has to change, as seen from the extraction

Notes for whoever makes the monorepo's side of the move (plan steps C3 and C5). Nothing here was done: this
repository never writes to the monorepo. Every item was found by reading the monorepo at the commit in
`../SOURCE.json`; check each against the monorepo's state when the change is made.

## Before the cutover: the moved-marker guard (C3)

**Done upstream:** merged in `trading-lab-research` as `c5d9830`. The marker is `var/crypto.MOVED`
(`wt.core.desk.moved_marker`), `wt.ops.jobs.run_job` returns before the lock while it exists, and a moved desk
no longer counts as installed, so the backup and the cadence check leave it alone. It still has to be deployed
on the host before the cutover. What follows is the request as it was written, kept for the reasons.

The cutover runbook (`docs/runbooks/cutover-from-monorepo.md`, pre-check 1.6) refuses to start without it.

- **What it must do.** While a marker file exists, the four crypto jobs (`crypto`, `dashboard-crypto`,
  `crypto-challengers`, `crypto-learn`) exit without creating `var/crypto`, without appending to a journal and
  without publishing. Today a missing `var/crypto` makes `wt.crypto.cycle.run` create a new book with its kill
  switch on, `wt.crypto.snapshot` publish it, and `wt.crypto.challengers.run` write a journal.
- **Where.** Before the child process starts, in `wt.ops.jobs.run_job`, is the one place all four pass through
  when systemd starts them; `python -m wt.crypto.cycle` run by hand does not pass through it.
- **Why it is needed even with the four timers disabled.** `deploy/oci/bin/wt-install-units` enables every
  `wt-*.timer` the code renders. Until the desk's jobs leave `wt.ops.schedule`, any run of it after the cutover
  turns the four timers back on.
- **The marker's path** is the monorepo's choice. The runbook here assumes `var/crypto.MOVED` under the old
  checkout and says so; tell this repository the real path so the runbook can name it.
- **It should also** keep `wt.ops.backup` and the cadence check quiet about a desk that has moved (they already
  skip a desk whose state directory is missing: `wt.core.desk.installed`).

## After 24 clean hours: remove the desk (C5)

What still names the crypto desk in the monorepo, by file:

| File | What refers to the desk |
|---|---|
| `src/wt/crypto/`, `src/wt/ml/`, `scripts/crypto_*.py`, `scripts/gen_crypto_fixture.py`, `config/crypto.yaml`, `requirements-ml.*`, `docs/runbooks/crypto-desk.md`, `tests/fixtures/kraken/`, `tests/unit/test_crypto*.py`, `tests/unit/test_ml_*.py`, `tests/ml/` | moved whole (`../paths.txt`) |
| `src/wt/ops/schedule.py` | the four `Job` records |
| `src/wt/ops/units.py` | four rows of `MEMORY` |
| `src/wt/ops/jobs.py` | `crypto_summary` and its line in `daily_summary`; three slugs in `HC_JOBS` |
| `src/wt/ops/deploy.py` | `sync_ml` and its constants (`ML_USERS` names `crypto-learn`), the `sync-ml` command |
| `src/wt/ops/ci.py` | `DEFAULT_REQUIRED` includes `ml`: drop it in a change deployed **before** the one that removes the `ml` job, or no commit is ever green again |
| `src/wt/ops/preflight.py` | `requirements-ml.lock.txt` in `CODE_PATHS` |
| `src/wt/ops/backup.py` | anchors and restores every installed desk's ledgers; the docstring's crypto notes |
| `src/wt/ops/publish.py` | `extras` filters jobs and alerts by desk (`desk_of_alert`) |
| `src/wt/core/desk.py` | the `crypto` record; `desk_of_alert` |
| `src/wt/research/trials.py` | `family_trial_count` |
| `config/risk.yaml` | keys `C`, `CT`, `CD`, `CH` |
| `config/trial_registry.yaml` | `families.C` |
| `Makefile` | `bootstrap-ml`, `lock-ml`, `sync-ml`, the `kill`/`unkill` branches for `DESK=crypto`, the `crypto-*` targets, the crypto line of `schema`, the crypto and ML modules in `TYPED_MODULES` |
| `.github/workflows/ci.yml` | the `ml` job; `crypto.schema.json` in the contract step |
| `.github/workflows/security.yml` | the audit of `requirements-ml.lock.txt` |
| `deploy/contract-paths.txt` | `src/wt/crypto/snapshot.py` |
| `tests/unit/test_desks.py`, `test_host_linux.py`, `test_ops_runtime.py`, `test_trials.py`, `test_backup.py` | cases that build or expect the crypto desk, its jobs or its units |
| `AGENTS.md`, `docs/adr/0005-desks.md`, `README.md` | prose |
| `dashboard/src/lib/crypto.schema.json`, `dashboard/test/fixtures/crypto.v1.json` | generated here until now; from then vendored from this repository's `contracts/` at a tag (`contract-<first 12 hex digits of the schema's sha256>`), with the two hashes of `contracts/SHA256SUMS` checked |

Things that are easy to miss:

- **Stale alert keys.** `var/alerts/state.json` in the old checkout keeps every `crypto:*` key that was firing at
  the cutover, and nothing resolves them. They are hidden only while `desk_of_alert` still knows the `crypto:`
  prefix. Once the desk's record is removed they read as the stocks desk's and show as firing for good. Clear
  them in the same change, or keep the prefix's owner.
- **The research index.** `tests/unit/test_trials.py` expects `EXP-0016-crypto-c1` in the experiments folder and
  `scripts/research_index.py` lists it. Add `research/MOVED.md` (the proposed text is `MOVED.md` beside this
  file) so the moved ids do not look free.
- **The four old units.** After the removal is deployed, `sudo wt-install-units` removes them, because they are
  in `/etc/wt/installed-units.txt` and no longer rendered. Until then they sit disabled.
- **The dashboard keeps showing the desk** from the snapshot it receives; only where its schema and fixture come
  from changes.
- **The stocks daily summary** loses its "Crypto:" line at the cutover, not at the removal.
