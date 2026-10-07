"""Whether the model in force may act (DEC-0016, 4): shadow, promotion at fixed checkpoints, demotion, drift.

A registered model scores every signal of the three registered sleeves before the signal's outcome exists. It
acts on nothing until it passes a test on those signals, and the test is only looked at after every 60 of them
have finished, at most 6 times for one lineage, with the significance level split across the looks.

    state       shadow     scores, acts on nothing
                acting     skips a signal scored below the cut-off, halves one scored just above it
                demoted    failed the rolling test while acting; acts on nothing again
                suspended  the market no longer looks like the training data; acts on nothing until retrained

The state is `var/crypto/models/promotion.json`; every change of it is a `model` row in the desk's journal.

Where the charter is silent this module reads it narrowly:
  * A "lineage" is a model kind with its settings (`m1:C=1`). Weekly retrainings of one lineage share its
    checkpoints: every signal counted was scored by a model trained before the signal existed. A retraining
    that chooses another kind or other settings starts a new lineage in shadow, with no checkpoints used.
  * "The plain win rate" a model's Brier score must beat is the win rate of the very signals it is tested on:
    the best a constant can do, so the harder of the two readings.
  * The stability index is measured over five equal parts of the training data. With 60 signals a finer cut
    would trip on noise alone.
  * A demoted lineage is not promoted again. The model does not apply to the baseline rule or to challengers:
    it was trained on the three registered rules and on nothing else.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wt.core.desk import Desk
from wt.crypto import risk, rules
from wt.crypto.book import write_atomic

SHADOW, ACTING, DEMOTED, SUSPENDED = "shadow", "acting", "demoted", "suspended"
N_BOOT, SEED, PARTS = 5000, 7, 5


def models_dir(desk: Desk) -> Path:
    return desk.state_dir / "models"


def state_path(desk: Desk) -> Path:
    return models_dir(desk) / "promotion.json"


@dataclass(frozen=True)
class Pointer:
    """What the trading side needs to know of the model in force. The file is the ML side's
    (`wt.ml.modelfile`); this side reads it as plain JSON and imports nothing from there."""
    version: str
    lineage: str
    trained_at: str


def pointer(desk: Desk) -> Pointer | None:
    try:
        d = json.loads((models_dir(desk) / "current.json").read_text())
        version = str(d["version"])
        if not version or "/" in version or version.startswith("."):
            return None
        return Pointer(version, str(d.get("lineage") or version), str(d.get("trained_at") or ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_state(desk: Desk) -> dict[str, Any]:
    try:
        got = json.loads(state_path(desk).read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(desk: Desk, state: dict[str, Any]) -> None:
    state_path(desk).parent.mkdir(parents=True, exist_ok=True)
    write_atomic(state_path(desk), json.dumps(state, indent=1, sort_keys=True))


def card(desk: Desk, version: str) -> dict[str, Any]:
    try:
        got = json.loads((models_dir(desk) / version / "card.json").read_text())
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass(frozen=True)
class InForce:
    """What the bar cycle does with the model: nothing, score in shadow, or act."""
    version: str | None
    lineage: str | None
    acting: bool

    @property
    def scoring(self) -> bool:
        return self.version is not None


def in_force(desk: Desk) -> InForce:
    """Read by every bar cycle. The owner's switch, a missing pointer or an unreadable one all mean no model."""
    p = pointer(desk)
    if p is None or risk.learning_file(desk).exists():
        return InForce(None, None, False)
    state = load_state(desk)
    return InForce(p.version, p.lineage, state.get("lineage") == p.lineage and state.get("state") == ACTING)


def applies_to(sleeve: str) -> bool:
    return sleeve in rules.NAMES


def model_row(rec: dict[str, Any]) -> dict[str, float]:
    """A signal row as the scorer reads it: its inputs that have a value, and which sleeve it is."""
    row = {k: float(v) for k, v in (rec.get("inputs") or {}).items() if isinstance(v, int | float) and not isinstance(v, bool)}
    row.update({f"is_{n}": float(rec.get("sleeve") == n) for n in rules.NAMES})
    return row


# ---- the tests ----

@dataclass(frozen=True)
class Obs:
    sid: str
    t: str          # when the signal was recorded
    day: str        # the UTC day its outcome fell on
    exit_t: str
    score: float
    cutoff: float
    r: float
    inputs: dict[str, Any]
    model: str = ""             # the version that scored it

    @property
    def kept(self) -> bool:
        return self.score >= self.cutoff


