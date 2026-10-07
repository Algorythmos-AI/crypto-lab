"""Real models through the real scorer (DEC-0016). Runs in the ML environment: `python -m unittest discover -s
tests/ml -t .`. In the trading environment, which has no ML library, the module is skipped."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import lightgbm as lgb
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
except ImportError as e:                                    # pragma: no cover
    if os.environ.get("WT_ML_REQUIRED"):
        raise
    raise unittest.SkipTest(f"the ML environment is not installed ({e.name})") from e

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from wt.core import desk as desks  # noqa: E402
from wt.crypto import scorer  # noqa: E402
from wt.ml import modelfile  # noqa: E402
from wt.ml import score as scoring  # noqa: E402

INPUTS = ["atr_pct", "rsi", "volume_ratio"]


def data(n: int = 400, seed: int = 3):
    rng = np.random.default_rng(seed)
    x = rng.normal([2.0, 50.0, 1.0], [0.8, 12.0, 0.5], size=(n, 3))
    y = (x[:, 0] * 0.9 - (x[:, 1] - 50) * 0.05 + rng.normal(0, 0.8, n) > 1.8).astype(int)
    return x, y


def rows(x) -> list[dict]:
    return [dict(zip(INPUTS, map(float, r), strict=True)) for r in x]


class Models(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.desk = dataclasses.replace(desks.DESKS["crypto"], state_dir=Path(self.tmp.name) / "crypto")
        self.models = scorer.models_dir(self.desk)
        self.now = dt.datetime.now(dt.UTC)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_logistic_model_scores_as_scikit_learn_does(self) -> None:
        x, y = data()
        scaler = StandardScaler().fit(x)
        clf = LogisticRegression(C=1.0).fit(scaler.transform(x), y)
        body = json.dumps({"intercept": float(clf.intercept_[0]), "mean": scaler.mean_.tolist(),
                           "scale": scaler.scale_.tolist(), "coef": clf.coef_[0].tolist()}).encode()
        modelfile.register(self.models, "m1", "logistic", body, INPUTS, self.now.isoformat(), 0.4, 0.6, {})
        got = scoring.score(self.models, rows(x[:50]), self.now, 14)
        want = clf.predict_proba(scaler.transform(x[:50]))[:, 1]
        np.testing.assert_allclose(got["scores"], want, atol=1e-5)
        self.assertEqual((got["version"], got["kind"]), ("m1", "logistic"))

    def test_a_boosted_model_scores_as_lightgbm_does_and_takes_missing_inputs(self) -> None:
        x, y = data()
        clf = lgb.LGBMClassifier(n_estimators=50, num_leaves=4, learning_rate=0.05, min_child_samples=40,
                                 subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=5, random_state=7,
                                 verbose=-1).fit(x, y)
        body = clf.booster_.model_to_string().encode()
        modelfile.register(self.models, "m2", "lightgbm", body, INPUTS, self.now.isoformat(), 0.4, 0.6, {})
        got = scoring.score(self.models, rows(x[:50]), self.now, 14)
        np.testing.assert_allclose(got["scores"], clf.predict_proba(x[:50])[:, 1], atol=1e-6)
        partial = scoring.score(self.models, [{"atr_pct": 2.0}, {}], self.now, 14)["scores"]
        self.assertTrue(all(0.0 <= s <= 1.0 for s in partial))

    def test_the_trading_side_gets_the_same_scores_through_the_process(self) -> None:
        x, y = data()
        clf = lgb.LGBMClassifier(n_estimators=30, num_leaves=4, min_child_samples=40, random_state=7, verbose=-1).fit(x, y)
        modelfile.register(self.models, "m2", "lightgbm", clf.booster_.model_to_string().encode(), INPUTS,
                           self.now.isoformat(), 0.4, 0.6, {})
        got, why = scorer.score(rows(x[:20]), self.desk, python=sys.executable, root=ROOT)
        self.assertIsNone(why)
        np.testing.assert_allclose(got.scores, clf.predict_proba(x[:20])[:, 1], atol=1e-6)
        self.assertTrue(all(got.factor(i) in (0.0, 0.5, 1.0) for i in range(20)))

    def test_a_damaged_boosted_model_is_an_exit_code_not_a_traceback(self) -> None:
        modelfile.register(self.models, "bad", "lightgbm", b"this is not a model", INPUTS, self.now.isoformat(), 0.4, 0.6, {})
        self.assertEqual(scorer.score([{"atr_pct": 1.0}], self.desk, python=sys.executable, root=ROOT), (None, "bad_model"))


if __name__ == "__main__":
    unittest.main()
