"""The deploy gate of an always-open desk (CRY-DEC-0001), and the deploy as one transaction on one exact commit.

No market calendar, no hour of the day, no check of a dashboard that is not in this repository. Real git
repositories in a temp dir; CI, the venv and the smoke test are stubbed.

Origin: trading-lab-research `tests/unit/test_deploy_transaction.py` at 217a4eb (trimmed and rewritten). Removed:
the dashboard-guard, state-migration and trading-window cases. The transaction tests keep their names and bodies;
the gate and contract tests are new."""
from __future__ import annotations

import ast
import datetime as dt
import subprocess
from pathlib import Path

import pytest

from wt.ops import ci, contract, deploy, locks
from wt.ops.schedule import JOBS

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT / "src" / "wt" / "ops"


def imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    return out


# ---------------------------------------------------------------- the gate

def test_the_gate_knows_no_market_calendar_and_no_dashboard_check():
    """There is nothing to consult: the modules do not exist here, and nothing the deploy or the job runner imports
    reads a stock-exchange session."""
    for gone in ("window.py", "dashguard.py", "agents.py", "migrate.py", "bootreconcile.py"):
        assert not (OPS / gone).exists(), gone
    for module in ("deploy.py", "jobs.py", "units.py", "preflight.py", "backup.py"):
        found = imports(OPS / module)
        assert not [m for m in found if m.split(".")[-1] in ("window", "dashguard") or m.startswith(("wt.data", "wt.brokers", "alpaca"))], module
    text = (OPS / "deploy.py").read_text() + (OPS / "jobs.py").read_text()
    code = "\n".join(line.split("#")[0] for line in text.splitlines())
    for word in ("load_sessions", "calendar(", "07:00", "PRE_SESSION", "is_open"):
        assert word not in code, word
    assert "now" not in deploy.gate_blockers.__code__.co_varnames         # the gate does not ask what time it is


@pytest.mark.parametrize("when", [dt.datetime(2026, 10, 12, 11, 30, tzinfo=dt.UTC),      # Monday 07:30 New York
                                  dt.datetime(2026, 10, 12, 14, 0, tzinfo=dt.UTC),       # Monday, the US session
                                  dt.datetime(2026, 10, 17, 3, 0, tzinfo=dt.UTC),        # a Saturday
                                  dt.datetime(2026, 12, 25, 15, 0, tzinfo=dt.UTC)])      # a holiday
def test_the_gate_is_open_at_any_hour_when_no_daily_job_runs(tmp_path, monkeypatch, when):
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(deploy, "_now", lambda: when)
    assert deploy.gate_blockers() == []


def test_the_gate_is_closed_only_while_a_daily_job_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    assert deploy.long_jobs() == ["crypto-challengers", "crypto-learn"]
    with locks.job_lock("crypto-learn"):
        assert deploy.gate_blockers() == ["crypto-learn holds its lock (a run is in progress)"]
    with locks.job_lock("crypto"), locks.job_lock("dashboard-crypto"):
        assert deploy.gate_blockers() == []                     # a 15-minute job in flight is waited for, not refused
        with deploy.quiesced(wait_s=0) as busy:
            assert busy == ["crypto", "dashboard-crypto"]
    monkeypatch.setattr(deploy, "running_jobs", lambda: {"cryptolab-crypto-challengers": True, "cryptolab-crypto-learn": False})
    assert deploy.gate_blockers() == ["cryptolab-crypto-challengers is running"]
    monkeypatch.setattr(deploy, "running_jobs", lambda: {"systemctl": True})          # the service manager can't be asked
    assert deploy.gate_blockers() == ["systemctl is running"]                         # fail closed


def test_required_checks_are_test_and_ml_and_the_ci_workflow_names_its_jobs_exactly_so():
    import yaml
    assert ci.DEFAULT_REQUIRED == ("test", "ml") and ci.required_names() == ("test", "ml")
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert list(wf["jobs"]) == ["test", "ml"] and [j["name"] for j in wf["jobs"].values()] == ["test", "ml"]
    assert ci.verdict([{"id": 1, "name": "test", "status": "completed", "conclusion": "success"}], ("test", "ml")).green is False
    both = [{"id": i, "name": n, "status": "completed", "conclusion": "success"} for i, n in enumerate(("test", "ml"))]
    assert ci.verdict(both, ci.DEFAULT_REQUIRED).green is True


