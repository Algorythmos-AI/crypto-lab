"""A registered model on disk, and how it is read back.

    <models>/current.json            which version is in force
    <models>/<version>/model.json    logistic regression: plain numbers (inputs, means, scales, weights)
    <models>/<version>/model.txt     LightGBM: its own text format
    <models>/<version>/card.json     the model card

No pickle anywhere: a model file is numbers or LightGBM's text, and its SHA-256 is checked against the one
`current.json` names before it is used. Standard library only, so the trading side can read the pointer too.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KINDS = {"logistic": "model.json", "lightgbm": "model.txt"}


class ModelError(Exception):
    """args[0] is a code: no_model, bad_pointer, bad_model, stale."""


@dataclass(frozen=True)
class Pointer:
    version: str
    kind: str
    inputs: tuple[str, ...]
    sha256: str
    trained_at: str
    cutoff: float               # a signal scored below this is skipped once the model is promoted
    half_below: float           # and one below this is taken at half size
    path: Path
    calibration: tuple[float, float] = (1.0, 0.0)   # Platt scaling (DEC-0017): sigmoid(a * logit(p) + b)
    lineage: str = ""           # the model's kind and settings: weekly retrainings of one lineage share its checkpoints
    features: str = ""          # wt.crypto.signals.features_id() at training: the inputs' names and definitions

    def age_days(self, now: dt.datetime) -> float:
        return (now - dt.datetime.fromisoformat(self.trained_at)).total_seconds() / 86_400


def read_pointer(models: Path) -> Pointer:
    f = models / "current.json"
    if not f.exists():
        raise ModelError("no_model")
    try:
        d = json.loads(f.read_text())
        kind, version = str(d["kind"]), str(d["version"])
        if kind not in KINDS or not version or "/" in version or version.startswith("."):
            raise ValueError(kind)
        cal = d.get("calibration") or [1.0, 0.0]
        return Pointer(version, kind, tuple(str(x) for x in d["inputs"]), str(d["sha256"]), str(d["trained_at"]),
                       float(d["cutoff"]), float(d["half_below"]), models / version / KINDS[kind],
                       (float(cal[0]), float(cal[1])), str(d.get("lineage") or ""), str(d.get("features") or ""))
    except (ValueError, KeyError, TypeError) as e:
        raise ModelError("bad_pointer") from e


def read_model(p: Pointer, now: dt.datetime, max_age_days: float) -> bytes:
    """The model file's bytes, after its hash and its age have been checked."""
    try:
        body = p.path.read_bytes()
    except OSError as e:
        raise ModelError("bad_model") from e
    if hashlib.sha256(body).hexdigest() != p.sha256:
        raise ModelError("bad_model")
    try:
        if p.age_days(now) > max_age_days:
            raise ModelError("stale")
    except (ValueError, TypeError) as e:
        raise ModelError("bad_pointer") from e
    return body


def _atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def register(models: Path, version: str, kind: str, body: bytes, inputs: list[str], trained_at: str, cutoff: float,
             half_below: float, card: dict[str, Any], calibration: tuple[float, float] = (1.0, 0.0),
             lineage: str = "", features: str = "") -> Pointer:
    """Write a model and its card under its version, then point `current.json` at it. The pointer is replaced
    last and atomically, so a reader sees the old model or the new one, never half of either."""
    _atomic(models / version / KINDS[kind], body)
    _atomic(models / version / "card.json", json.dumps(card, indent=1, sort_keys=True).encode())
    pointer = {"version": version, "kind": kind, "inputs": inputs, "sha256": hashlib.sha256(body).hexdigest(),
               "trained_at": trained_at, "cutoff": cutoff, "half_below": half_below, "calibration": list(calibration), "lineage": lineage,
               "features": features}
    _atomic(models / "current.json", json.dumps(pointer, indent=1, sort_keys=True).encode())
    return read_pointer(models)
