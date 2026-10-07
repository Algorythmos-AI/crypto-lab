"""The ML platform's trading side (DEC-0016, 4): the cycle asks a separate process for scores and never depends on
the answer. These run in the trading environment, which has no ML library: that is the point."""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import hashlib
import json
import stat
import sys
from pathlib import Path

import pytest

from wt.core import desk as desks
from wt.crypto import scorer
from wt.ml import modelfile
from wt.ops import deploy

ROOT = Path(__file__).resolve().parents[2]
ML_LIBRARIES = {"sklearn", "lightgbm", "torch", "transformers", "chronos", "joblib"}
NOW = dt.datetime(2026, 10, 7, 12, tzinfo=dt.UTC)
LOGISTIC = {"intercept": 0.0, "mean": [2.0, 50.0], "scale": [1.0, 10.0], "coef": [1.0, -0.5]}


def imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0] if not node.module.startswith("wt.") else node.module)
    return out


def test_no_trading_module_imports_an_ml_library_or_the_ml_package():
    for path in sorted((ROOT / "src" / "wt").rglob("*.py")):
        rel = path.relative_to(ROOT / "src" / "wt").as_posix()
        if rel.startswith("ml/"):
            continue
        found = imports(path)
        assert not found & ML_LIBRARIES, f"{rel} imports {found & ML_LIBRARIES}"
        assert not {m for m in found if m.startswith("wt.ml")}, f"{rel} imports the ML package"
    assert "lightgbm" not in (ROOT / "requirements.lock.txt").read_text()            # the trading lock is untouched
    assert "lightgbm==" in (ROOT / "requirements-ml.lock.txt").read_text()
    assert "--hash=sha256:" in (ROOT / "requirements-ml.lock.txt").read_text()


def test_the_model_file_helpers_need_only_the_standard_library():
    assert imports(ROOT / "src/wt/ml/modelfile.py") <= {"__future__", "datetime", "hashlib", "json", "os", "uuid",
                                                         "dataclasses", "pathlib", "typing"}


@pytest.fixture
def desk(tmp_path):
    return dataclasses.replace(desks.DESKS["crypto"], state_dir=tmp_path / "crypto")


def register(desk, trained=NOW, body=None):
    body = json.dumps(LOGISTIC).encode() if body is None else body
    return modelfile.register(scorer.models_dir(desk), "m1-test", "logistic", body, ["atr_pct", "rsi"],
                              trained.isoformat(), 0.4, 0.6, {"note": "test"})