def scored(rows: list[dict[str, Any]], lineage: str) -> list[Obs]:
    """Signals this lineage scored whose outcome is known, in the order their trades ended."""
    outcome = {r.get("sid"): r for r in rows if r.get("kind") == "outcome"}
    out, seen = [], set()
    for r in rows:
        if r.get("kind") != "signal" or r.get("lineage") != lineage or r.get("sid") in seen:
            continue
        o = outcome.get(r.get("sid"))
        if o is None or not isinstance(r.get("score"), int | float) or not isinstance(o.get("r"), int | float):
            continue
        seen.add(r.get("sid"))
        out.append(Obs(str(r["sid"]), str(r.get("t")), str(o.get("exit_t"))[:10], str(o.get("exit_t")), float(r["score"]),
                       float(r.get("cutoff", 0.0)), float(o["r"]), dict(r.get("inputs") or {}), str(r.get("model") or "")))
    return sorted(out, key=lambda x: (x.exit_t, x.sid))


def spread(obs: list[Obs]) -> float | None:
    """Mean R of the signals at or above the cut-off, minus mean R of those below it."""
    kept, dropped = [o.r for o in obs if o.kept], [o.r for o in obs if not o.kept]
    return sum(kept) / len(kept) - sum(dropped) / len(dropped) if kept and dropped else None


def spread_lower(obs: list[Obs], alpha: float, n_boot: int = N_BOOT, seed: int = SEED) -> float | None:
    """The one-sided lower bound of the spread at `alpha`, from a bootstrap over the days outcomes fell on. Above
    zero means the kept signals did better than the skipped ones by more than luck allows at that level."""
    import numpy as np
    days: dict[str, list[Obs]] = {}
    for o in obs:
        days.setdefault(o.day, []).append(o)
    keys = sorted(days)
    if len(keys) < 2 or spread(obs) is None:
        return None
    # Per day: sums and counts for each side, so one resample is four additions.
    table = np.array([[sum(o.r for o in days[k] if o.kept), sum(1 for o in days[k] if o.kept),
                       sum(o.r for o in days[k] if not o.kept), sum(1 for o in days[k] if not o.kept)] for k in keys])
    rng = np.random.default_rng(seed)
    got = table[rng.integers(0, len(keys), size=(n_boot, len(keys)))].sum(axis=1)
    ok = (got[:, 1] > 0) & (got[:, 3] > 0)
    if ok.mean() < 0.9:
        return None                                         # one side is too thin for the test to mean anything
    diff = got[ok, 0] / got[ok, 1] - got[ok, 2] / got[ok, 3]
    return float(np.quantile(diff, alpha))


def brier(obs: list[Obs]) -> tuple[float, float]:
    """(the model's Brier score, that of always saying the sample's own win rate)."""
    wins = [float(o.r > 0) for o in obs]
    base = sum(wins) / len(wins)
    return (sum((o.score - w) ** 2 for o, w in zip(obs, wins, strict=True)) / len(obs),
            sum((base - w) ** 2 for w in wins) / len(wins))


def psi(edges: list[float], values: list[float]) -> float | None:
    """Population-stability index of `values` against data cut into equal parts by `edges`."""
    if len(edges) != PARTS - 1 or not values:
        return None
    counts = [0] * PARTS
    for v in values:
        counts[sum(v > e for e in edges)] += 1
    expected, total, floor = 1.0 / PARTS, len(values), 0.5 / len(values)
    return sum((max(c / total, floor) - expected) * math.log(max(c / total, floor) / expected) for c in counts)


def drift(card_drift: dict[str, Any], latest: list[Obs], limit: float, max_inputs: int) -> dict[str, Any]:
    """Whether the latest signals have moved away from the training data: the scores, or `max_inputs` inputs."""
    score_psi = psi(list(card_drift.get("scores") or []), [o.score for o in latest])
    moved = {}
    for name, edges in (card_drift.get("edges") or {}).items():
        vals = [float(o.inputs[name]) for o in latest if isinstance(o.inputs.get(name), int | float)]
        if len(vals) >= len(latest) * 0.8 and (v := psi(list(edges), vals)) is not None and v > limit:
            moved[name] = round(v, 3)
    return {"score_psi": None if score_psi is None else round(score_psi, 3), "inputs": moved,
            "drifted": (score_psi is not None and score_psi > limit) or len(moved) >= max_inputs}


