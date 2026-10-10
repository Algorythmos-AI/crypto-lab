# Cutover: the live crypto desk moves from `trading-lab-research` to this repository

For the owner. Every command on the host is the owner's to run; nobody else can, and no automated contributor
may. Read it to the end once before starting. Nothing here changes a strategy, a limit or a model: the same
code reads the same state from a different checkout, started by differently named units.

**Written without access to the host.** Each step marked **[UNVERIFIED]** rests on something that could only be
read from the repositories, never checked on the server. Those steps start with a read-only check; if the check
does not show what the step expects, stop there. The whole list is in [What could not be verified](#what-could-not-be-verified).

| | |
|---|---|
| Downtime | none planned: the cutover fits between two bar cycles (about twelve minutes); a missed cycle is made up by the next |
| Reversible | yes, until the desk's code is removed from `trading-lab-research`: [Rollback](#5-rollback) |
| What moves | three directories, **moved, never copied**: `var/crypto`, `var/dashboard-crypto`, `data/crypto` |
| What changes on the host | four old timers disabled, eight new job units and five static units installed under `cryptolab-*`, one new checkout `/home/wt/crypto-lab` |
| What does not change | the secrets, the dashboard, the three healthchecks of the jobs, the stocks desk and everything named `wt-*` except those four timers |

## The risks this is built around

1. **A second desk.** If a job of either checkout runs against a missing state directory, the old code makes a
   new book and publishes it as if nothing happened; two publishers would then overwrite each other every 15
   minutes, and the dashboard accepts both (it answers 409 to the older one, which the publisher counts as
   stored). Three things prevent it, and the cutover needs all three:
   - this repository's jobs refuse to run without the desk's state (CRY-DEC-0001);
   - the old repository's four crypto jobs do nothing once the **moved marker** exists. That guard is in
     `trading-lab-research` from commit `c5d9830` and **must be deployed on the host first** (pre-check 1.6);
   - all **four** old timers are disabled, not two: `wt-crypto`, `wt-dashboard-crypto`,
     `wt-crypto-challengers`, `wt-crypto-learn`. The last two are daily and easy to forget; the challengers
     job writes to the journal and would create one.
2. **`sudo wt-install-units` undoes step 1.** The old installer enables every timer the old code renders,
   and until the desk's code is removed from `trading-lab-research` that includes the four crypto timers. After
   the cutover, a routine "units changed; the owner runs: sudo wt-install-units" after a stocks deploy turns
   them back on. With the marker guard deployed they then fire and do nothing; without it they start a second
   desk. Do not run `sudo wt-install-units` between the cutover and the removal unless pre-check 1.6 passed.
3. **A forked journal.** The journal is a hash chain and is anchored off the host every day. A copy that both
   checkouts could append to is two histories with one beginning. So the directories are renamed into place
   (`mv -T`, one filesystem), never copied, and the journal's length and last line are compared before and after.
4. **Evidence without a backup.** Once `var/crypto` leaves, the old backup stops covering the desk's journals
   without saying so (it anchors only desks that are installed). This repository's backup timer must be
   enabled the same day, and its first anchor checked the next morning.
5. **A missed signal.** A rule acts on a bar only within 20 minutes of its close. The 4-hour rules' bars close
   at 00:00, 04:00, 08:00, 12:00, 16:00 and 20:00 UTC. A cutover in the 20 minutes after one of those could
   turn a fresh 4-hour signal into a refused late one. An open position is not at risk at any hour: exits are
   resolved from 1-minute bars since the last check, which the venue keeps for 12 hours.

## When

Pick one quarter-hour that meets all of these, and do steps 3.1 to 3.6 inside it:

- **after a cycle and its publish have finished, before the next cycle starts:** minutes `:02` to `:14`, `:17` to
  `:29`, `:32` to `:44` or `:47` to `:59` (cycles start at `:00:10`, `:15:10`, `:30:10`, `:45:10` UTC and usually
  take seconds; the publish starts a minute later);
- **not in the 20 minutes after a 4-hour boundary:** not 00:00 to 00:20, 04:00 to 04:20, 08:00 to 08:20, 12:00 to
  12:20, 16:00 to 16:20, 20:00 to 20:20 UTC;
- **not while a daily job may be running:** not between 03:30 and 06:30 New York (the challengers start 03:30
  and may run 75 minutes; the learning run starts 04:30 and may run 105);
- **not at 05:20 UTC** (this repository's backup) and **not at 19:30 New York** (the old backup, which until
  the cutover is the one that anchors the desk);
- **not on the day of the first install of another platform on the host.**

If the slot is missed (step 3.2 shows a job still running at `:13`), wait for the next quarter. Nothing is lost
by waiting.

## 1. Read-only pre-checks (any time; nothing changes)

Run as yourself on the host unless a line says `sudo -u wt`.

**1.1 This repository is ready.**

- `Algorythmos-AI/crypto-lab` exists since 2026-10-11. `main` holds the extraction, or a later sync from the
  old repository (README, "Where this came from"), and the commit to deploy has green `test` and `ml` checks.
- `tools/from-monorepo/SOURCE.json` on that commit names the commit the host runs (1.2) or a later one whose
  desk files are the same, `tools/from-monorepo/drift.py` reported no drift there, and `tools/parity.sh` ended
  with `PARITY: every item holds`.

**1.2 The host runs the code this repository was extracted from.** [UNVERIFIED]

```bash
git -C /home/wt/trading rev-parse HEAD
# on a development machine, in a clone of trading-lab-research, with <host> that commit and <extracted> the
# commit in crypto-lab's tools/from-monorepo/SOURCE.json: this must print nothing
git diff --stat <extracted> <host> -- src/wt/crypto src/wt/ml config/crypto.yaml config/risk.yaml config/trial_registry.yaml
```

If it prints files, the state on the host was written by code this repository does not have, or the reverse.
Deploy the old repository or re-extract until it prints nothing.

**1.3 The old desk is healthy and its four timers are the ones expected.** [UNVERIFIED]

```bash
systemctl list-timers 'wt-*crypto*' --all --no-pager        # four timers: wt-crypto, wt-dashboard-crypto, wt-crypto-challengers, wt-crypto-learn
sudo -u wt /usr/local/bin/wt-deploy status                  # both 15-minute jobs ok, cadence [ok]
ls -la /home/wt/trading/var/crypto /home/wt/trading/var/dashboard-crypto /home/wt/trading/data/crypto
ls /home/wt/trading/var/crypto/{book.json,crypto_journal.jsonl}
ls /home/wt/trading/var/crypto/{KILL,latch,chain-broken,LEARNING_OFF,HARVEST_OFF} 2>/dev/null   # note which exist: they move too
```

A `chain-broken` flag means the evidence chain is flagged: resolve that first, in the old repository.

**1.4 The three directories and the new checkout are on one filesystem, with room.** [UNVERIFIED]

```bash
df -h /home/wt/trading/var /home/wt/trading/data /home/wt                 # the same "Filesystem" on all three lines
du -sh /home/wt/trading/var/crypto /home/wt/trading/var/dashboard-crypto /home/wt/trading/data/crypto
df -h /home/wt | tail -1                                                  # at least 5 GB free: a second checkout and two environments, above the jobs' 3 GB floor
ls -d /home/wt/crypto-lab 2>/dev/null && echo "EXISTS: stop and look at what it is"
```

Different filesystems would turn the rename into a copy: stop.

**1.5 The host has what this repository needs.** [UNVERIFIED]

```bash
dpkg -s libgomp1 restic 2>/dev/null | grep -E '^(Package|Status)'         # deploy/host-packages.txt
sudo -u wt bash -lc 'command -v uv || ls ~/.local/bin/uv'
sudo test -r /run/wt-secrets/env && sudo grep -cE '^(DASHBOARD_INGEST_URL|DASHBOARD_INGEST_SECRET|NTFY_TOPIC|HC_PING_KEY|RESTIC_REPOSITORY|RESTIC_PASSWORD|R2_ACCOUNT_ID|R2_ACCESS_KEY_ID|R2_SECRET_ACCESS_KEY)=' /run/wt-secrets/env   # 9
sudo test -r /etc/wt/paging && echo "paging file present"
systemctl list-unit-files 'cryptolab-*' --no-pager                        # "0 unit files listed"
ls /etc/cryptolab 2>/dev/null && echo "EXISTS: stop and look"
```

**1.6 The old repository's moved-marker guard is deployed.**

The guard makes the old code's four crypto jobs return before they take a lock or start anything, creating
nothing and publishing nothing, while a marker file exists. It was merged in `trading-lab-research` as commit
`c5d9830` (`wt.core.desk.moved_marker`, read by `wt.ops.jobs.run_job`). The marker is the desk's state
directory with `.MOVED` added to its name: `/home/wt/trading/var/crypto.MOVED`. Merged is not deployed, so
check the host's checkout:

```bash
MARKER=/home/wt/trading/var/crypto.MOVED
sudo -u wt git -C /home/wt/trading merge-base --is-ancestor c5d9830 HEAD && echo "the guard is deployed"
sudo -u wt env PYTHONPATH=/home/wt/trading/src /home/wt/trading/.venv/bin/python -c \
  'from wt.core.desk import DESKS, moved_marker; print(moved_marker(DESKS["crypto"]))'      # prints $MARKER
```

If the first line prints nothing, or the second prints another path or fails, the guard is not there or reads
another file: do not cut over. Deploy `trading-lab-research` first.

The guard covers jobs started by the scheduler. A module run by hand in the old checkout
(`python -m wt.crypto.cycle`) does not pass through it: after the cutover, run nothing of the desk's there.

**1.7 Healthchecks.** At healthchecks.io create one new check, slug `wt-crypto-backup`, cron `20 5 * * *` in
UTC, grace 2 hours. The checks `wt-crypto`, `wt-crypto-challengers` and `wt-crypto-learn` stay as they are: the
new units ping the same slugs. `wt-backup` stays too; it is the old repository's.

**1.8 A restore point.** The evening before, look at the old backup's last run and note the day of its last
anchor; step 4.5 reads that anchor back.

```bash
journalctl -u wt-backup -n 20 --no-pager | grep -E 'anchor|restore|failed'
```

## 2. Preparation (no downtime; the old desk keeps running)

**2.1 The checkout and its environments**, as `wt`. [UNVERIFIED: how the host reaches GitHub for this
repository; a public repository needs no credential]

```bash
sudo -u wt git clone https://github.com/Algorythmos-AI/crypto-lab.git /home/wt/crypto-lab
cd /home/wt/crypto-lab
sudo -u wt git rev-parse HEAD                                  # the commit checked in 1.1
sudo -u wt make bootstrap                                      # .venv from requirements.lock.txt, hashes checked
sudo -u wt make sync-venv                                      # writes the stamp preflight compares with the lockfile
sudo -u wt make sync-ml                                        # builds .venv-ml beside nothing and puts it in place: "ML environment: synced"
sudo -u wt .venv/bin/python -m pytest -m smoke -q              # the smoke tests the deploy runs
```

**2.2 It refuses to run, as it must.** The desk's state is not here yet.

```bash
sudo -u wt env WT_HOST=systemd WT_ENV_FILE=/run/wt-secrets/env make -C /home/wt/crypto-lab desk-check
#   NOT READY: the crypto desk has no state on this host (/home/wt/crypto-lab/var/crypto does not exist) ...
sudo -u wt env WT_HOST=systemd WT_ENV_FILE=/run/wt-secrets/env make -C /home/wt/crypto-lab preflight
#   every line [ok] for all four jobs
```

**Never run `make init` here.** It makes a new, empty desk; the cutover moves the existing one.

**2.3 Install the units without enabling anything.** [UNVERIFIED: the unit files were rendered and compared
with the old ones by text; no systemd has parsed them]

```bash
sudo /home/wt/crypto-lab/deploy/bin/cryptolab-install-units --no-enable
#   ... installed, NOT enabled (--no-enable): no timer of this repository was started, stopped or changed
systemd-analyze verify /etc/systemd/system/cryptolab-*.service /etc/systemd/system/cryptolab-*.timer
systemctl list-timers 'cryptolab-*' --all --no-pager           # listed, none active
systemctl list-timers 'wt-*' --no-pager | head -20             # the old timers, unchanged
```

`systemd-analyze verify` must print nothing about the `cryptolab-*` files. If it does, stop: nothing is running
from them, and the old desk is untouched.

## 3. The cutover (inside the slot chosen above)

Set these once in the shell you will use:

```bash
OLD=/home/wt/trading; NEW=/home/wt/crypto-lab
MARKER=/home/wt/trading/var/crypto.MOVED         # the path from 1.6
FOUR="wt-crypto wt-dashboard-crypto wt-crypto-challengers wt-crypto-learn"
```

**3.1 Write down where the journals stand.**

```bash
for f in $OLD/var/crypto/crypto_journal.jsonl $OLD/var/crypto/harvest/harvest_journal.jsonl; do
  echo "$f $(wc -l < $f) lines, last line sha256 $(tail -n 1 $f | tr -d '\n' | sha256sum | cut -c1-16)"; done | tee /tmp/cutover-before.txt
```

**3.2 Disable all four old timers, and see that no old job is running.**

```bash
for u in $FOUR; do sudo systemctl disable --now $u.timer; done
for u in $FOUR; do printf '%s: %s\n' $u "$(systemctl is-active $u.service)"; done      # four times "inactive"
systemctl list-timers 'wt-*crypto*' --no-pager                                        # "0 timers listed"
```

If a service says `activating` or `active`, a job had just started: wait until it says `inactive`. Do not stop
it by hand; a bar cycle that is killed half way repairs itself on its next run, but there will be no next run
in the old checkout. `failed` means the unit's last run ended badly and it is not running now: look at why
(`journalctl -u <unit> -n 30`) before going on, because the desk should be healthy when it moves (1.3).

**3.3 Create the marker.**

```bash
sudo -u wt touch "$MARKER" && ls -la "$MARKER"
```

From here an old crypto job that starts anyway does nothing.

**3.4 Move the three directories. Move, not copy.**

```bash
sudo -u wt mkdir -p $NEW/var $NEW/data
ls -d $NEW/var/crypto $NEW/var/dashboard-crypto $NEW/data/crypto 2>/dev/null && echo "A DESTINATION EXISTS: stop"
sudo -u wt mv -T $OLD/var/crypto            $NEW/var/crypto
sudo -u wt mv -T $OLD/var/dashboard-crypto  $NEW/var/dashboard-crypto
sudo -u wt mv -T $OLD/data/crypto           $NEW/data/crypto
ls -d $OLD/var/crypto $OLD/var/dashboard-crypto $OLD/data/crypto 2>/dev/null && echo "STILL IN THE OLD CHECKOUT: stop"
```

`mv -T` refuses to move a directory *into* an existing one. If any `mv` fails, nothing has started yet: look at
the message, and either finish the move or put back what was moved (the same three commands with the paths
swapped), remove the marker and re-enable the four timers (5.4).

**3.5 The desk that arrived is the desk that left.**

```bash
for f in $NEW/var/crypto/crypto_journal.jsonl $NEW/var/crypto/harvest/harvest_journal.jsonl; do
  echo "$f $(wc -l < $f) lines, last line sha256 $(tail -n 1 $f | tr -d '\n' | sha256sum | cut -c1-16)"; done
cat /tmp/cutover-before.txt                                    # the same counts and the same hashes
sudo -u wt env WT_HOST=systemd WT_ENV_FILE=/run/wt-secrets/env make -C $NEW desk-check
#   the crypto desk has state in /home/wt/crypto-lab/var/crypto: jobs may run
```

If `desk-check` says NOT READY, do not go on: the message names what is missing. Nothing is running; go to 5.

**3.6 Enable the new units.**

```bash
sudo cryptolab-install-units
systemctl list-timers 'cryptolab-*' --no-pager                 # six timers with a next run: four jobs, backup, restore-test
```

The installer refuses if the gate is closed or the desk has no state, and says which.

## 4. Verification

**4.1 The first cycle here** (at the next `:00:10`, `:15:10`, `:30:10` or `:45:10`):

```bash
journalctl -u cryptolab-crypto -n 30 --no-pager                # "start crypto ...", "cycle <time>: evaluated [...]", "end crypto: exit 0"
tail -n 1 $NEW/var/crypto/crypto_journal.jsonl | head -c 300   # a row of this cycle; kind "cycle" or "sleeve", "t" a few seconds after the bar closed
sudo -u wt env WT_HOST=systemd WT_ENV_FILE=/run/wt-secrets/env make -C $NEW status
#   Desk state: ok ...; crypto ok; no "refused"; preflight all [ok]
sudo -u wt env PYTHONPATH=$NEW/src $NEW/.venv/bin/python -c "
from wt.core import ledger; from wt.core.desk import DESKS
for n, p in DESKS['crypto'].ledgers: print(n, ledger.head(p)[0], 'lines', ledger.verify_chain(p) or 'chain intact')"
```

The line count is the one from 3.1 plus this cycle's rows, and both chains are intact: the new rows continue
the old chain.

`make status` also prints two cadence lines, and both say `[FAIL]` for about the first 45 minutes: they count
this checkout's runs in the last hour and want three of four, and its run history started empty. They turn
`[ok]` after the third cycle and the third publish. Nothing pages on them.

**4.2 The snapshot** (a minute later):

```bash
journalctl -u cryptolab-dashboard-crypto -n 10 --no-pager      # "crypto snapshot <run id>: ... bytes", "sent: True (HTTP 200)"
```

On the dashboard the Crypto pages show a new run id within about 16 minutes of the cutover, and the same book,
positions and history as before. The job list on those pages starts empty and fills from here (expected).

**4.3 The old side stays quiet.**

```bash
systemctl list-timers 'wt-*crypto*' --no-pager                 # 0 timers
ls -d $OLD/var/crypto 2>/dev/null && echo "RECREATED: a second desk has started; go to 5.5"
sudo -u wt /usr/local/bin/wt-deploy status                     # the stocks jobs as before; no crypto cadence line
```

Look again after an hour, and after the next deploy of the old repository.

**4.4 Healthchecks and pages.** `wt-crypto` goes green at the first cycle. Alerts that were firing at the
cutover may fire once more from here; a job-level alert no longer shows on any dashboard page (it still pages).

**4.5 The next morning.**

```bash
systemctl list-timers 'cryptolab-*' --no-pager
journalctl -u cryptolab-crypto-challengers -u cryptolab-crypto-learn --since yesterday --no-pager | grep -E 'start|end|REFUSED'
journalctl -u cryptolab-backup -n 20 --no-pager                # "anchor: anchored anchors/crypto-lab/<UTC day>.json"
# the journal that moved is the one the old checkout last anchored (the day noted in 1.8, a New York date):
cd /home/wt/crypto-lab && sudo -u wt env WT_HOST=systemd WT_ENV_FILE=/run/wt-secrets/env PYTHONPATH=src .venv/bin/python -m wt.ops.backup extends anchors/crypto/<day>.json
#   ... the ledgers on disk EXTEND it (only grown, chains intact)
journalctl -u wt-backup -n 10 --no-pager                       # the old backup still succeeds; it now covers the stocks ledgers only
```

`wt-crypto-backup`, `wt-crypto-challengers` and `wt-crypto-learn` are green at healthchecks.io.

**4.6 After 24 clean hours** tell the session that maintains `trading-lab-research`: the desk's code can be
removed there. Until that is deployed, risk 2 above stands.

## 5. Rollback

Possible at any time until the desk's code is removed from `trading-lab-research`. The state the new checkout
wrote is readable by the old code: it is the same code. Choose a slot by the same rules as the cutover.

**5.1 Stop the new desk.**

```bash
NEWT="cryptolab-crypto cryptolab-dashboard-crypto cryptolab-crypto-challengers cryptolab-crypto-learn cryptolab-backup cryptolab-restore-test"
for u in $NEWT; do sudo systemctl disable --now $u.timer; done
for u in $NEWT; do printf '%s: %s\n' $u "$(systemctl is-active $u.service)"; done      # all "inactive"; wait if not
```

**5.2 Move the three directories back.**

```bash
ls -d $OLD/var/crypto $OLD/var/dashboard-crypto $OLD/data/crypto 2>/dev/null && echo "A DESTINATION EXISTS: stop (5.5)"
sudo -u wt mv -T $NEW/var/crypto            $OLD/var/crypto
sudo -u wt mv -T $NEW/var/dashboard-crypto  $OLD/var/dashboard-crypto
sudo -u wt mv -T $NEW/data/crypto           $OLD/data/crypto
```

**5.3 Remove the marker.** `sudo -u wt rm "$MARKER"`

**5.4 Re-enable the four old timers.**

```bash
for u in $FOUR; do sudo systemctl enable --now $u.timer; done
systemctl list-timers 'wt-*crypto*' --no-pager                 # four timers with a next run
```

Then verify as in 4.1 to 4.4 with the old unit names (`journalctl -u wt-crypto`, `wt-deploy status`). The
anchors written under `anchors/crypto-lab/` stay in the bucket; they are write-once and harmless. The new
checkout and its units may stay installed and disabled; `sudo cryptolab-install-units` would refuse to enable
them without the state.

**5.5 If a second desk has started** (an old or a new job ran against a missing directory and created one):
disable every crypto timer of both prefixes first. The directory that holds the long journal (compare
`wc -l` with 3.1) is the desk; the other holds a book of exactly US$10,000 and a journal a few rows long.
Move the stray one aside under another name (do not delete it), put the real one where the code you are
keeping expects it, and only then enable that side's timers. Tell the dashboard's maintainer: snapshots of the
stray desk were published in between.

## Effects that are expected

- Alerts that were firing at the cutover fire once more; a once-a-day notice can repeat that day.
- The snapshot's job history starts empty, and for the same reason `make status` shows the two cadence lines
  as `[FAIL]` until three cycles and three publishes have run here.
- The "Crypto:" line leaves the stocks daily summary.
- Job-level alerts of the four jobs are on no dashboard page any more (they were listed with the stocks desk's).
- The desk's anchors continue under `anchors/crypto-lab/<UTC day>.json`; `anchors/crypto/` ends on the last
  New York day before the cutover.
- Crypto alert keys that were firing in the old checkout stay recorded as firing in its `var/alerts/state.json`,
  where nothing resolves them. They are not shown while the old code still knows the crypto desk; the change
  that removes the desk there has to clear them, or they will be read as the stocks desk's.

## What could not be verified

Everything below was taken from the two repositories' code and runbooks, not from the host.

| Assumption | Where it matters | The check |
|---|---|---|
| The old checkout is `/home/wt/trading`, the job user is `wt`, the secrets are in `/run/wt-secrets/env`, the paging file is `/etc/wt/paging` | every step | 1.3, 1.5 |
| The host runs the commit this repository was extracted from, or one with identical desk files | the state is read by the same code | 1.2 |
| `var/crypto`, `var/dashboard-crypto`, `data/crypto` and `/home/wt` are one filesystem | the move is a rename | 1.4 |
| `/home/wt/crypto-lab` and `/etc/cryptolab` do not exist | preparation | 1.4, 1.5 |
| There is room for a second checkout with two environments | preparation; the jobs' 3 GB floor | 1.4 |
| `libgomp1`, `restic` and `uv` are installed | the ML environment, the backup, the environments | 1.5 |
| The secrets file holds the nine values this repository reads | publish, paging, healthchecks, backup | 1.5 |
| The moved-marker guard (`trading-lab-research` `c5d9830`) is deployed on the host and reads `var/crypto.MOVED` there. It exists and is tested upstream; whether the host runs it is not known here | risk 1 and risk 2; 3.3, 5.3 | 1.6 |
| The `cryptolab-*` units parse and behave under the host's systemd as their text says. They were compared with the old units' text and differ only in names and paths; the two backup timers use `OnCalendar` forms (`*-*-* 05:20 UTC`, `Sun 06:20 UTC`) that no systemd has parsed | 2.3, 3.6 | `systemd-analyze verify` in 2.3 |
| `cryptolab-install-units` runs correctly as root on the host. It was checked with shellcheck and never executed | 2.3, 3.6 | 2.3 runs it first with `--no-enable`, which starts nothing |
| `mv -T` (GNU coreutils) is available | 3.4 | it is on Ubuntu; `mv --version` |
| The host can clone this repository from GitHub, and its deploys can read the commit's checks (public API, or `GH_TOKEN`) | 2.1, later deploys | 2.1 |
| The anchor bucket's lock and the backup token's rights cover the new path `anchors/crypto-lab/` | 4.5 | the first backup's log line |
| The restic repository accepts a second host name from the same machine | 4.5 | the first backup's log line |
| Whether a masked timer survives `wt-install-units`. Not relied on: the marker is the guard | risk 2 | none |
| No daily job of the old desk is running in the chosen slot | 3.2 | 3.2 |
