"""The trading side of the scorer (DEC-0016, 4): ask the ML process for scores, and never depend on the answer.

The cycle imports no ML library. It starts `python -m wt.ml.score` in the ML environment with a time limit and
reads one line back. Anything else that happens (no ML environment, no model, a damaged or stale model, a
timeout, a crash, a wrong-shaped answer) returns a reason instead of scores, and the caller trades the signal
exactly as its registered rule says. Standard library only.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wt.core.config import ROOT
from wt.core.desk import Desk

ML_PYTHON = ".venv-ml/bin/python"
TIMEOUT_S = 20.0
MAX_AGE_DAYS = 14.0
REASONS = {3: "no_model", 4: "bad_model", 5: "stale", 2: "bad_request"}
QUIET = ("no_environment", "no_model")      # nothing is wrong: learning has not got this far on this host yet


@dataclass(frozen=True)
class Scored:
    version: str
    scores: tuple[float, ...]
    cutoff: float
    half_below: float

    def factor(self, i: int) -> float:
        """What a promoted model does with signal i: 0 skip, 0.5 half size, 1 as the rule says. Never more."""
        s = self.scores[i]
        return 0.0 if s < self.cutoff else 0.5 if s < self.half_below else 1.0


def models_dir(desk: Desk) -> Path:
    return desk.state_dir / "models"


def score(rows: list[dict[str, Any]], desk: Desk, timeout_s: float = TIMEOUT_S, max_age_days: float = MAX_AGE_DAYS,
          python: str | Path | None = None, root: Path = ROOT) -> tuple[Scored | None, str | None]:
    """(scores, None) or (None, why not). Never raises."""
    exe = Path(python) if python is not None else root / ML_PYTHON
    models = models_dir(desk)
    if not rows:
        return None, "no_rows"
    if not exe.exists():
        return None, "no_environment"
    if not (models / "current.json").exists():
        return None, "no_model"
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
    env.update(PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1")
    try:
        r = subprocess.run([str(exe), "-m", "wt.ml.score", "--models", str(models), "--max-age-days", str(max_age_days)],
                           input=json.dumps({"rows": rows}), capture_output=True, text=True, timeout=timeout_s,
                           env=env, cwd=root)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "failed"
    if r.returncode != 0:
        return None, REASONS.get(r.returncode, "failed")
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
        scores = tuple(float(x) for x in d["scores"])
        if len(scores) != len(rows) or not all(0.0 <= s <= 1.0 for s in scores):
            raise ValueError("scores")
        cutoff, half = float(d["cutoff"]), float(d["half_below"])
        return Scored(str(d["version"]), scores, cutoff, max(cutoff, half)), None
    except (ValueError, KeyError, TypeError, IndexError):
        return None, "bad_answer"

