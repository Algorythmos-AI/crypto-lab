"""The training pipeline (DEC-0016, 3): purged validation, the allowed settings, the choice. Runs in the ML
environment; skipped where it is not installed."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

try:
    import numpy as np
    import sklearn  # noqa: F401
    import lightgbm  # noqa: F401
except ImportError as e:                                    # pragma: no cover
    if os.environ.get("WT_ML_REQUIRED"):
        raise
    raise unittest.SkipTest(f"the ML environment is not installed ({e.name})") from e

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from wt.crypto import signals  # noqa: E402
from wt.ml import dataset, models, train, validate  # noqa: E402
from wt.ml import score as scoring  # noqa: E402
from wt.ml import modelfile  # noqa: E402

CFG = yaml.safe_load((ROOT / "config/crypto.yaml").read_text())
DAY, H4 = 86_400, 14_400
T0 = 1_730_000_000 // H4 * H4


def examples(n: int = 900, seed: int = 5, signal: float = 1.2) -> list[dataset.Example]:
    """Signals four hours apart whose chance of winning rises with `atr_pct` and falls with `rsi`."""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        atr, rsi = float(rng.normal(2.0, 0.7)), float(rng.normal(55, 10))
        z = signal * (atr - 2.0) - 0.08 * signal * (rsi - 55) + rng.normal(0, 1.0)
        x = dict.fromkeys(signals.INPUTS, None)
        x.update(atr_pct=atr, rsi=rsi, stop_pct=2 * atr, volume_ratio=float(abs(rng.normal(1, 0.4))),
                 breadth=float(rng.integers(1, 5)), hour_utc=float(i % 6 * 4), day_of_week=float(i // 6 % 7))
        t = T0 + i * H4
        out.append(dataset.Example(f"break|X/USD|{t}", ("trend", "break", "dip")[i % 3], "X/USD", t,
                                   t + int(rng.integers(1, 30)) * H4, x, 1.5 if z > 0 else -1.0, "target" if z > 0 else "stop"))
    return out


class Validation(unittest.TestCase):
    def test_no_fold_trains_on_a_signal_whose_outcome_was_not_yet_known(self) -> None:
        ex = examples()
        embargo = 30 * DAY
        seen = 0
        for train_idx, test_idx in validate.folds(ex, 5, embargo):
            first_test = min(ex[i].t for i in test_idx)
            self.assertTrue(all(ex[i].exit_t < first_test for i in train_idx))
            self.assertTrue(all(ex[i].t < first_test - embargo for i in train_idx))
            self.assertFalse(set(train_idx) & set(test_idx))
            seen += 1
        self.assertGreaterEqual(seen, 3)

    def test_an_outcome_still_open_at_the_test_block_keeps_its_signal_out_of_training(self) -> None:
        ex = examples()
        late = ex[100]
        ex[100] = dataset.Example(late.sid, late.sleeve, late.pair, late.t, ex[-1].t + DAY, late.inputs, late.r, late.reason)
        for train_idx, _ in validate.folds(ex, 5, 0):
            self.assertNotIn(100, set(train_idx))                # it finishes after every test block starts

    def test_weights_are_smaller_where_trades_overlap(self) -> None:
        ex = examples(60)
        crowd = [dataset.Example(f"c{i}", "break", "Y/USD", T0, T0 + 40 * H4, ex[0].inputs, 1.0, "target") for i in range(10)]
        alone = dataset.Example("alone", "break", "Z/USD", T0 + 400 * H4, T0 + 410 * H4, ex[0].inputs, 1.0, "target")
        w = dataset.uniqueness([*crowd, alone], H4)
        self.assertLess(w[0], w[-1])
        self.assertAlmostEqual(float(w.mean()), 1.0, places=6)


class Training(unittest.TestCase):
    def test_a_real_pattern_is_found_out_of_sample_and_noise_is_not(self) -> None:
        ex = examples()
        inputs = dataset.usable_inputs(ex)
        self.assertIn("atr_pct", inputs)
        self.assertNotIn("spread_pct", inputs)                   # history cannot supply it
        self.assertNotIn("held_elsewhere", inputs)
        got = train.compare(CFG, ex, inputs)
        self.assertIn(got["chosen"], ("m1", "m2"))
        best = got["best"][got["chosen"]]
        self.assertLess(best["log_loss"], got["m0"]["log_loss"])
        self.assertGreater(best["kept_mean_r"], best["dropped_mean_r"])
        self.assertEqual(len(got["tried"]["m1"]), 3)             # the charter's settings and no others
        self.assertEqual(len(got["tried"]["m2"]), 4)
        noise = train.compare(CFG, examples(signal=0.0), inputs)
        self.assertIsNone(noise["chosen"])                       # nothing to find: no candidate, nothing registered

    def test_training_twice_on_the_same_data_gives_the_same_model(self) -> None:
        ex = examples(400)
        inputs = dataset.usable_inputs(ex)
        for kind, settings in (("m1", {"C": 1}), ("m2", {"num_leaves": 4, "n_estimators": 100})):
            a, sa, ca = train.train_final(CFG, ex, inputs, kind, settings)
            b, sb, cb = train.train_final(CFG, ex, inputs, kind, settings)
            self.assertEqual((a, ca), (b, cb))
            np.testing.assert_array_equal(sa, sb)
        self.assertEqual(dataset.data_hash(ex), dataset.data_hash(examples(400)))

    def test_the_registered_model_scores_through_the_scorer_as_it_did_in_training(self) -> None:
        import datetime as dt
        import tempfile
        ex = examples(400)
        inputs = dataset.usable_inputs(ex)
        for kind, name, settings in (("m1", "logistic", {"C": 1}), ("m2", "lightgbm", {"num_leaves": 4, "n_estimators": 100})):
            body, scores, cal = train.train_final(CFG, ex, inputs, kind, settings)
            self.assertNotEqual(cal, (1.0, 0.0))                 # enough examples: a calibration was fitted
            with tempfile.TemporaryDirectory() as tmp:
                now = dt.datetime.now(dt.UTC)
                modelfile.register(Path(tmp), "v", name, body, inputs, now.isoformat(), 0.4, 0.6, {}, cal)
                rows = [{**{k: v for k, v in e.inputs.items() if v is not None}, f"is_{e.sleeve}": 1.0,
                         **{f"is_{n}": 0.0 for n in ("trend", "break", "dip") if n != e.sleeve}} for e in ex[:40]]
                got = scoring.score(Path(tmp), rows, now, 14)["scores"]
            np.testing.assert_allclose(got, scores[:40], atol=1e-5)
            self.assertTrue(models.importance(name, body, inputs))


class Calibration(unittest.TestCase):
    def test_overconfident_scores_are_pulled_back_without_changing_their_order(self) -> None:
        p = np.array([0.02, 0.2, 0.5, 0.8, 0.98])
        np.testing.assert_allclose(validate.platt(p, 1.0, 0.0), p, atol=1e-6)
        soft = validate.platt(p, 0.3, -0.5)
        self.assertTrue(np.all(np.diff(soft) > 0))                # monotone: the ranking is untouched
        self.assertLess(soft.max() - soft.min(), p.max() - p.min())

    def test_calibration_is_fitted_only_on_predictions_for_unseen_signals_and_improves_log_loss(self) -> None:
        ex = examples(900, signal=0.5)
        inputs = dataset.usable_inputs(ex)
        x, w = dataset.matrix(ex, inputs), dataset.uniqueness(ex, H4)
        fit = train._fit(CFG, "m2", {"num_leaves": 8, "n_estimators": 300})
        raw = validate.walk_forward(ex, x, w, fit, 5, 30 * DAY, 40, calibrate=False)
        cal = validate.walk_forward(ex, x, w, fit, 5, 30 * DAY, 40, calibrate=True)
        self.assertLess(cal["log_loss"], raw["log_loss"])
        self.assertEqual(validate.calibration(ex[:60], x[:60], w[:60], fit, 30 * DAY), (1.0, 0.0))    # too few: none


if __name__ == "__main__":
    unittest.main()
