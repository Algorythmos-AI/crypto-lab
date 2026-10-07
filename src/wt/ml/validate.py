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

Predict = Callable[[np.ndarray], np.ndarray]
Fit = Callable[[np.ndarray, np.ndarray, np.ndarray], Predict]
INNER_FOLDS, MIN_CALIBRATION = 3, 100                    # DEC-0017


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return np.asarray(np.log(p / (1 - p)))


def platt(p: np.ndarray, a: float, b: float) -> np.ndarray:
    return np.asarray(1.0 / (1.0 + np.exp(-np.clip(a * _logit(p) + b, -60, 60))))


def calibration(examples: list[Example], x: np.ndarray, w: np.ndarray, fit: Fit, embargo_s: int) -> tuple[float, float]:
    """Platt scaling for one model on one training set (DEC-0017): (a, b) fitted on predictions the model made
    for signals it was not trained on, from inner walk-forward folds purged like the outer ones. (1, 0) when
    there are too few such predictions to fit two numbers on."""
    from sklearn.linear_model import LogisticRegression
    y = np.array([e.y for e in examples], dtype=float)
    pred = np.full(len(examples), np.nan)
    for train, test in folds(examples, INNER_FOLDS, embargo_s):
        pred[test] = fit(x[train], y[train], w[train])(x[test])
    ok = np.isfinite(pred)
    if ok.sum() < MIN_CALIBRATION or len(set(y[ok])) < 2:
        return 1.0, 0.0
    clf = LogisticRegression(C=1e6, max_iter=1000).fit(_logit(pred[ok]).reshape(-1, 1), y[ok], sample_weight=w[ok])
    return float(clf.coef_[0][0]), float(clf.intercept_[0])


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
                 cutoff_pct: float, calibrate: bool = True) -> dict[str, object]:
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
            a, b = calibration([examples[i] for i in train], x[train], w[train], fit, embargo_s) if calibrate else (1.0, 0.0)
            p_test, cut = platt(model(x[test]), a, b), float(np.percentile(platt(model(x[train]), a, b), cutoff_pct))
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