def fake_python(tmp_path: Path, body: str) -> Path:
    """An "interpreter" that ignores its arguments and runs `body`: what a scorer that misbehaves looks like."""
    exe = tmp_path / "fake-python"
    exe.write_text(f"#!{sys.executable}\nimport sys\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_a_registered_model_is_read_back_only_when_its_hash_and_age_are_right(desk):
    p = register(desk)
    assert (p.version, p.kind, p.inputs, p.cutoff, p.half_below) == ("m1-test", "logistic", ("atr_pct", "rsi"), 0.4, 0.6)
    assert json.loads(modelfile.read_model(p, NOW, 14)) == LOGISTIC
    assert json.loads((scorer.models_dir(desk) / "m1-test" / "card.json").read_text()) == {"note": "test"}
    with pytest.raises(modelfile.ModelError, match="stale"):
        modelfile.read_model(p, NOW + dt.timedelta(days=15), 14)
    p.path.write_bytes(b"tampered")
    with pytest.raises(modelfile.ModelError, match="bad_model"):
        modelfile.read_model(p, NOW, 14)
    with pytest.raises(modelfile.ModelError, match="no_model"):
        modelfile.read_pointer(desk.state_dir / "nowhere")
    (scorer.models_dir(desk) / "current.json").write_text('{"version": "../../etc", "kind": "logistic"}')
    with pytest.raises(modelfile.ModelError, match="bad_pointer"):
        modelfile.read_pointer(scorer.models_dir(desk))


def test_the_real_scorer_answers_with_the_standard_library_alone_for_a_logistic_model(desk):
    register(desk, trained=dt.datetime.now(dt.UTC))
    got, why = scorer.score([{"atr_pct": 3.0, "rsi": 50.0}, {"atr_pct": 2.0}, {}], desk, python=sys.executable, root=ROOT)
    assert why is None and got.version == "m1-test" and got.scores == (pytest.approx(0.731059, abs=1e-6), 0.5, 0.5)
    assert [got.factor(i) for i in range(3)] == [1.0, 0.5, 0.5] and (got.cutoff, got.half_below) == (0.4, 0.6)


@pytest.mark.parametrize("body,why", [
    ("import time; time.sleep(30)", "timeout"),
    ("sys.exit(1)", "failed"),
    ("sys.exit(3)", "no_model"),
    ("sys.exit(4)", "bad_model"),
    ("sys.exit(5)", "stale"),
    ("print('not json')", "bad_answer"),
    ("print('{\"version\": \"x\", \"scores\": [0.5], \"cutoff\": 0.4, \"half_below\": 0.6}')", "bad_answer"),   # one score, two rows
    ("print('{\"version\": \"x\", \"scores\": [0.5, 7.0], \"cutoff\": 0.4, \"half_below\": 0.6}')", "bad_answer"),
])
def test_every_way_the_scorer_can_fail_ends_in_a_reason_and_no_scores(desk, tmp_path, body, why):
    register(desk)
    got = scorer.score([{"atr_pct": 1.0}, {"atr_pct": 2.0}], desk, timeout_s=1.0, python=fake_python(tmp_path, body), root=ROOT)
    assert got == (None, why)


def test_a_stale_or_damaged_model_and_a_missing_environment_are_reasons_too(desk, tmp_path):
    assert scorer.score([{"a": 1}], desk, python=tmp_path / "absent", root=ROOT) == (None, "no_environment")
    assert scorer.score([{"a": 1}], desk, python=sys.executable, root=ROOT) == (None, "no_model")
    assert scorer.score([], desk, python=sys.executable, root=ROOT) == (None, "no_rows")
    register(desk, trained=dt.datetime.now(dt.UTC) - dt.timedelta(days=15))
    assert scorer.score([{"a": 1}], desk, python=sys.executable, root=ROOT) == (None, "stale")
    p = register(desk, trained=dt.datetime.now(dt.UTC))
    p.path.write_bytes(b"{}")
    assert scorer.score([{"a": 1}], desk, python=sys.executable, root=ROOT) == (None, "bad_model")
    assert scorer.QUIET == ("no_environment", "no_model")


def test_a_promoted_model_can_only_skip_or_halve():
    s = scorer.Scored("v", (0.0, 0.39, 0.4, 0.59, 0.6, 1.0), 0.4, 0.6)
    assert [s.factor(i) for i in range(6)] == [0.0, 0.0, 0.5, 0.5, 1.0, 1.0]
    assert max(s.factor(i) for i in range(6)) <= 1.0


def test_the_ml_environment_syncs_only_when_its_lock_changes_and_never_fails_a_deploy(tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(deploy, "ROOT", tmp_path)
    monkeypatch.setattr(deploy, "Alerts", lambda: type("A", (), {"fire": lambda *a, **k: calls.append(("fire", a[1])),
                                                                "resolve": lambda *a, **k: None})())

    def run(*cmd, check=True, timeout=600):
        calls.append(cmd[1:3])
        if cmd[1] == "venv":
            (tmp_path / ".venv-ml" / "bin").mkdir(parents=True)
            (tmp_path / ".venv-ml" / "bin" / "python").write_text("")
    monkeypatch.setattr(deploy, "_run", run)
    assert deploy.sync_ml() == "none" and calls == []                       # a checkout from before the lock existed
    (tmp_path / "requirements-ml.lock.txt").write_text("lightgbm==4.7.0 --hash=sha256:aa\n")
    assert deploy.sync_ml() == "synced" and calls == [("venv", "--quiet"), ("pip", "sync")]
    want = hashlib.sha256((tmp_path / "requirements-ml.lock.txt").read_bytes()).hexdigest()
    assert (tmp_path / ".venv-ml" / ".lock-sha256").read_text().strip() == want
    calls.clear()
    assert deploy.sync_ml() == "current" and calls == []                    # unchanged lock: nothing is rebuilt
    (tmp_path / "requirements-ml.lock.txt").write_text("lightgbm==4.8.0 --hash=sha256:bb\n")
    monkeypatch.setattr(deploy, "_run", lambda *c, **k: (_ for _ in ()).throw(RuntimeError("no network")))
    assert deploy.sync_ml() == "failed" and calls == [("fire", "deploy-ml")]


def test_the_ml_environment_is_ignored_by_git_and_has_its_own_ci_job():
    import yaml
    assert ".venv-ml/" in (ROOT / ".gitignore").read_text().splitlines()
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = yaml.safe_dump(wf["jobs"]["ml"])
    assert "requirements-ml.lock.txt" in steps and "--require-hashes" in steps and "tests/ml" in steps
    assert "requirements-ml" not in yaml.safe_dump(wf["jobs"]["test"])       # the trading suite never installs it