def test_smoke_tests_exist_and_are_marked_so_the_deploys_smoke_step_has_something_to_pass():
    r = subprocess.run([str(ROOT / ".venv/bin/python") if (ROOT / ".venv/bin/python").exists() else "python3", "-m", "pytest",
                        "-m", "smoke", "--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}, timeout=300)
    picked = [line for line in r.stdout.splitlines() if "::" in line]
    assert r.returncode == 0 and len(picked) >= 10, r.stdout[-400:]
    assert all("test_smoke.py" in line for line in picked)
    assert 'markers = [\n    "smoke:' in (ROOT / "pyproject.toml").read_text()
    assert '"-m", "smoke"' in (OPS / "deploy.py").read_text()


# ---------------------------------------------------------------- the transaction

def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo: Path, name: str, text: str | None = None) -> str:
    f = repo / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(name if text is None else text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    origin, dev, live = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "live"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True, capture_output=True)
    git(dev, "checkout", "-q", "-b", "main")
    first = commit(dev, contract.SCHEMA, '{"v": 1}\n')
    git(dev, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(live)], check=True, capture_output=True)
    monkeypatch.setattr(deploy, "ROOT", live)
    monkeypatch.setattr(deploy, "DEPLOY_DIR", tmp_path / "records")
    monkeypatch.setattr(deploy, "STAGE_DIR", tmp_path / "stage")
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.delenv(contract.ACK_ENV, raising=False)
    state = {"gate": [[]], "green": True, "smoke": True, "stage": (True, "ok"), "synced": 0, "alerts": []}

    def gate():
        return state["gate"].pop(0) if len(state["gate"]) > 1 else state["gate"][0]
    monkeypatch.setattr(deploy, "gate_blockers", gate)
    monkeypatch.setattr(deploy.preflight, "check_git", lambda root: [])
    monkeypatch.setattr(deploy.preflight, "lock_hash", lambda root: "h")
    monkeypatch.setattr(deploy.ci, "check", lambda sha: ci.Verdict(state["green"], "stub", {"test": "success", "ml": "success"}))
    monkeypatch.setattr(deploy, "sync_venv", lambda: state.__setitem__("synced", state["synced"] + 1))
    monkeypatch.setattr(deploy, "smoke", lambda: (state["smoke"], "smoke output"))
    monkeypatch.setattr(deploy, "stage_tests", lambda target: state["stage"])
    monkeypatch.setattr(deploy, "sync_ml", lambda: "none")

    class A:
        def fire(self, key, title, msg, prio=4):
            state["alerts"].append(title)

        def resolve(self, *a, **k):
            pass
    monkeypatch.setattr(deploy, "Alerts", A)
    return dev, live, first, state


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def test_deploys_exactly_the_checked_commit_not_the_newer_tip(world):
    dev, live, first, st = world
    second = commit(dev, "b")
    commit(dev, "c")                                            # main moves on after the check
    git(dev, "push", "-q", "origin", "main")
    assert deploy.deploy(second) == 0
    assert head(live) == second
    assert any(t.startswith("runtime-") for t in git(live, "tag").split())


def test_refuses_a_red_commit_a_branch_commit_and_going_backwards(world, capsys):
    dev, live, first, st = world
    second = commit(dev, "b")
    git(dev, "push", "-q", "origin", "main")
    st["green"] = False
    assert deploy.deploy(second) == 2 and head(live) == first
    st["green"] = True
    git(dev, "checkout", "-q", "-b", "side")
    side = commit(dev, "s")
    git(dev, "push", "-q", "origin", "side")
    git(live, "fetch", "-q", "origin", "side")
    assert deploy.deploy(side) == 2 and head(live) == first
    assert "not on main" in capsys.readouterr().out
    assert deploy.deploy(second) == 0 and head(live) == second
    assert deploy.deploy(first) == 2 and head(live) == second      # backwards is rollback's job
    assert deploy.deploy(second) == 0                               # already there: nothing to do


def test_a_red_staged_suite_changes_nothing(world):
    dev, live, first, st = world
    second = commit(dev, "b")
    git(dev, "push", "-q", "origin", "main")
    st["stage"] = (False, "1 failed")
    assert deploy.deploy(second, stage=True) == 1
    assert head(live) == first and st["synced"] == 0
    assert "Deploy refused: tests failed" in st["alerts"]


