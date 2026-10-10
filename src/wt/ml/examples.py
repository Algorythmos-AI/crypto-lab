"""One training example and what is worked out from a list of them: weights, the matrix, the set's hash.

These know nothing of any desk. An example is a signal with its inputs and its outcome; where the signal came
from (`wt.ml.dataset` builds the crypto desk's) is the caller's business. `wt.ml.dataset` re-exports every name
here, so existing imports are unchanged.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Example:
    sid: str
    sleeve: str
    pair: str
    t: int                      # when the signal bar closed: the moment the signal existed
    exit_t: int                 # when its outcome was known
    inputs: dict[str, float | None]
    r: float
    reason: str

    @property
    def y(self) -> int:
        return int(self.r > 0)


def uniqueness(examples: list[Example], step_s: int) -> np.ndarray:
    """A weight per example: how much of its holding period it has to itself (Lopez de Prado's average
    uniqueness). Trades open at the same time share one stretch of market, so each counts for less."""
    if not examples:
        return np.zeros(0)
    t0 = min(e.t for e in examples)
    span = []
    for e in examples:
        a = (e.t - t0) // step_s
        # Up to the step its outcome falls in, and never empty: a trade stopped out inside its first bar still
        # shared that bar with every other trade open then. (An empty span once gave such a trade full weight,
        # twenty times a normal one.)
        span.append((a, max(a + 1, -(-(e.exit_t - t0) // step_s))))
    count = np.zeros(max(b for _, b in span) + 1)
    for a, b in span:
        count[a:b] += 1
    w = np.array([float(np.mean(1.0 / count[a:b])) for a, b in span])
    return w / w.mean()


def effective_n(w: np.ndarray) -> float:
    """How many equally weighted examples the weights are worth (Kish)."""
    return float(w.sum() ** 2 / (w ** 2).sum()) if len(w) and (w ** 2).sum() > 0 else 0.0


def matrix(examples: list[Example], names: list[str]) -> np.ndarray:
    """Inputs as numbers, NaN where an input is missing. The sleeve is three 0/1 columns named `is_<sleeve>`."""
    rows = []
    for e in examples:
        rows.append([float(e.sleeve == n[3:]) if n.startswith("is_") else
                     (float("nan") if e.inputs.get(n) is None else float(e.inputs[n])) for n in names])    # type: ignore[arg-type]
    return np.array(rows, dtype=float).reshape(len(examples), len(names))


def data_hash(examples: list[Example]) -> str:
    h = hashlib.sha256()
    for e in examples:
        h.update(f"{e.sid},{e.t},{e.exit_t},{e.r},{sorted(e.inputs.items())}\n".encode())
    return h.hexdigest()[:16]
