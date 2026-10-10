"""Deploy reviewed code into the live checkout without changing it under a running job.

    python -m wt.ops.deploy gate                          # print whether a deploy is allowed now, and why not
    python -m wt.ops.deploy deploy [--sha SHA] [--stage]  # one transaction: see below
    python -m wt.ops.deploy rollback <tag>                # back to a runtime-* tag (same gate, same checks)
    python -m wt.ops.deploy sync-ml                       # finish or retry the ML environment's sync

The desk is always open (CRY-DEC-0001): there is no market calendar here and no hour of the day when a deploy is
forbidden. The gate refuses only while one of the two daily jobs is running, because they run for up to an hour
and a half and a deploy does not wait that long. The two 15-minute jobs are short: a deploy waits for one in
flight and holds every job's lock while it changes the checkout (`quiesced`); a job that fires meanwhile waits
for the deploy (`wt.ops.jobs`). A bar cycle missed during a deploy is made up by the next one.

A deploy is one transaction on one exact commit:
  1. the target (--sha, default the tip of main) must be on main, a fast-forward of the live checkout, and green:
     every required GitHub check (`test` and `ml`) concluded "success" on that commit (wt.ops.ci);
  2. the snapshot contract must be the one the live checkout has, or the one the owner names (wt.ops.contract):
     the dashboard that receives the snapshot lives in another repository and rejects fields it does not know;
  3. --stage: the full test suite runs in a throwaway worktree of the target with its own locked venv, BEFORE the
     live checkout changes; a red suite changes nothing;
  4. under the deploy lock, with the gate checked again: tag, fast-forward to exactly the target (never a second
     fetch), sync the venv, smoke test. A failed smoke test rolls back to the tag inside the same lock, without
     asking the gate again: restoring the code that was running is always allowed.

Origin: trading-lab-research `src/wt/ops/deploy.py` at 217a4eb (trimmed). Removed: the equity trading window and
its calendar (`wt.ops.window`), the check of the live dashboard's build (`wt.ops.dashguard`; replaced by step 2),
the runtime-state migration, the Mac's agents, and the broker clean-up before a rollback. Unchanged: `quiesced`,
`sync_venv`, `sync_ml` and everything it uses, `smoke`, `stage_tests`, `resolve_target`, the tag and record
names, and the order of the transaction.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from wt.core.config import ROOT, STATE_DIR
from wt.ops import ci, contract, host, preflight
from wt.ops.alerts import Alerts
from wt.ops.locks import DEPLOY_LOCK, held, job_lock
from wt.ops.schedule import JOBS, PY

DEPLOY_DIR = STATE_DIR / "deploy"
STAGE_DIR = STATE_DIR / "stage"
STAGE_TIMEOUT_S = 1800.0
CI_OVERRIDE_ENV = "WT_DEPLOY_CI_OVERRIDE"       # owner only: deploy a commit whose checks can't be read (reason)
TAGGER = ("-c", "user.name=cryptolab-deploy", "-c", "user.email=cryptolab-deploy@localhost")   # a host may have no git identity


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _run(*cmd: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(list(cmd), cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def long_jobs() -> list[str]:
    """The jobs a deploy does not wait for: the ones on a clock time, which run for up to an hour and a half."""
    return [j.name for j in JOBS.values() if not j.interval_s]


def running_jobs() -> dict[str, bool]:
    """Which of the long jobs the service manager shows running (wt.ops.host); fails closed when it can't tell."""
    return host.current().running_jobs([JOBS[n] for n in long_jobs()])


def gate_blockers() -> list[str]:
    """Why a deploy may not start now; empty when it may. No clock and no calendar: only what is running."""
    out = [f"{name} is running" for name, on in sorted(running_jobs().items()) if on]
    out += [f"{name} holds its lock (a run is in progress)" for name in held(long_jobs())]
    return out


QUIESCE_WAIT_S = 120.0


