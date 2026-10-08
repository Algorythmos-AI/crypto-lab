"""The scorer: one process, one request, one answer.

    echo '{"rows": [{"atr_pct": 2.1, ...}]}' | .venv-ml/bin/python -m wt.ml.score --models var/crypto/models

Reads the request on stdin and writes `{"version", "kind", "scores", "cutoff", "half_below"}` on stdout. A score
is the model's probability that the signal ends with a positive result after costs. An input the request does
not carry is "missing", which each model handles its own way; an input the model does not know is ignored.

Exit codes: 0 answered; 2 bad request; 3 no model registered; 4 the model file is damaged or its pointer is
unreadable; 5 the model is too old. The caller (`wt.crypto.scorer`) treats every non-zero code, and silence, as
"trade the signal as its rule says".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

from wt.crypto import signals
from wt.ml.modelfile import ModelError, Pointer, read_model, read_pointer

EXIT = {"no_model": 3, "bad_pointer": 4, "bad_model": 4, "stale": 5, "features": 6}


def _value(row: dict[str, Any], name: str) -> float:
    v = row.get(name)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else math.nan


def _platt(p: float, a: float, b: float) -> float:
    """The model's calibration (DEC-0017): sigmoid(a * logit(p) + b). (1, 0) leaves the score as it is."""
    p = min(1 - 1e-6, max(1e-6, p))
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, a * math.log(p / (1 - p)) + b))))


def logistic(body: bytes, p: Pointer, rows: list[dict[str, Any]]) -> list[float]:
    """Standardised inputs times weights, through the logistic function. A missing input takes its training mean,
    which is zero after standardising: it neither helps nor hurts."""
    m = json.loads(body)
    out = []
    for row in rows:
        z = float(m["intercept"])
        for name, mean, scale, w in zip(p.inputs, m["mean"], m["scale"], m["coef"], strict=True):
            x = _value(row, name)
            if not math.isnan(x) and scale:
                z += (x - mean) / scale * w
        out.append(1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z)))))
    return out


def boosted(body: bytes, p: Pointer, rows: list[dict[str, Any]]) -> list[float]:
    import lightgbm as lgb
    import numpy as np
    booster = lgb.Booster(model_str=body.decode())
    x = np.array([[_value(row, name) for name in p.inputs] for row in rows], dtype=float)
    return [float(v) for v in booster.predict(x)]


def score(models: Path, rows: list[dict[str, Any]], now: dt.datetime, max_age_days: float) -> dict[str, Any]:
    p = read_pointer(models)
    if p.features and p.features != signals.features_id():
        # Trained on inputs that meant something else. A score from it would be a number about another question.
        raise ModelError("features")
    body = read_model(p, now, max_age_days)
    a, b = p.calibration
    scores = [_platt(s, a, b) for s in (logistic if p.kind == "logistic" else boosted)(body, p, rows)]
    if len(scores) != len(rows) or not all(0.0 <= s <= 1.0 for s in scores):
        raise ModelError("bad_model")
    return {"version": p.version, "kind": p.kind, "scores": [round(s, 6) for s in scores], "cutoff": p.cutoff,
            "half_below": p.half_below}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.ml.score")
    ap.add_argument("--models", required=True)
    ap.add_argument("--max-age-days", type=float, default=14.0)
    a = ap.parse_args(argv)
    try:
        req = json.loads(sys.stdin.read())
        rows = req["rows"]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("rows")
    except (ValueError, KeyError, TypeError):
        print("bad request", file=sys.stderr)
        return 2
    try:
        print(json.dumps(score(Path(a.models), rows, dt.datetime.now(dt.UTC), a.max_age_days)))
        return 0
    except ModelError as e:
        print(str(e.args[0]), file=sys.stderr)
        return EXIT.get(str(e.args[0]), 4)
    except Exception as e:  # noqa: BLE001 — a damaged model file: say so, never a traceback the caller must parse
        print(f"bad_model ({e.__class__.__name__})", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
