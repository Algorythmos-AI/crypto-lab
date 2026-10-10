# Security

## Reporting

Report a suspected secret leak or vulnerability privately to info@algorythmos.com.au. Never open a public issue.

## The repository is public for now

| | |
|---|---|
| Decided | By the owner (org decision D-026, 2026-10-10): public for now, as this code already was inside the repository it came from; private again per the org's go-private plan |
| Why | GitHub Actions for private repositories is blocked by an org billing issue; public repositories run CI free |
| What is here | The desk's code, tests on recorded public market data and synthetic bars, its configuration, and research records written in our own words |
| Never committed | Runtime state (`var/`), market-data caches (`data/`), logs, model files, credentials, tokens, account or host identifiers, and anything derived from third-party trading courses or books |
| How that is enforced | The history was built from an allowlist of paths (`tools/from-monorepo/paths.txt`); the pre-commit hook blocks runtime paths, data and model file types and `.env`; gitleaks runs in pre-commit and in CI (`security.yml`) |
| What CI holds | No secrets and no artifacts. The workflows have `contents: read` and nothing else |
| If something leaks | Treat it as published. Rotate the credential first, then tell the owner; removing it from history comes after and is the owner's call |

## Secrets

- **Where they live.** Nowhere in this repository and nowhere in CI. On the host they are in the shared secrets
  file (`/run/wt-secrets/env`, tmpfs, written at boot by the host's own secrets unit), which this repository's
  units read and never write. A development machine needs none: see `.env.example`.
- **What the desk reads.** The dashboard's ingest URL and signing secret, the deployment-protection bypass
  secret, the ntfy topic, the healthchecks ping key, and the backup's restic and R2 credentials.
- **What must not exist.** A venue credential. The desk reads public endpoints only; a test fails on any private
  endpoint, signing header or key variable (`tests/unit/test_crypto.py`).
- **Scrubbing them.** The snapshot publisher redacts secret values and token patterns from every string, and
  drops every field its allowlist does not name. The snapshot has no free-text field at all.
- **A separate secrets file** for this repository is a recorded follow-up; until then the host's file also holds
  another platform's keys, which this code never reads.

## Trading safety

- **Paper only, by construction.** Kraken has no spot sandbox, so paper means the lab's own simulator
  (`wt.crypto.book`) on live public quotes. There is no order route to remove: `wt.crypto` uses public
  market-data endpoints only, and the tests fail on a private endpoint, an auth header, a key variable or a
  broker library anywhere under `src/` and `scripts/`.
- **Entries only ever get blocked, exits never.** The kill switch, the daily-loss latches, the desk-wide limits
  and a broken evidence chain stop new entries; an open position is always managed to its exit.
- **No silent fresh desk.** A job refuses to run when the desk's state is missing or incomplete; only
  `python -m wt.ops.init --new-desk` makes a new book, with its kill switch on.
- **Evidence.** Every decision is a line in a hash-chained journal. A daily job verifies the chain, backs the
  state up off the host and writes the day's head to a write-once bucket; a broken chain turns entries off.
- **Frozen configuration.** `config/crypto.yaml` and the limits in `config/risk.yaml` are frozen per gate. A test
  pins their hashes; a change is a new trial and needs a decision record first.
