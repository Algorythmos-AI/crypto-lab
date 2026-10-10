# AGENTS.md: rules for automated contributors in crypto-lab

Read the org rules first (`Algorythmos-AI/.github-private` AGENTS.md and `catalog.yaml`). This file adds the rules
specific to this repository. Where the two conflict, the org file's "Never" sections win. The separation contract
between the trading platforms lives in that repository's `docs/trading-platforms.md`.

## Hard rules

1. **Paper only, by construction.** Real-money trading cannot be enabled from this repo, and nothing may be
   added that could.
   - The desk books simulated fills on its own simulator (`wt.crypto.book`) from public market data. It has no
     venue credentials, no private endpoint, no signing and no order route.
   - Never add a broker or exchange client library, a private endpoint, an auth header or a key variable.
     `tests/unit/test_crypto.py` fails on each.
   - Network libraries are imported under `src/wt/crypto/` by `data.py` only.
2. **One platform per pull request, and no imports from other platforms.**
   - This repository is one platform. It has its own config, kill switch, state directory, journal, alert-key
     prefix, snapshot schema id, unit prefix and runbooks, and it reads nobody else's state.
   - No import, submodule or copied state from another trading platform's repository. A generic piece that is
     needed here is copied in and trimmed, with its origin commit noted (`tools/from-monorepo/copies.json`).
   - The dashboard is not here. The desk publishes a snapshot under a versioned contract (`contracts/`); a change
     to the contract is deployed only after the dashboard has it (`wt.ops.contract`).
3. **Pre-registration.** Every strategy or evaluation change needs a hypothesis, an experiment and a decision
   record in `research/`, written before any affected P&L is viewed.
   - New records here are `CRY-HYP-NNNN`, `CRY-EXP-NNNN`, `CRY-DEC-NNNN` and `CRY-LL-NNNN`, sequential and never
     reused. Records that moved with the desk keep their old ids, and those numbers are never reused either.
   - Experiments are append-only: a re-run gets a new id. A hold-out is run once per frozen config.
   - A reference to another repository's record is written `repo:ID`.
   - `config/crypto.yaml` (`strategy`, `costs`, `quality`, `pairs`, `sleeves`, `learning`, `harvest`) and the
     limits in `config/risk.yaml` are frozen per gate. A change is a new trial in family C, counted in
     `config/trial_registry.yaml`, and `tests/unit/test_config_pins.py` fails until the pin is moved in the same
     pull request as the decision record.
4. **The live checkout is production.**
   - The jobs run on the host from `/home/wt/crypto-lab`. It changes only through the gated deploy
     (`cryptolab-deploy`), never by hand.
   - Never edit, switch branches in, or reinstall the venv of a checkout that runs jobs.
   - Never create, move, copy or delete a desk's state directory. Never run `python -m wt.ops.init --new-desk` on
     a host. Hand the owner the command.
5. **Units.** Never install, enable, disable, start or stop the `cryptolab-*` systemd units, and never touch
   another platform's units on the shared host. Hand the owner the command (`sudo cryptolab-install-units`).
6. **Commits and PRs.**
   - Branch, open a PR, and let the `test` and `ml` checks pass, then squash-merge.
   - No `Co-Authored-By` trailer, and no tool or assistant named as an author or helper anywhere: not in commit
     messages, files, docs or comments (org decision D-016).
   - Author: `skalaliya <skalaliya@gmail.com>`.

## Data classes

The repository is **public for now**. Assume every byte committed is read by strangers and indexed.

| Class | Examples | May be committed |
|---|---|---|
| NEVER | Runtime state (`var/`), market-data caches and history (`data/`), logs, model files and weights, credentials and tokens, host names and addresses, text derived from third-party trading courses, books or the private knowledge base | Never |
| PRIVATE | The paper books' balances and positions, log lines, owner notes | Never here; they stay on the host or behind the owner's login |
| SAFE | Source code, tests on recorded public market data and synthetic bars, configuration, research records written in our own words, counts, ids, statuses, dates and statistics | Yes |

Research records are written in our own words. Quote nothing from course material, books or the private
knowledge base.

## Layout

```
src/wt/        crypto (the desk) · ml (the learning side) · core · ops (the host layer) · backtest · research
scripts/       research drivers (crypto_backtest, crypto_ranked, ...) and the fixture generator
config/        crypto.yaml, risk.yaml, trial_registry.yaml
contracts/     the snapshot's schema and fixture, with their hashes
research/      hypotheses/ experiments/ decisions/ lessons_learned/
deploy/        bin/ and systemd/, every file named cryptolab-*
docs/          adr/ and runbooks/
tests/         unit/ (pytest; CI-safe: fixtures and tmp_path only, no network) and ml/ (the ML environment)
tools/         parity.sh and from-monorepo/ (the repeatable extraction)
```

Git-ignored: `data/`, `logs/`, `var/` (runtime state), `.venv/`, `.venv-ml/`, `.env`, `KILL`.

Until the cutover is done and the desk's code has left the repository it came from, the files listed in
`tools/from-monorepo/paths.txt` are changed there, not here: a change made here is lost at the next extraction.
Files under `tools/from-monorepo/overlay/` are the source of the same paths at the root; change both
(`make overlay-check` fails otherwise).

## Everyday commands

```
make bootstrap       # .venv from requirements.lock.txt (and the dev tools)
make bootstrap-ml    # .venv-ml from requirements-ml.lock.txt
make ci              # ruff + mypy (typed modules) + pytest; the same as the required `test` check
make test-ml         # the ML suite in .venv-ml; the same as the required `ml` check
make hooks           # pre-commit: gitleaks, runtime-path guard, ruff
```

The kill switch is the file `var/crypto/KILL` in the live checkout. While it exists the desk makes no new
entries; it keeps managing exits.