@contextlib.contextmanager
def quiesced(wait_s: float | None = None) -> Iterator[list[str]]:
    """Hold the lock of every job the gate does not wait for (the interval jobs and the daily runs),
    waiting for a run in flight to finish. Those are not trading jobs, so the gate lets a deploy start while one
    runs; without this the checkout (and the venv) would change under it. Yields the jobs still busy after
    `wait_s` (empty = quiet)."""
    wait_s = QUIESCE_WAIT_S if wait_s is None else wait_s
    with contextlib.ExitStack() as stack:
        busy = [j.name for j in JOBS.values()
                if not j.trading and not stack.enter_context(job_lock(j.name, wait_s=wait_s, poll_s=2.0))]
        yield busy


def sync_venv() -> None:
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    _run(uv, "pip", "sync", "--python", str(ROOT / PY), "requirements.lock.txt")
    (ROOT / preflight.LOCK_STAMP).write_text(preflight.lock_hash(ROOT) + "\n")


ML_LOCK, ML_VENV = "requirements-ml.lock.txt", ".venv-ml"
ML_NEXT, ML_PREV = ".venv-ml.next", ".venv-ml.prev"
# Held while a deploy tests its target before the switch. Staging runs for minutes, outside the deploy lock (which
# every job waits on), and starts by clearing its worktree: without this a second deploy of the same commit removed
# the first one's worktree under its running tests, and that first deploy was refused as "tests failed".
STAGE_LOCK = "deploy-stage"
ML_ENV_LOCK = "ml-env"                  # one build at a time; not the deploy lock, which every job waits on
ML_USERS = ("crypto-learn",)            # jobs that run for long in the ML environment: the swap waits for them
SWAP_WAIT_S = 120.0
ML_IMPORT_CHECK = "import sklearn, lightgbm"       # what the learning job and the scorer import


def sync_ml(swap_wait_s: float | None = None) -> str:
    """Bring the machine-learning environment (DEC-0016) in line with its own hashed lockfile: "none" when the
    checkout has no such lockfile, "current" when nothing changed, "synced", "pending" (built, not yet swapped
    in), "busy" (another build is running) or "failed".

    It is a separate environment so that the trading one stays exactly its lockfile. A failure here never fails
    the deploy: the trading jobs do not import it, and the scorer falls back to unfiltered trading when it is
    missing (wt.crypto.scorer).

    Nothing here holds a lock a trading job waits on. Every job waits for the deploy lock before it starts, so
    this runs after that lock is released, under a lock of its own: a large install delays no job. The new
    environment is built beside the live one and put in its place by two renames, so the scorer sees the old
    one or the new one and never one that is half installed. The renames wait for the learning job, which
    trains in the environment for up to an hour; if it is still running the built environment is kept and the
    next call (the next deploy, or `make sync-ml`) only has to swap it in."""
    lock = ROOT / ML_LOCK
    if not lock.exists():
        return "none"
    want = hashlib.sha256(lock.read_bytes()).hexdigest()
    live, nxt, prev = ROOT / ML_VENV, ROOT / ML_NEXT, ROOT / ML_PREV
    stamp = live / ".lock-sha256"
    if stamp.exists() and stamp.read_text().strip() == want:
        return "current"
    with job_lock(ML_ENV_LOCK) as mine:
        if not mine:
            return "busy"
        try:
            built = nxt / ".lock-sha256"
            if not (built.exists() and built.read_text().strip() == want):
                uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
                shutil.rmtree(nxt, ignore_errors=True)
                _run(uv, "venv", "--quiet", "--python", str(ROOT / PY), str(nxt))
                _run(uv, "pip", "sync", "--quiet", "--python", str(nxt / "bin" / "python"), "--require-hashes",
                     str(lock), timeout=1800)
                # Installed is not usable: a wheel can need a system library the host lacks (LightGBM needs
                # OpenMP, `libgomp1` on Debian). An environment that cannot import is never swapped in.
                _run(str(nxt / "bin" / "python"), "-c", ML_IMPORT_CHECK, timeout=120)
                built.write_text(want + "\n")
            wait = SWAP_WAIT_S if swap_wait_s is None else swap_wait_s
            with contextlib.ExitStack() as stack:
                if not all(stack.enter_context(job_lock(n, wait_s=wait, poll_s=2.0)) for n in ML_USERS):
                    Alerts().fire("deploy-ml", "ML environment built, not yet in use",
                                  "The learning job is running in the old one. Trading is unaffected. The next deploy "
                                  "swaps the new one in, or run: make sync-ml", 3)
                    return "pending"
                shutil.rmtree(prev, ignore_errors=True)
                if live.exists():
                    os.rename(live, prev)
                os.rename(nxt, live)
            shutil.rmtree(prev, ignore_errors=True)
            Alerts().resolve("deploy-ml", "ML environment in sync", "The machine-learning environment matches its lockfile.")
            return "synced"
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            if not live.exists() and prev.exists():             # the second rename failed: put the old one back
                with contextlib.suppress(OSError):
                    os.rename(prev, live)
            shutil.rmtree(nxt, ignore_errors=True)
            Alerts().fire("deploy-ml", "ML environment did not sync",
                          f"{str(e)[:200]}. Trading is unaffected; models are not scored until the next deploy fixes it.", 3)
            return "failed"


