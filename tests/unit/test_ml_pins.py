"""Pins on the learning code's shared pieces, written before any of them is moved (stocks plan, F1).

Each value here is a literal worked out on the code as it stood. A pin that fails means a number every trained
model and every recorded signal depends on has changed: the inputs' identity, the training set's hash, the
weights, or what the scorer answers. That is a decision for a record, never a side effect of a refactor.
"""
from __future__ import annotations

import datetime as dt
import io
import json

import numpy as np

from wt.crypto import signals
from wt.ml import dataset, modelfile
from wt.ml import score as scoring

H4 = 14_400
T0 = 1_730_000_000 // H4 * H4
WEIGHTS = [2.187342, 1.640506, 0.972152, 0.820253, 0.729114, 0.729114, 0.729114, 0.729114, 0.729114, 1.093671, 0.729114, 0.911392]
EFFECTIVE_N = 10.062883
LOGISTIC = {"intercept": 0.0, "mean": [2.0, 50.0], "scale": [1.0, 10.0], "coef": [1.0, -0.5]}


def fixed(n: int = 12) -> list[dataset.Example]:
    """Twelve signals with no randomness in them: overlapping holding periods, a missing input, all three sleeves."""
    out = []
    for i in range(n):
        x: dict[str, float | None] = dict.fromkeys(signals.INPUTS, None)
        x.update(atr_pct=1.5 + 0.25 * i, rsi=40.0 + 3 * i, stop_pct=3.0 + 0.5 * i, hour_utc=float(i % 6 * 4))
        if i % 4 == 0:
            x["rsi"] = None
        t = T0 + i * H4
        out.append(dataset.Example(f"s|X/USD|{t}", ("trend", "break", "dip")[i % 3], "X/USD", t, t + (1 + i % 5) * H4,
                                   x, 1.5 if i % 3 else -1.0, "target" if i % 3 else "stop"))
    return out


def test_the_inputs_identity_is_pinned():
    # Changes only with FEATURES_VERSION or the list of inputs, which a decision record changes (DEC-0016).
    assert signals.features_id() == "b9c0ee5c81f1"


def test_the_training_sets_hash_is_pinned():
    assert dataset.data_hash(fixed()) == "c7d899ad63492d24"
    assert dataset.data_hash(fixed(11)) != dataset.data_hash(fixed())
    assert dataset.data_hash([]) == "e3b0c44298fc1c14"                       # sha256 of nothing


def test_the_weights_and_the_matrix_are_pinned():
    w = dataset.uniqueness(fixed(), H4)
    assert [round(float(v), 6) for v in w] == WEIGHTS
    assert round(dataset.effective_n(w), 6) == EFFECTIVE_N
    m = dataset.matrix(fixed(), ["atr_pct", "rsi", "is_trend", "is_dip", "spread_bps"])
    assert m.shape == (12, 5) and np.isnan(m[0, 1]) and np.isnan(m[:, 4]).all()
    assert m[1].tolist()[:4] == [1.75, 43.0, 0.0, 0.0] and m[:, 2].tolist() == [1.0, 0.0, 0.0] * 4
    assert dataset.uniqueness([], H4).tolist() == [] and dataset.effective_n(np.zeros(0)) == 0.0


def test_the_scorers_answer_is_pinned(tmp_path, monkeypatch, capsys):
    now = dt.datetime.now(dt.UTC)
    modelfile.register(tmp_path, "m1-pin", "logistic", json.dumps(LOGISTIC).encode(), ["atr_pct", "rsi"],
                       now.isoformat(), 0.4, 0.6, {"note": "pin"})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rows": [{"atr_pct": 3.0, "rsi": 50.0}, {"atr_pct": 2.0}, {}]})))
    assert scoring.main(["--models", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"version": "m1-pin", "kind": "logistic", "cutoff": 0.4,
                                                   "half_below": 0.6, "scores": [0.731059, 0.5, 0.5]}
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert scoring.main(["--models", str(tmp_path)]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO('{"rows": []}'))
    assert scoring.main(["--models", str(tmp_path / "none")]) == 3



def test_the_shared_pieces_know_no_desk_and_the_old_names_still_resolve():
    """Stocks plan, F2: an example and what is worked out from a list of them live in `wt.ml.examples`, which
    imports nothing of any desk. `wt.ml.dataset` still offers every name."""
    import ast
    from pathlib import Path

    from wt.ml import examples
    src = Path(examples.__file__).read_text()
    wt_imports = {n.module for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("wt")}
    assert wt_imports == set()
    for name in ("Example", "uniqueness", "effective_n", "matrix", "data_hash"):
        assert getattr(dataset, name) is getattr(examples, name)


def test_the_scorer_checks_the_inputs_identity_it_is_given(tmp_path, monkeypatch, capsys):
    now = dt.datetime.now(dt.UTC)
    modelfile.register(tmp_path, "m1-other", "logistic", json.dumps(LOGISTIC).encode(), ["atr_pct", "rsi"],
                       now.isoformat(), 0.4, 0.6, {"note": "pin"}, features="feedfacecafe")
    req = json.dumps({"rows": [{"atr_pct": 3.0, "rsi": 50.0}]})
    monkeypatch.setattr("sys.stdin", io.StringIO(req))
    assert scoring.main(["--models", str(tmp_path)]) == 6                     # the crypto desk's inputs: another question
    monkeypatch.setattr("sys.stdin", io.StringIO(req))
    assert scoring.main(["--models", str(tmp_path), "--features-id", "feedfacecafe"]) == 0
    assert json.loads(capsys.readouterr().out)["scores"] == [0.731059]
