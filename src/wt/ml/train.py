"""Train, validate, choose and register (DEC-0016, 3).

    .venv-ml/bin/python -m wt.ml.train                      # build the training set, validate, print the comparison
    .venv-ml/bin/python -m wt.ml.train --register           # and register the chosen model for the desk
    .venv-ml/bin/python -m wt.ml.train --out research/experiments/EXP-0017-crypto-models

The settings tried are the charter's and no others. The simplest model whose out-of-sample log-loss is within one
standard error of the best is chosen, provided it beats the model that takes every signal (M0). If none does,
there is no candidate, nothing is registered, and that is the result.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from typing import Any

import numpy as np

from wt.core.config import ROOT, load_yaml
from wt.core.desk import DESKS
from wt.crypto import signals
from wt.crypto.data import CoinbasePublic, DataError
from wt.crypto.history import DAY, WARMUP_D, load_hourly
from wt.ml import dataset, modelfile, models, validate

MIN_HISTORY_D = 548             # 18 months of hourly bars, or the pair is not used for training (DEC-0016)
ORDER = ("m1", "m2")            # simplest first
FETCH_TRIES, FETCH_PAUSE_S = 3, 5.0
MIN_PAIRS_SHARE = 0.8           # of learning.training_pairs; below it the run stops instead of training on less


class TooFewPairs(RuntimeError):
    """The training universe did not load: a result from it would not be the charter's experiment."""


def history(pairs: list[str], start: int, end: int, offline: bool, sleep: Any = time.sleep) -> dict[str, list[Any]]:
    """Hourly bars per pair. A pair that fails to load is tried again (a public endpoint has bad minutes), and the
    reason a pair is left out says which of three things happened: the fetch failed, there is nothing for it, or
    it has under 18 months of bars."""
    client = None if offline else CoinbasePublic()
    out: dict[str, list[Any]] = {}
    for pair in pairs:
        bars, why = [], None
        for attempt in range(1 if offline else FETCH_TRIES):
            try:
                bars, why = load_hourly(pair, start - WARMUP_D * DAY, end, client), None
                break
            except DataError as e:
                why = f"the fetch failed ({e})"
                sleep(FETCH_PAUSE_S * (attempt + 1))
        if why is None and not bars:
            why = "nothing cached for it" if offline else "the exchange returned no bars for it"
        elif why is None and (bars[-1].t - bars[0].t) < MIN_HISTORY_D * DAY:
            why = "under 18 months of bars"
        if why is None:
            out[pair] = bars
        else:
            print(f"history {pair}: skipped ({why})")
    return out


def check_universe(cfg: dict[str, Any], loaded: dict[str, list[Any]]) -> None:
    """Every traded pair, and most of the training pairs, or no run: a universe that quietly shrank (seven pairs
    were once lost to a failed fetch and reported as short histories) is a different experiment."""
    missing = sorted(set(cfg["sleeves"]["common"]["pairs"]) - set(loaded))
    if missing:
        raise TooFewPairs(f"traded pairs without history: {', '.join(missing)}")
    wanted = len(cfg["learning"]["training_pairs"])
    if len(loaded) < MIN_PAIRS_SHARE * wanted:
        raise TooFewPairs(f"only {len(loaded)} of {wanted} training pairs loaded")


