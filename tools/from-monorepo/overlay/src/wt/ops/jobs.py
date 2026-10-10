"""Run a job the way the service manager should: locked, pre-flighted, time-boxed, observed.

    python -m wt.ops.jobs run <crypto|dashboard-crypto|crypto-challengers|crypto-learn> [--preflight-only]
    python -m wt.ops.jobs status

For every run:
  1. Wait out a deploy, then take the job's own lock. If another run holds it, this is a refusal.
  2. Refuse when the desk has no state (`wt.ops.init`, CRY-DEC-0001): a job never makes a desk by running.
  3. Run the preflight checks (`wt.ops.preflight`). A failure is a refusal: one alert per job per day, exit 0.
  4. Run the job as a child process under a deadline, with its output appended to logs/<log>_<YYYYMMDD>.log.
  5. Inspect the outcome: the exit code. Alerts fire on transitions and resolve when the problem clears.
  6. Write the heartbeat.

Exit code: the child's exit code, 0 for a refusal, 124 for a deadline kill.

Origin: trading-lab-research `src/wt/ops/jobs.py` at c5d9830 (trimmed). Removed: everything that belongs to
the stocks jobs (the market-calendar lookup at the start of every run, the wait for the close, the paper
session's journal checks and exits-only start, the follower jobs, the weekly evidence step, the daily summary
with its crypto line), the broker read, and the early return for a desk that has moved out of that
repository. Added: step 2, and the removal of a new desk's mark after its first
bar cycle. Unchanged for the four crypto jobs: the deploy wait, the job lock, the heartbeat, preflight, the
child's environment and deadline, the signal handling, the healthchecks slugs and the alert keys.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from wt.core.clock import ET
from wt.core.config import ROOT
from wt.core.desk import DESKS, installed
from wt.ops import hc, init, preflight
from wt.ops.alerts import Alerts
from wt.ops.heartbeat import Heartbeat, last_runs, ok_runs_since
from wt.ops.locks import DEPLOY_LOCK, job_lock
from wt.ops.schedule import JOBS, PY, SYDNEY, Job

DEPLOY_WAIT_S = 30 * 60           # a job due while a deploy switches the checkout waits for it, then starts
KILL_GRACE_S = 30
CYCLE_JOB = "crypto"              # the job that writes the desk's book: a new desk stops being new after it


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _sha(root: Path) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Log:
    """Appends wrapper lines to the job's dated log. Never raises (a full disk must not kill the job)."""

    def __init__(self, root: Path, job: Job) -> None:
        self.path = root / "logs" / f"{job.log}_{dt.datetime.now(SYDNEY):%Y%m%d}.log"

    def __call__(self, msg: str) -> None:
        line = f"[jobs {dt.datetime.now(ET):%Y-%m-%d %H:%M:%S %Z}] {msg}"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        print(line, file=sys.stderr, flush=True)


def deadline_for(job: Job, start: dt.datetime) -> dt.datetime | None:
    """Wall-clock limit for the job's work."""
    return start + dt.timedelta(minutes=job.deadline_min) if job.deadline_min else None