def test_a_failed_smoke_test_rolls_back_even_when_the_gate_has_closed(world):
    dev, live, first, st = world
    second = commit(dev, "b")
    git(dev, "push", "-q", "origin", "main")
    st["smoke"] = False
    st["gate"] = [[], [], ["crypto-learn is running"]]             # open, open (under the lock), then closed
    assert deploy.deploy(second) == 1
    assert head(live) == first and st["synced"] == 2              # synced forward, then back
    assert "Deploy rolled back" in st["alerts"]


def test_the_gate_is_asked_again_under_the_lock(world):
    dev, live, first, st = world
    second = commit(dev, "b")
    git(dev, "push", "-q", "origin", "main")
    st["gate"] = [[], ["crypto-challengers is running"]]          # a daily job started while staging ran
    assert deploy.deploy(second, stage=True) == 2 and head(live) == first


def test_a_cycle_in_flight_is_waited_for_and_one_that_never_ends_refuses_the_deploy(world, monkeypatch, capsys):
    dev, live, first, st = world
    second = commit(dev, "b")
    git(dev, "push", "-q", "origin", "main")
    monkeypatch.setattr(deploy, "QUIESCE_WAIT_S", 0.0)
    with locks.job_lock("crypto"):
        assert deploy.deploy(second) == 2 and head(live) == first
    assert "still running after 0s: crypto" in capsys.readouterr().out
    assert deploy.deploy(second) == 0 and head(live) == second
    assert not locks.held(list(JOBS)) and not locks.is_held(locks.DEPLOY_LOCK)     # every lock is given back


def test_a_deploy_that_changes_the_snapshot_contract_needs_the_new_hash_from_the_owner(world, monkeypatch, capsys):
    dev, live, first, st = world
    second = commit(dev, contract.SCHEMA, '{"v": 2}\n')
    git(dev, "push", "-q", "origin", "main")
    assert deploy.deploy(second) == 2 and head(live) == first
    said = capsys.readouterr().out
    assert "the snapshot contract changes with this deploy" in said and contract.ACK_ENV in said
    new = contract.sha256(b'{"v": 2}\n')
    monkeypatch.setenv(contract.ACK_ENV, "0" * 12)                 # a hash, but not this schema's
    assert deploy.deploy(second) == 2 and head(live) == first
    monkeypatch.setenv(contract.ACK_ENV, new[:11])                 # too short to mean it
    assert deploy.deploy(second) == 2 and head(live) == first
    monkeypatch.setenv(contract.ACK_ENV, new[:12])
    assert deploy.deploy(second) == 0 and head(live) == second
    import json
    rec = json.loads(sorted((deploy.DEPLOY_DIR).glob("*.json"))[-1].read_text())
    assert rec["contract"] == {"from": contract.sha256(b'{"v": 1}\n'), "to": new, "acknowledged": True}
    monkeypatch.delenv(contract.ACK_ENV)
    third = commit(dev, "c")                                       # the contract is the live one again: no word needed
    git(dev, "push", "-q", "origin", "main")
    assert deploy.deploy(third) == 0 and head(live) == third


def test_a_target_without_a_contract_is_refused(world):
    dev, live, first, st = world
    git(dev, "rm", "-q", contract.SCHEMA)
    git(dev, "commit", "-q", "-m", "no contract")
    git(dev, "push", "-q", "origin", "main")
    assert deploy.deploy(head(dev)) == 2 and head(live) == first


def test_rollback_goes_back_to_a_runtime_tag_under_the_same_locks(world):
    dev, live, first, st = world
    second = commit(dev, "b")
    git(dev, "push", "-q", "origin", "main")
    assert deploy.deploy(second) == 0
    tag = [t for t in git(live, "tag").split() if t.startswith("runtime-")][0]
    assert deploy.rollback("v1") == 2 and head(live) == second                  # only runtime-* tags
    st["gate"] = [["crypto-learn is running"]]
    assert deploy.rollback(tag) == 2 and head(live) == second                   # the same gate
    st["gate"] = [[]]
    assert deploy.rollback(tag) == 0 and head(live) == first