def compare(cfg: dict[str, Any], examples: list[dataset.Example], inputs: list[str]) -> dict[str, Any]:
    """Every allowed setting of every model through the walk-forward folds, and the choice."""
    lg = cfg["learning"]
    x, w = dataset.matrix(examples, inputs), dataset.uniqueness(examples, int(cfg["sleeves"]["common"]["timeframe_min"]) * 60)
    embargo = _embargo(cfg)
    n, cut = int(lg["validation"]["folds"]), float(lg["promotion"]["cutoff_percentile"])
    m1, m2 = lg["models"]["m1"], lg["models"]["m2"]
    tried: dict[str, list[dict[str, Any]]] = {"m1": [], "m2": []}
    for s in models.grid(m1, ("C",)):
        res = validate.walk_forward(examples, x, w, _fit(cfg, "m1", s), n, embargo, cut)
        tried["m1"].append({"settings": s, **res})
    for s in models.grid(m2, ("num_leaves", "n_estimators")):
        res = validate.walk_forward(examples, x, w, _fit(cfg, "m2", s), n, embargo, cut)
        tried["m2"].append({"settings": s, **res})
    best = {k: min(v, key=lambda d: d["log_loss"]) for k, v in tried.items()}
    m0 = validate.walk_forward(examples, x, w, None, n, embargo, cut)
    top = min(best.values(), key=lambda d: d["log_loss"])
    bar = top["log_loss"] + (top["log_loss_se"] or 0.0)
    chosen = next((k for k in ORDER if best[k]["log_loss"] <= bar and best[k]["log_loss"] < m0["log_loss"]), None)
    return {"m0": m0, "tried": tried, "best": best, "chosen": chosen, "embargo_days": embargo // DAY, "folds": n,
            "calibration": "platt, 3 inner purged folds (DEC-0017)", "attempt": 3,
            "effective_n": round(dataset.effective_n(w), 1)}


def _fit(cfg: dict[str, Any], kind: str, settings: dict[str, Any]) -> validate.Fit:
    m2 = cfg["learning"]["models"]["m2"]
    if kind == "m1":
        return lambda a, b, c: models.fit_logistic(a, b, c, **settings)[0]
    return lambda a, b, c: models.fit_boosted(a, b, c, m2, **settings)[0]


def _embargo(cfg: dict[str, Any]) -> int:
    tf_s = int(cfg["sleeves"]["common"]["timeframe_min"]) * 60
    return max(int(v["time_stop_bars"]) for k, v in cfg["sleeves"].items() if k != "common") * tf_s


def train_final(cfg: dict[str, Any], examples: list[dataset.Example], inputs: list[str], kind: str,
                settings: dict[str, Any]) -> tuple[bytes, np.ndarray, tuple[float, float]]:
    """The chosen model on every example, its calibrated scores on them, and its calibration (DEC-0017)."""
    x, y = dataset.matrix(examples, inputs), np.array([e.y for e in examples], dtype=float)
    w = dataset.uniqueness(examples, int(cfg["sleeves"]["common"]["timeframe_min"]) * 60)
    predict, body = (models.fit_logistic(x, y, w, **settings) if kind == "m1"
                     else models.fit_boosted(x, y, w, cfg["learning"]["models"]["m2"], **settings))
    a, b = validate.calibration(examples, x, w, _fit(cfg, kind, settings), _embargo(cfg))
    return body, validate.platt(predict(x), a, b), (a, b)


def quintiles(values: np.ndarray) -> list[float]:
    """The four edges that cut the finite values into five equal parts."""
    v = values[np.isfinite(values)]
    return [float(q) for q in np.percentile(v, [20, 40, 60, 80])] if len(v) else []


def drift_edges(x: np.ndarray, inputs: list[str]) -> dict[str, list[float]]:
    """Quintile edges of every input that varies enough to have them (a 0/1 input has none)."""
    out = {}
    for k, name in enumerate(inputs):
        edges = quintiles(x[:, k])
        if len(set(edges)) == 4:
            out[name] = edges
    return out


def _slim(d: dict[str, Any]) -> dict[str, Any]:
    return {k: d.get(k) for k in ("log_loss", "log_loss_se", "kept", "kept_mean_r", "dropped", "dropped_mean_r", "spread",
                                  "spread_ci")}


