"""The models DEC-0016 names, and nothing else: logistic regression (M1) and gradient-boosted trees (M2), each
with the few settings the charter allows. A model here is a `fit` that returns a `predict`, plus a way to write
itself down in the registry's formats (plain numbers, or LightGBM's text): no pickle.
"""
from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any

import lightgbm as lgb
import numpy as np
from sklearn.linear_model import LogisticRegression

SEED = 7
Predict = Callable[[np.ndarray], np.ndarray]


def grid(spec: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Every combination of the settings the charter lists as a list; the others are fixed."""
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*(spec[k] for k in keys))]


def _standardise(x: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = np.array([np.average(col[np.isfinite(col)], weights=w[np.isfinite(col)]) if np.isfinite(col).any() else 0.0
                     for col in x.T])
    filled = np.where(np.isfinite(x), x, mean)
    scale = filled.std(axis=0)
    return mean, np.where(scale > 0, scale, 1.0)


def fit_logistic(x: np.ndarray, y: np.ndarray, w: np.ndarray, C: float) -> tuple[Predict, bytes]:  # noqa: N803
    """Standardised inputs, a missing value at the training mean (zero after standardising): exactly what
    `wt.ml.score.logistic` does with the numbers written here."""
    mean, scale = _standardise(x, w)
    z = (np.where(np.isfinite(x), x, mean) - mean) / scale
    clf = LogisticRegression(C=C, max_iter=2000).fit(z, y, sample_weight=w)
    coef, intercept = clf.coef_[0], float(clf.intercept_[0])

    def predict(q: np.ndarray) -> np.ndarray:
        zq = (np.where(np.isfinite(q), q, mean) - mean) / scale
        return np.asarray(1.0 / (1.0 + np.exp(-np.clip(zq @ coef + intercept, -60, 60))))
    body = json.dumps({"intercept": intercept, "mean": mean.tolist(), "scale": scale.tolist(), "coef": coef.tolist()})
    return predict, body.encode()


def fit_boosted(x: np.ndarray, y: np.ndarray, w: np.ndarray, spec: dict[str, Any], num_leaves: int,
                n_estimators: int) -> tuple[Predict, bytes]:
    clf = lgb.LGBMClassifier(
        num_leaves=num_leaves, n_estimators=n_estimators, learning_rate=float(spec["learning_rate"]),
        min_child_samples=int(spec["min_child_samples"]), subsample=float(spec["subsample"]), subsample_freq=1,
        colsample_bytree=float(spec["colsample_bytree"]), reg_lambda=float(spec["reg_lambda"]), random_state=SEED,
        n_jobs=1, deterministic=True, force_row_wise=True, verbose=-1).fit(x, y, sample_weight=w)
    booster = clf.booster_

    def predict(q: np.ndarray) -> np.ndarray:
        return np.asarray(booster.predict(q))
    return predict, booster.model_to_string().encode()


def importance(kind: str, body: bytes, inputs: list[str]) -> list[dict[str, Any]]:
    """What the model leans on, strongest first: a logistic model's weights per standard deviation (with their
    sign), a boosted model's share of total gain."""
    if kind == "logistic":
        coef = json.loads(body)["coef"]
        out = [{"input": n, "weight": round(float(c), 4)} for n, c in zip(inputs, coef, strict=True)]
        return sorted(out, key=lambda d: -abs(d["weight"]))
    gain = lgb.Booster(model_str=body.decode()).feature_importance(importance_type="gain")
    total = float(gain.sum()) or 1.0
    return sorted(({"input": n, "gain_share": round(float(g) / total, 4)} for n, g in zip(inputs, gain, strict=True)),
                  key=lambda d: -d["gain_share"])