def _note_ml(rec: Path | None) -> None:
    """Sync the ML environment and write the outcome into the deploy's record."""
    state = sync_ml()
    if rec is not None and rec.exists():
        rec.write_text(json.dumps({**json.loads(rec.read_text()), "ml_env": state}, indent=1, default=str))
    if state in ("synced", "failed", "pending", "busy"):
        print(f"ML environment: {state}" + ("  (finish with: make sync-ml)" if state in ("pending", "busy") else ""))


def smoke() -> tuple[bool, str]:
    r = _run(str(ROOT / PY), "-m", "pytest", "-m", "smoke", "-q", check=False, timeout=900)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-600:]


def next_tag() -> str:
    day = _now().strftime("%Y%m%d")                 # UTC: the same name whichever host deploys
    existing = _run("git", "tag", "--list", f"runtime-{day}-*").stdout.split()
    return f"runtime-{day}-{len(existing) + 1}"


def record(body: dict[str, object]) -> Path:
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    p = DEPLOY_DIR / f"{_now():%Y%m%dT%H%M%SZ}.json"
    p.write_text(json.dumps(body, indent=1, default=str))
    return p


def _refuse_if_closed() -> bool:
    if blockers := gate_blockers():
        print("Deploy gate CLOSED:\n  " + "\n  ".join(blockers))
        return True
    return False