def report(res: dict[str, Any]) -> str:
    day = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).date().isoformat()      # noqa: E731
    c = res["comparison"]
    lines = [f"# {res['id']}: crypto desk, the first models", "",
             f"- Decision: {res['decision']} (DEC-0016 as amended). Training set: {res['examples']} signals from {len(res['pairs'])} pairs, "
             f"{day(res['start'])} to {day(res['end'])} (data hash `{res['data_hash']}`).",
             f"- Win rate of all signals after costs: {res['win_rate'] * 100:.1f}%. Mean R: {res['mean_r']:+.3f}.",
             f"- Validation: {c['folds']} purged walk-forward folds, embargo {c['embargo_days']} days. "
             f"Inputs used: {len(res['inputs'])}.",
             f"- Calibration: {c.get('calibration', 'none')}. This is attempt {c.get('attempt', 1)} at model selection.",
             f"- Overlapping trades are down-weighted: the {res['examples']} signals count as about "
             f"{c['effective_n']:.0f} independent ones. Training signals per fold: "
             f"{', '.join(str(v) for v in c['m0']['fold_train_n'])}.", "",
             "## Comparison on signals the model never saw", "",
             "Kept minus skipped is the difference in mean R, with a 95% interval from a bootstrap over days.", "",
             "| Model | Settings | Log-loss | Kept | Mean R kept | Skipped | Mean R skipped | Kept minus skipped |",
             "|---|---|---|---|---|---|---|---|"]

    def row(name: str, d: dict[str, Any], settings: str) -> str:
        f = lambda v: "-" if v is None else f"{v:+.3f}"                           # noqa: E731
        ci = d.get("spread_ci")
        gap = "-" if ci is None else f"{d['spread']:+.3f} ({ci[0]:+.3f} to {ci[1]:+.3f})"
        return (f"| {name} | {settings} | {d['log_loss']:.4f} | {d['kept']} | {f(d['kept_mean_r'])} | {d['dropped']} | "
                f"{f(d['dropped_mean_r'])} | {gap} |")
    lines.append(row("M0 take everything", c["m0"], "-"))
    for k, label in (("m1", "M1 logistic regression"), ("m2", "M2 boosted trees")):
        for d in c["tried"][k]:
            mark = " (best of its kind)" if d is c["best"][k] or d == c["best"][k] else ""
            lines.append(row(label + mark, d, ", ".join(f"{a}={b}" for a, b in d["settings"].items())))
    lines += ["", "## Choice", ""]
    if c["chosen"] is None:
        lines.append("**No candidate.** No model beat taking every signal on unseen data, so none is registered.")
    else:
        b = c["best"][c["chosen"]]
        lines.append(f"**{c['chosen'].upper()}** ({', '.join(f'{a}={v}' for a, v in b['settings'].items())}): the simplest "
                     "model within one standard error of the best that also beats M0.")
        lines += ["", "What it leans on, strongest first:", ""]
        lines += [f"- `{d['input']}`: {d.get('weight', d.get('gain_share'))}" for d in res["importance"][:8]]
    lines += ["", "## Notes", "",
              "- Each signal is followed on its own with its sleeve's exits and costs (`wt.crypto.signals.outcome`).",
              "- A registered model only scores signals in shadow. It acts on nothing until it passes the checkpoint",
              "  test in DEC-0016, section 4, on signals that finish after it was trained.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.ml.train")
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--offline", action="store_true", help="use the cached history only")
    ap.add_argument("--register", action="store_true", help="register the chosen model for the desk")
    ap.add_argument("--out", default=None, help="write result.json and report.md into this folder")
    ap.add_argument("--summary", default=None, help="write the run's result as one JSON file here (the weekly job)")
    a = ap.parse_args(argv)
    cfg = load_yaml("crypto.yaml")
    end = (a.end or int(time.time())) // 14_400 * 14_400
    start = end - int(a.years * 365 * DAY)
    hourly = history(list(cfg["learning"]["training_pairs"]), start, end, a.offline)
    try:
        check_universe(cfg, hourly)
    except TooFewPairs as e:
        print(f"no run: {e}")
        return 1
    t0 = time.time()
    examples = dataset.build(cfg, hourly, start, end)
    print(f"training set: {len(examples)} signals from {len(hourly)} pairs in {time.time() - t0:.0f}s")
    if len(examples) < 200:
        print("too few signals to train on")
        return 1
    inputs = dataset.usable_inputs(examples)
    comparison = compare(cfg, examples, inputs)
    res: dict[str, Any] = {
        "decision": "DEC-0018", "start": start, "end": end, "pairs": sorted(hourly), "examples": len(examples),
        "win_rate": float(np.mean([e.y for e in examples])), "mean_r": float(np.mean([e.r for e in examples])),
        "by_sleeve": {n: {"n": sum(e.sleeve == n for e in examples),
                          "mean_r": round(float(np.mean([e.r for e in examples if e.sleeve == n])), 4)}
                      for n in sorted({e.sleeve for e in examples})},
        "inputs": inputs, "data_hash": dataset.data_hash(examples), "comparison": comparison, "importance": []}
    chosen = comparison["chosen"]
    if chosen is not None:
        settings = comparison["best"][chosen]["settings"]
        body, scores, (cal_a, cal_b) = train_final(cfg, examples, inputs, chosen, settings)
        kind = "logistic" if chosen == "m1" else "lightgbm"
        res["importance"] = models.importance(kind, body, inputs)
        p = cfg["learning"]["promotion"]
        cutoff = float(np.percentile(scores, float(p["cutoff_percentile"])))
        half = float(np.percentile(scores, float(p["half_size_below_percentile"])))
        trained = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        version = f"{chosen}-{dt.datetime.now(dt.UTC):%Y%m%d}-{res['data_hash'][:8]}"
        lineage = f"{chosen}:" + ",".join(f"{k}={v}" for k, v in sorted(settings.items()))
        res["model"] = {"version": version, "kind": kind, "settings": settings, "cutoff": cutoff, "half_below": half,
                        "calibration": [cal_a, cal_b], "lineage": lineage}
        if a.register:
            card = {**{k: res[k] for k in ("decision", "start", "end", "examples", "win_rate", "mean_r", "by_sleeve",
                                           "inputs", "data_hash", "importance")},
                    "version": version, "kind": kind, "settings": settings, "trained_at": trained, "lineage": lineage,
                    "validation": {"m0": comparison["m0"], "chosen": comparison["best"][chosen]}, "state": "shadow",
                    # What the training data looked like, for the drift check (DEC-0016, 4): the edges that cut
                    # each input, and the scores, into five equal parts.
                    "drift": {"edges": drift_edges(dataset.matrix(examples, inputs), inputs), "scores": quintiles(scores)}}
            modelfile.register(DESKS["crypto"].state_dir / "models", version, kind, body, inputs, trained, cutoff, half,
                               {**card, "features": signals.features_id()}, (cal_a, cal_b), lineage,
                               signals.features_id())
            print(f"registered {version}")
    print(json.dumps({"chosen": chosen, "m0": comparison["m0"]["log_loss"],
                      **{k: v["log_loss"] for k, v in comparison["best"].items()}}))
    if a.summary:
        from pathlib import Path
        slim = {"t": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "chosen": chosen, "model": res.get("model"),
                **{k: res[k] for k in ("decision", "start", "end", "examples", "win_rate", "mean_r", "data_hash")},
                "pairs": len(res["pairs"]), "effective_n": comparison["effective_n"], "attempt": comparison["attempt"],
                "m0": _slim(comparison["m0"]), "best": {k: {"settings": v["settings"], **_slim(v)} for k, v in comparison["best"].items()},
                "importance": res["importance"][:8]}
        Path(a.summary).parent.mkdir(parents=True, exist_ok=True)
        Path(a.summary).write_text(json.dumps(slim, indent=1, sort_keys=True, default=float) + "\n")
    if a.out:
        folder = ROOT / a.out
        folder.mkdir(parents=True, exist_ok=True)
        res["id"] = folder.name.split("-crypto")[0]
        (folder / "result.json").write_text(json.dumps(res, indent=1, sort_keys=True, default=float) + "\n")
        (folder / "report.md").write_text(report(res))
        print(f"wrote {folder.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