# ---- the daily evaluation ----

def evaluate(desk: Desk, cfg: dict[str, Any], rows: list[dict[str, Any]], now: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bring the state up to date with the journal. Returns (state, the events to record): each event is a
    `model` row for the journal, written by the caller. Looks only happen at checkpoints."""
    pr = cfg["learning"]["promotion"]
    step, most = int(pr["checkpoint_signals"]), int(pr["max_checkpoints"])
    alpha = float(pr["alpha"]) / most
    stamp = dt.datetime.fromtimestamp(now, dt.UTC).isoformat(timespec="seconds")
    state, events = load_state(desk), []
    p = pointer(desk)

    def event(what: str, **more: Any) -> None:
        events.append({"kind": "model", "event": what, "t": stamp, "model": state.get("version"),
                       "lineage": state.get("lineage"), **more})

    # A lineage that is no longer in force keeps what it had used: if a later retraining chooses it again it
    # carries on from there, with the checkpoints it has left and no test taken twice.
    past: dict[str, Any] = dict(state.get("past") or {})
    if state.get("lineage") and (p is None or state["lineage"] != p.lineage):
        past[state["lineage"]] = {k: v for k, v in state.items() if k != "past"}
    if p is None:
        if not state.get("lineage"):
            return state, events
        event("none")
        return {"state": None, "lineage": None, "version": None, "updated": stamp, "past": past}, events
    if state.get("lineage") != p.lineage:
        if p.lineage in past:
            state = {**past.pop(p.lineage), "version": p.version, "updated": stamp}
            event("returned", trained_at=p.trained_at)
        else:
            state = {"lineage": p.lineage, "version": p.version, "state": SHADOW, "checkpoints": 0, "since": stamp,
                     "looks": [], "updated": stamp}
            event("lineage", trained_at=p.trained_at)
        if state.get("state") == SUSPENDED:
            state["state"] = state.pop("before", SHADOW)
    elif state.get("version") != p.version:
        state["version"] = p.version
        if state.get("state") == SUSPENDED:                 # a retraining ends a drift suspension
            state["state"] = state.pop("before", SHADOW)
            event("resumed")
        event("retrained", trained_at=p.trained_at)

    state["past"] = past
    state["lineages_started"] = len(past) + 1              # published with the model (DEC-0019)
    obs = scored(rows, p.lineage)
    done = int(state.get("checkpoints", 0))
    due = len(obs) // step
    state.update(finished=len(obs), next_checkpoint=(done + 1) * step, updated=stamp)
    while done < due:
        done += 1
        if state["state"] == SHADOW and done <= most:
            sample = obs[:done * step]
            lower, (model_b, base_b) = spread_lower(sample, alpha), brier(sample)
            passed = lower is not None and lower > 0 and model_b < base_b
            look = {"checkpoint": done, "signals": len(sample), "spread": spread(sample), "lower": lower, "alpha": alpha,
                    "brier": round(model_b, 5), "brier_base": round(base_b, 5), "passed": passed, "t": stamp}
            state.setdefault("looks", []).append(look)
            if passed:
                state["state"] = ACTING
                event("promoted", **look)
            else:
                event("checkpoint", **look)
        elif state["state"] == ACTING:
            window = obs[max(0, done * step - int(pr["demotion_window_signals"])):done * step]
            gap = spread(window)
            look = {"checkpoint": done, "signals": len(window), "spread": gap, "t": stamp}
            if gap is None or gap <= 0:
                state["state"] = DEMOTED
                event("demoted", **look)
            else:
                event("held", **look)
    state["checkpoints"] = done
    state["next_checkpoint"] = (done + 1) * step

    # Drift is measured on signals the version in force scored, against that version's own training data: scores
    # of an earlier version are another model's. So a retraining ends a suspension, and the check starts again
    # once the new version has scored enough. Measured in shadow too, so the owner sees it; only a model that
    # acts can be suspended by it.
    mine = [o for o in obs if o.model == p.version]
    if len(mine) < step:
        state.pop("drift", None)
    else:
        d = drift(card(desk, p.version).get("drift") or {}, mine[-step:], float(pr["drift_psi"]), int(pr["drift_inputs"]))
        state["drift"] = d
        if d["drifted"] and state["state"] == ACTING:
            state["before"], state["state"] = ACTING, SUSPENDED
            event("suspended", **d)
    return state, events