def is_ancestor(a: str, b: str) -> bool:
    return _run("git", "merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def stage_tests(target: str, timeout_s: float = STAGE_TIMEOUT_S) -> tuple[bool, str]:
    """The full test suite on `target`, in a throwaway worktree with its own venv synced from the target's hashed
    lockfile (uv's cache makes that cheap). The live checkout and its venv are never touched. The suite runs with
    a minimal environment: no secrets file, the worktree's own state dir."""
    d = STAGE_DIR / target[:12]
    _run("git", "worktree", "remove", "--force", str(d), check=False)
    shutil.rmtree(d, ignore_errors=True)
    _run("git", "worktree", "prune", check=False)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _run("git", "worktree", "add", "--detach", "--force", str(d), target)
        uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
        _run(uv, "venv", "--quiet", "--python", str(ROOT / PY), str(d / ".venv"))
        _run(uv, "pip", "sync", "--quiet", "--python", str(d / PY), "--require-hashes", str(d / "requirements.lock.txt"))
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
        env.update(PYTHONPATH="src", PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([str(d / PY), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="], cwd=d,
                           env=env, capture_output=True, text=True, timeout=timeout_s)
        tail = (r.stdout + r.stderr).strip()[-600:]
        return r.returncode == 0, tail
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        return False, f"staging failed: {e}"[:600]
    finally:
        _run("git", "worktree", "remove", "--force", str(d), check=False)
        shutil.rmtree(d, ignore_errors=True)
        _run("git", "worktree", "prune", check=False)


def resolve_target(sha: str | None) -> tuple[str | None, str]:
    """(target commit, why not) after fetching main: on main, a fast-forward of HEAD, not HEAD itself."""
    _run("git", "fetch", "--quiet", "origin", "main")
    main_head = _run("git", "rev-parse", "origin/main").stdout.strip()
    if sha is None:
        target = main_head
    else:
        r = _run("git", "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
        if r.returncode != 0:
            return None, f"{sha} is not a commit here"
        target = r.stdout.strip()
    if not is_ancestor(target, main_head):
        return None, f"{target[:12]} is not on main"
    head = _run("git", "rev-parse", "HEAD").stdout.strip()
    if head == target:
        return None, f"already at {target[:12]}"
    if not is_ancestor(head, target):
        return None, f"{target[:12]} does not follow the live checkout {head[:12]} (use rollback to go back)"
    return target, ""


def deploy(sha: str | None = None, stage: bool = False) -> int:
    if _refuse_if_closed():
        return 2
    git = [c for c in preflight.check_git(ROOT) if c.name in ("on main", "clean code") and not c.ok]
    if git:
        print("Refusing: " + "; ".join(f"{c.name}: {c.detail}" for c in git))
        return 2
    target, why = resolve_target(sha)
    if target is None:
        print(f"Nothing deployed: {why}")
        return 0 if why.startswith("already at") else 2
    green = ci.check(target)
    ci_override = os.environ.get(CI_OVERRIDE_ENV, "").strip()
    if not green.green and not ci_override:
        print(f"Refusing: {target[:12]} is not green ({green.detail})\n  (owner override: {CI_OVERRIDE_ENV}='reason')")
        return 2
    agreed = contract.guard(ROOT, target)
    if not agreed.ok:
        print(f"Refusing: {agreed.why}")
        return 2
    if agreed.acknowledged:
        print(f"Snapshot contract changes with this deploy, acknowledged by the owner: {agreed.live} -> {agreed.target}")
    if stage:
        print(f"staging {target[:12]}: the full test suite in a throwaway worktree ...", flush=True)
        with job_lock(STAGE_LOCK) as mine:
            if not mine:
                # Not a failed suite: nothing was tested, so no alert and no record. The other deploy decides.
                print("Refusing: another deploy is testing a commit before its switch; wait for it to finish, "
                      "then run this again (it will say so if there is nothing left to deploy)")
                return 2
            passed, out = stage_tests(target)
        if not passed:
            record({"target": target, "staged": False, "stage_output": out})
            Alerts().fire("deploy", "Deploy refused: tests failed", f"The test suite failed on {target[:8]} before "
                          "the switch; the live checkout is unchanged.", 4)
            print(f"Refusing: the test suite failed on {target[:12]}; nothing changed\n{out}")
            return 1
    with job_lock(DEPLOY_LOCK) as got:
        if not got:
            print("Refusing: another deploy is running")
            return 2
        if _refuse_if_closed():                    # staging took a while: a daily job may have started
            return 2
        with quiesced() as busy:
            if busy:
                print(f"Refusing: still running after {QUIESCE_WAIT_S:.0f}s: {', '.join(busy)}")
                return 2
            code, rec = _switch(target, stage, green, ci_override, agreed)
    # Every lock is released: jobs start again. The ML environment has its own lock (see `sync_ml`).
    if code == 0:
        _note_ml(rec)
    return code


def _switch(target: str, staged: bool, green: ci.Verdict, ci_override: str,
            agreed: contract.Agreed) -> tuple[int, Path | None]:
    before = _run("git", "rev-parse", "HEAD").stdout.strip()
    tag = next_tag()
    _run("git", *TAGGER, "tag", "-a", tag, "-m", f"runtime state before deploy at {_now():%Y-%m-%dT%H:%M:%SZ}", before)
    _run("git", "merge", "--ff-only", "--quiet", target)             # exactly the checked commit: no second fetch
    after = _run("git", "rev-parse", "HEAD").stdout.strip()
    sync_venv()
    ok, out = smoke()
    body: dict[str, object] = {"from": before, "to": after, "rollback_tag": tag, "lock": preflight.lock_hash(ROOT),
                               "smoke_ok": ok, "staged": staged,
                               "ci": green.checks if green.green else {"override": ci_override, "why": green.detail},
                               "contract": {"from": agreed.live, "to": agreed.target,
                                            "acknowledged": agreed.acknowledged}}
    if not ok:
        print(f"Smoke test FAILED on {after[:8]}; rolling back to {tag} (inside the deploy lock)\n{out}")
        _run("git", "reset", "--hard", "--quiet", tag)
        sync_venv()
        body["rolled_back"] = True
        record(body)
        Alerts().fire("deploy", "Deploy rolled back", f"Smoke test failed on {after[:8]}; live checkout restored "
                      f"to {tag}.", 4)
        return 1, None
    rec = record(body)
    Alerts().resolve("deploy", "Deploy healthy", f"Live checkout at {after[:8]}.")
    shown = rec.relative_to(ROOT) if rec.is_relative_to(ROOT) else rec          # WT_STATE may live elsewhere
    print(f"Deployed {before[:8]} -> {after[:8]} (rollback tag {tag}); record {shown}")
    return 0, rec


def rollback(tag: str) -> int:
    if not tag.startswith("runtime-"):
        print("Rollback targets are runtime-* tags only")
        return 2
    if _refuse_if_closed():
        return 2
    with job_lock(DEPLOY_LOCK) as got:
        if not got:
            print("Refusing: a deploy is running")
            return 2
        with quiesced() as busy:
            if busy:
                print(f"Refusing: still running after {QUIESCE_WAIT_S:.0f}s: {', '.join(busy)}")
                return 2
            before = _run("git", "rev-parse", "HEAD").stdout.strip()
            _run("git", "reset", "--hard", "--quiet", tag)
            sync_venv()
            ok, out = smoke()
    # The ML environment follows the checkout back, so the scorer never runs against a lock from the future.
    _note_ml(record({"rollback_from": before, "to_tag": tag, "smoke_ok": ok}))
    print(f"Rolled back {before[:8]} -> {tag}; smoke {'ok' if ok else 'FAILED'}")
    if not ok:
        print(out)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.ops.deploy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gate")
    dp = sub.add_parser("deploy")
    dp.add_argument("--sha", help="the commit to deploy (default: the tip of main)")
    dp.add_argument("--stage", action="store_true", help="run the full test suite on the target before switching")
    sub.add_parser("sync-ml")
    r = sub.add_parser("rollback")
    r.add_argument("tag")
    a = ap.parse_args(argv)
    if a.cmd == "gate":
        b = gate_blockers()
        print("Deploy gate OPEN" if not b else "Deploy gate CLOSED:\n  " + "\n  ".join(b))
        return 0 if not b else 2
    if a.cmd == "sync-ml":
        state = sync_ml()
        print(f"ML environment: {state}")
        if state not in ("none", "current", "synced"):
            return 1
        if state == "none":
            return 0
        # In sync with its lockfile is not the same as working: say so if it cannot import what it is for.
        got = _run(str(ROOT / ML_VENV / "bin" / "python"), "-c", ML_IMPORT_CHECK, check=False, timeout=120)
        if got.returncode != 0:
            print("ML environment: installed, but it cannot import its libraries:\n  "
                  + (got.stderr or got.stdout).strip().splitlines()[-1][:200]
                  + "\n  (LightGBM needs the system's OpenMP library: sudo apt-get install -y libgomp1)")
            return 1
        return 0
    if a.cmd == "rollback":
        return rollback(a.tag)
    return deploy(a.sha, a.stage)


if __name__ == "__main__":
    sys.exit(main())
