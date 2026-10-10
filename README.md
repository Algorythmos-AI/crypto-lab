# Crypto Lab

> Crypto Lab: crypto paper-trading desk on public market data and its own simulator, with a learning loop (Python)

A crypto desk that trades simulated money around the clock. It reads Kraken's public market data, books
simulated fills on its own paper books, records every decision in a hash-chained journal, and publishes a
snapshot of itself. Beside the frozen baseline strategy it runs a tournament of registered strategies, draws and
tests challengers by itself, follows every signal to its outcome and retrains a model every week under rules
written down in advance.

|                 |                                                                                                              |
| --------------- | ------------------------------------------------------------------------------------------------------------ |
| **Status**      | Extracted from `trading-lab-research`, not yet cut over: the desk still runs from that repository · paper only · no live trading |
| **Owner**       | [@Algorythmos-AI/maintainers](https://github.com/orgs/Algorythmos-AI/teams/maintainers)                      |
| **Runs at**     | Nothing runs from this repository yet. Target: the shared Linux host, `/home/wt/crypto-lab`, systemd units `cryptolab-*` |
| **Dashboard**   | The status site lives in another repository and shows this desk from the snapshot it receives ([contract](contracts/README.md)) |
| **Run locally** | `make bootstrap && make ci` (the ML suite: `make bootstrap-ml && make test-ml`)                              |
| **Context**     | [Where the desk stands](research/CRYPTO_SUMMARY.md) · [Research registry](research/README.md) · [What differs from the origin](research/decisions/CRY-DEC-0001-standalone-repository.md) · [ADRs](docs/adr/) · [Runbooks](docs/runbooks/) · [Cutover](docs/runbooks/cutover-from-monorepo.md) · [Security](SECURITY.md) · [Licence](LICENSE) |

---

> **Paper trading only. Not financial advice.** The desk has no venue credentials, no private endpoint and no
> order route: "paper" here means the lab's own simulator (`wt.crypto.book`) on live public quotes. Nothing here
> claims the strategies make money. The first registered results say they do not, and the records say so.

## Is everything OK?

1. **The dashboard's Crypto pages.** The banner is green, amber or red, with the reasons.
2. **Your phone.** ntfy alerts arrive only when something changes: a job failed or refused to run, the venue
   went quiet, a loss limit latched, the evidence chain broke.
3. **In a terminal on the host:** `make -C /home/wt/crypto-lab status`. It shows whether the desk has state, the
   kill switch, each job's last run, firing alerts and preflight.

## How it runs

```
systemd timers on the host (/home/wt/crypto-lab at a deployed main commit; runtime state in var/)
  cryptolab-crypto.timer              every 15 minutes, 10 s after the bar closes   wt.crypto.cycle
  cryptolab-dashboard-crypto.timer    every 15 minutes, 1 min after the bar closes  wt.crypto.snapshot
  cryptolab-crypto-challengers.timer  every day 03:30 New York                      wt.crypto.challengers
  cryptolab-crypto-learn.timer        every day 04:30 New York                      wt.crypto.learn
  cryptolab-backup.timer              every day 05:20 UTC                           wt.ops.backup nightly
        │
        └── each job runs through wt.ops.jobs: wait out a deploy, take the job's lock, refuse without the
            desk's state, preflight, run under a deadline, heartbeat, healthchecks ping, alert on a change
```

- **No silent fresh desk.** A job never makes a desk by running. Without the desk's state it refuses and says
  so; `python -m wt.ops.init --new-desk` is the only way a new book is made
  ([CRY-DEC-0001](research/decisions/CRY-DEC-0001-standalone-repository.md)).
- **The kill switch** is the file `var/crypto/KILL`. While it exists the desk opens nothing and keeps managing
  exits. `make kill REASON="..."`, `make unkill`.
- **Deploys** go through one gate (`make deploy`, on the host `cryptolab-deploy deploy`): a commit on `main`
  whose `test` and `ml` checks passed, the snapshot contract unchanged or acknowledged, the full suite in a
  throwaway worktree, then fast-forward, smoke test, and an automatic rollback if that fails.

## Layout

```
src/wt/crypto/   the desk: data, quality, strategy, book, risk, cycle, sleeves, challengers, outcomes,
                 promotion, learn, harvest, monitor, snapshot, control
src/wt/ml/       the learning side: dataset, models, train, validate, score (runs in .venv-ml only)
src/wt/core/     config, desk record, hash-chained ledger, clock
src/wt/ops/      the host layer: jobs, schedule, units, deploy, contract, preflight, init, backup, alerts,
                 heartbeat, healthchecks, publish machinery
config/          crypto.yaml (frozen per gate), risk.yaml (keys C, CT, CD, CH), trial_registry.yaml (family C)
contracts/       the snapshot's JSON Schema and a fixture, with their hashes: what the dashboard vendors
research/        hypotheses/ experiments/ decisions/ lessons_learned/ (CRY- prefix for new records)
deploy/          bin/ (cryptolab-* helpers) and systemd/ (cryptolab-* static units)
docs/            adr/ and runbooks/
tests/           unit/ (pytest, the `test` check) and ml/ (unittest in .venv-ml, the `ml` check)
tools/           parity.sh (the proof against the origin) and from-monorepo/ (the extraction, repeatable)
```

The Python package is still called `wt`, as in the repository it came from, so that the desk's code, state files
and frozen configuration are byte-identical across the move. A rename is a later change.

## Where this came from

The desk was built inside `Algorythmos-AI/trading-lab-research` and extracted with the history of its own files.
`tools/from-monorepo/` holds the extraction as a script, so it can be run again at a later commit:

| | |
|---|---|
| `paths.txt` | the allowlist of paths that move whole, with history |
| `extract.sh <clone> <commit>` | filters a scratch copy with the allowlist, lays the overlay on top, applies the patches |
| `overlay/` | everything that is not a whole-file move: trimmed copies of shared modules, new files, governance |
| `patches/` | the few deliberate changes to files that move whole |
| `drift.py <clone> <commit>` | what changed upstream since the copies were made, and what a re-run must redo by hand |
| `tools/parity.sh <clone>` | proves the desk here behaves as it does there |

To build it again at a later commit, from a clean checkout of this repository. The result is a new directory with
its own history; nothing here and nothing in the clone is overwritten:

```
git -C <clone> fetch origin
.venv/bin/python tools/from-monorepo/drift.py <clone> <commit>           # ends "no drift", or lists what to redo by hand first
.venv/bin/python tools/from-monorepo/drift.py <clone> <commit> --write   # the pins and the mechanical copies move to <commit>
tools/from-monorepo/extract.sh <clone> <commit> --out <new directory>
cd <new directory> && make bootstrap && make ci && make bootstrap-ml && make test-ml && tools/parity.sh <clone>
```

Research records that moved keep their ids (`HYP-0020` to `HYP-0024`, `DEC-0012` to `DEC-0027` where they are
the desk's, `EXP-0016` to `EXP-0022`, `LL-0003` to `LL-0006`). New records here start `CRY-`.
