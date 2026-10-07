"""Purged walk-forward validation (DEC-0016, 3): a model is never scored on a signal whose outcome overlaps what it
was trained on.

Five expanding folds over time. For each, the training examples are those whose outcome was known before the
test block starts, less an embargo of the longest holding period: an example still open when the test block
begins, or opened within the embargo before it, shares market with the test block and is dropped.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

import numpy as np

from wt.ml.dataset import Example

Fit = Callable[[np.ndarray, np.ndarray, np.ndarray], Callable[[np.ndarray], np.ndarray]]


def folds(examples: list[Example], n: int, embargo_s: int) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """(train indices, test indices) for each fold, oldest test block first. `examples` are in time order."""
    t = np.array([e.t for e in examples])
    done = np.array([e.exit_t for e in examples])
    edges = np.quantile(t, np.linspace(0, 1, n + 2))
    for k in range(1, n + 1):
        lo, hi = edges[k], edges[k + 1]
        test = np.where((t >= lo) & ((t < hi) if k < n else (t <= hi)))[0]
        train = np.where((done < lo) & (t < lo - embargo_s))[0]
        if len(test) and len(train):
            yield train, test


def log_loss(y: np.ndarray, p: np.ndarray, w: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-(w * (y * np.log(p) + (1 - y) * np.log(1 - p))).sum() / w.sum())


def walk_forward(examples: list[Example], x: np.ndarray, w: np.ndarray, fit: Fit | None, n: int, embargo_s: int,
                 cutoff_pct: float) -> dict[str, object]:
    """Out-of-sample scores of one model over the folds. `fit` is None for M0, the model that takes every signal
    and predicts the training win rate. Besides log-loss it reports the question that matters to the desk: the
    mean R of the signals the model would keep against the ones it would skip, at the cut-off DEC-0016 fixes."""
    y = np.array([e.y for e in examples], dtype=float)
    r = np.array([e.r for e in examples], dtype=float)
    losses, kept, dropped, pred = [], [], [], np.full(len(examples), np.nan)
    for train, test in folds(examples, n, embargo_s):
        if fit is None:
            base = float((w[train] * y[train]).sum() / w[train].sum())
            p_test, cut = np.full(len(test), base), -np.inf
        else:
            model = fit(x[train], y[train], w[train])
            p_test, cut = model(x[test]), float(np.percentile(model(x[train]), cutoff_pct))
        pred[test] = p_test
        losses.append(log_loss(y[test], p_test, w[test]))
        kept += list(r[test][p_test >= cut])
        dropped += list(r[test][p_test < cut])
    ok = np.isfinite(pred)
    return {"folds": len(losses), "log_loss": float(np.mean(losses)) if losses else None,
            "log_loss_se": float(np.std(losses, ddof=1) / np.sqrt(len(losses))) if len(losses) > 1 else None,
            "fold_log_loss": [round(v, 5) for v in losses], "scored": int(ok.sum()),
            "brier": float(np.mean((pred[ok] - y[ok]) ** 2)) if ok.any() else None,
            "kept": len(kept), "kept_mean_r": float(np.mean(kept)) if kept else None,
            "dropped": len(dropped), "dropped_mean_r": float(np.mean(dropped)) if dropped else None,
            "all_mean_r": float(r[ok].mean()) if ok.any() else None}