def run_child(job: Job, root: Path, log: Log, deadline: dt.datetime | None,
              extra_env: dict[str, str] | None = None) -> tuple[int, bool, bool]:
    """Run the job's command. Returns (exit code, killed_at_deadline, stopped_by_sigterm).

    A deadline stops the job's process group with SIGTERM, then SIGKILL after KILL_GRACE_S (exit 124).
    A SIGTERM to this runner (systemd stopping the unit, KillMode=mixed) is forwarded to the job's process group
    the same way (exit 143), so the runner survives long enough to write its heartbeat and page."""
    env = {**os.environ, "PYTHONPATH": "src", "MODE": job.mode, "PYTHONUNBUFFERED": "1",
           "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
    log(f"start {job.name}: {' '.join(job.command)} (MODE={job.mode}, deadline "
        f"{deadline.astimezone(ET):%H:%M} ET)" if deadline else f"start {job.name}: {' '.join(job.command)}")
    try:
        out = open(log.path, "a")
    except OSError:
        out = open(os.devnull, "w")
    with out:
        proc = subprocess.Popen([str(root / PY), *job.command], cwd=root, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        stop: dict[str, float | None] = {"sigterm": None, "deadline": None}

        def _killpg(sig: int) -> None:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

        def _on_sigterm(signum: int, frame: Any) -> None:
            if stop["sigterm"] is None:
                stop["sigterm"] = time.monotonic()
                _killpg(signal.SIGTERM)

        prev = None
        if threading.current_thread() is threading.main_thread():
            prev = signal.signal(signal.SIGTERM, _on_sigterm)
        try:
            limit = None if deadline is None else time.monotonic() + max(1.0, (deadline - _now()).total_seconds())
            while True:
                try:
                    code = proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if limit is not None and now >= limit and stop["deadline"] is None:
                    log(f"deadline reached: stopping {job.name} (pid {proc.pid})")
                    stop["deadline"] = now
                    _killpg(signal.SIGTERM)
                first = min((t for t in (stop["sigterm"], stop["deadline"]) if t is not None), default=None)
                if first is not None and now - first > KILL_GRACE_S:
                    _killpg(signal.SIGKILL)
                    code = proc.wait()
                    break
        finally:
            if prev is not None:
                signal.signal(signal.SIGTERM, prev)
        if stop["sigterm"] is not None:
            log(f"stopped by SIGTERM: {job.name} (pid {proc.pid}) ended with {code}")
            return 143, False, True
        if stop["deadline"] is not None:
            return 124, True, False
        return code, False, False


# Jobs with a healthchecks.io check. `crypto` pings every 15 minutes, at any hour: its check has no schedule gap.
# The two daily jobs alert when they fail; only a check notices when one never starts (a timer disabled or never
# installed). The publish job has none: the dashboard's own watchdog notices a missing snapshot.
HC_JOBS = frozenset({"crypto", "crypto-challengers", "crypto-learn"})


def hc_slug(job: Job) -> str:
    """The check's name. It keeps the form the jobs had before they moved here, so the same checks carry on."""
    return f"wt-{job.name}"


def refuse(job: Job, reason: str, alerts: Alerts, log: Log, hb: Heartbeat | None = None, ping: bool = True) -> int:
    """A refusal exits 0 (systemd must not retry it) but is not a success: it pings the check's /fail."""
    log(f"REFUSED {job.name}: {reason}")
    alerts.once_per_day(f"refuse:{job.name}", f"{job.name} refused to run", reason, 3)
    if hb:
        hb.finish("refused", 0, reason)
    if ping and job.name in HC_JOBS:
        hc.ping(hc_slug(job), "fail", f"refused: {reason}")
    return 0


def run_job(job: Job, root: Path = ROOT, preflight_only: bool = False, alerts: Alerts | None = None) -> int:
    alerts = alerts or Alerts()
    log = Log(root, job)
    if preflight_only:
        checks = preflight.run_checks(root)
        for c in checks:
            print(f"  [{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
        return 0 if not preflight.failures(checks) else 1

    # Wait out a deploy before taking the job lock: a deploy holds the jobs' locks while it changes the checkout
    # (wt.ops.deploy.quiesced), and a job that already held its own would make it wait for nothing.
    waited = time.monotonic()
    with job_lock(DEPLOY_LOCK, wait_s=DEPLOY_WAIT_S) as free:   # released at once: only waits out a deploy
        if not free:
            return refuse(job, "a deploy is still changing the checkout after 30 minutes", alerts, log)
    if (waited := time.monotonic() - waited) > 1:
        log(f"waited {waited:.0f}s for a deploy to finish")
    with job_lock(job.name) as got:
        if not got:
            # not a failure: the run that holds the lock is the one that pings
            return refuse(job, "another run of this job is still in progress", alerts, log, ping=False)
        hb = Heartbeat(job.name, _sha(root))
        desk = DESKS[job.desk]
        if (why := init.problem(desk)) is not None:
            # CRY-DEC-0001: no state, no run. Nothing below this line may make a desk.
            return refuse(job, why, alerts, log, hb)
        fails = preflight.failures(preflight.run_checks(root)) if job.preflight else []
        if fails:
            return refuse(job, "; ".join(f"{c.name}: {c.detail}" for c in fails), alerts, log, hb)

        work_start = _now()
        try:
            code, killed, terminated = run_child(job, root, log, deadline_for(job, work_start))
        except OSError as e:            # the child could not start (missing interpreter, fork failure, ...)
            log(f"could not start {job.name}: {e.__class__.__name__}: {e}")
            code, killed, terminated = 127, False, False
        log(f"end {job.name}: exit {code}{' (deadline kill)' if killed else ''}{' (SIGTERM)' if terminated else ''}")

        if job.name in HC_JOBS:
            hc.ping(hc_slug(job), "" if code == 0 else "fail", f"exit {code}")
        key = f"job:{job.name}"
        if code == 0:
            alerts.resolve(key, f"{job.name} recovered", f"{job.name} completed normally.")
            if job.name == CYCLE_JOB and init.settle(desk):
                log("the new desk has its book and its journal: it is an existing desk from now on")
            hb.finish("ok", 0)
        else:
            what = ("was stopped at its deadline" if killed else
                    "was stopped by the system (SIGTERM)" if terminated else f"exited with code {code}")
            alerts.fire(key, f"{job.name} failed", f"{job.name} {what}. See {log.path.relative_to(root)}.", 4)
            hb.finish("timeout" if killed else "terminated" if terminated else "failed", code)
    return code


def cadence(now: dt.datetime | None = None, heartbeats: Path | None = None) -> list[str]:
    """Each interval job against its own interval over the last hour. One missed run is allowed (a deploy, a slow
    venue); fewer means the scheduler isn't starting it, which no job-level alert can see."""
    now = now or dt.datetime.now(dt.UTC)
    out = []
    for j in JOBS.values():
        if j.interval_s and installed(DESKS[j.desk]):       # a desk this host doesn't run has nothing to be late
            want = 3600 // j.interval_s
            got = ok_runs_since(j.name, now - dt.timedelta(hours=1), heartbeats)
            out.append(f"  [{'ok' if got >= want - 1 else 'FAIL'}] {j.name} cadence: {got} of {want} runs ok in the last hour")
    return out


def status(root: Path = ROOT) -> int:
    """What's going on, in the terminal: the desk's state, last runs, firing alerts, kill switch, preflight."""
    desk = DESKS["crypto"]
    why = init.problem(desk)
    print(f"Desk state: {'ok (' + str(desk.state_dir) + ')' if why is None else 'NOT READY: ' + why}")
    print(f"KILL switch: {'ON (no new entries; exits are still managed)' if desk.kill_file.exists() else 'off'}")
    print("Last runs:")
    for name, r in last_runs().items():
        print(f"  {name:18} {r.get('status', '?'):8} exit={r.get('exit')} started={r.get('started')} "
              f"ended={r.get('ended')} sha={r.get('sha')}  {r.get('detail', '')[:80]}")
    for line in cadence():
        print(line)
    firing = Alerts().firing()
    print(f"Alerts firing: {', '.join(sorted(firing)) or 'none'}")
    print("Preflight:")
    for c in preflight.run_checks(root):
        print(f"  [{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.ops.jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("job", choices=sorted(JOBS))
    r.add_argument("--preflight-only", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        return status()
    return run_job(JOBS[a.job], preflight_only=a.preflight_only)


if __name__ == "__main__":
    sys.exit(main())
