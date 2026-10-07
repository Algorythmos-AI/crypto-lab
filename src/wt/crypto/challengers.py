"""Challengers: new strategy ideas drawn, tested and admitted with no person in the loop (DEC-0016, 5 and 6).

    python -m wt.crypto.challengers            (the daily job `crypto-challengers`)

One run does, in this order:

  1. retire    a live challenger that meets a retirement rule opens nothing more
  2. finish    a challenger registered by a run that died before its backtest is backtested now
  3. draw      at most two in an ISO week: one beside the best current sleeve, one at random from the untested
               space, both seeded by the week
  4. register  the exact rules go into the journal BEFORE the backtest: a trial from that moment, pass or fail
  5. gate C1   DEC-0015's test through the desk's own code, on EXP-0016's span, with the deflated Sharpe taken
               at the family's count plus the challengers registered so far
  6. admit     one that passes trades its own paper book from the next bar cycle; one that fails never trades

The record is the desk's hash-chained journal: rows of kind `challenger` that name the challenger as their
`sleeve`, so the baseline's figures never see them. `var/crypto/sleeves/challengers.json` is that record folded
into one file for the bar cycle and the snapshot; it is rebuilt from the journal whenever it is missing.

Where the charter is silent this module reads it narrowly:
  * "Differs from the best current sleeve in exactly one dial": the registered sleeves' own numbers are not all
    in the search space, and a challenger outside it is never automatic. So the neighbour is drawn from the
    members of the space with the fewest dials different from the best sleeve: exactly one when the best is
    itself in the space. If every such member is already a trial, no neighbour is drawn that week.
  * "The same history": the span of the experiment `learning.c1` names, not a window that moves with the date.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import random
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from wt.core import ledger
from wt.core.config import ROOT, load_yaml
from wt.core.desk import DESKS, Desk
from wt.crypto import risk, rules
from wt.crypto.book import write_atomic
from wt.crypto.data import Bar, PairInfo
from wt.ops.locks import job_lock

DAY = 86_400
CYCLE_JOB = "crypto"
LOCK_WAIT_S = 600.0                     # a bar cycle is killed at 300 s
CONTROLS, SEED = 100, 7                 # the random-entry control, as EXP-0016 ran it
LIVE, RETIRED = "live", "retired"
OFF_CODE, RETIRED_CODE = "learning_off", "retired"


def state_path(desk: Desk) -> Path:
    return desk.state_dir / "sleeves" / "challengers.json"


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


def _epoch(t: Any) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(t)).timestamp()
    except ValueError:
        return None


def week_of(t: float) -> str:
    y, w, _ = dt.datetime.fromtimestamp(t, dt.UTC).isocalendar()
    return f"{y}-W{w:02d}"


# ---- the search space ----

def space(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every challenger the charter allows, by id. Settings that are the same strategy are one member."""
    grid = cfg["learning"]["challengers"]["dials"]
    out: dict[str, dict[str, Any]] = {}
    for combo in itertools.product(*(grid[k] for k in rules.DIALS)):
        d = rules.canonical(dict(zip(rules.DIALS, combo, strict=True)))
        out.setdefault(rules.challenger_id(d), d)
    return out


def sleeve_dials(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    """A registered sleeve written as dials, with its own numbers (which need not be in the search space)."""
    sc = cfg["sleeves"]
    c, p = sc["common"], sc[name]
    return rules.canonical({"base": name, "timeframe_min": c["timeframe_min"], "high_bars": p.get("high_bars"),
                            "stop_atr": c["stop_atr"], "target_atr": p.get("target_atr"), "trail_atr": p.get("trail_atr"),
                            "min_stop_pct": c["min_stop_pct"], "btc_filter": False, "volume_filter": False,
                            "skip_held": False})


def distance(a: dict[str, Any], b: dict[str, Any]) -> int:
    """How many dials two strategies differ in. A dial that does nothing for either of them is not a difference;
    "no target" is a setting of its own."""
    return sum(1 for k in rules.DIALS
               if (k == "target_atr" or (a.get(k) is not None and b.get(k) is not None)) and a.get(k) != b.get(k))


def describe(d: dict[str, Any]) -> str:
    """A challenger's rules in one line, for the journal and the dashboard."""
    bars = "daily" if int(d["timeframe_min"]) == 1440 else f"{int(d['timeframe_min']) // 60}-hour"
    parts = [f"{d['base']} rule on {bars} bars"]
    if d.get("high_bars") is not None:
        parts.append(f"{d['high_bars']}-bar high")
    parts.append(f"stop {d['stop_atr']:g} ATR (at least {d['min_stop_pct']:g}%)")
    parts.append(f"target {d['target_atr']:g} ATR" if d.get("target_atr") is not None else f"trailed {d['trail_atr']:g} ATR")
    parts += [text for key, text in (("btc_filter", "only while Bitcoin trends up"), ("volume_filter", "volume above average"),
                                     ("skip_held", "skips a pair another sleeve holds")) if d.get(key)]
    return ", ".join(parts)


# ---- the record ----

def read_journal(desk: Desk) -> list[dict[str, Any]]:
    try:
        lines = desk.journal.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def state_of(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The challengers' rows folded into one record each, in the order they were registered."""
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("kind") != "challenger":
            continue
        cid, event = str(r.get("sleeve")), r.get("event")
        if event == "registered":
            out.setdefault(cid, {"id": cid, "dials": r.get("dials"), "rules": r.get("rules"), "slot": r.get("slot"),
                                 "of": r.get("of"), "week": r.get("week"), "registered": r.get("t"),
                                 "n_trials": r.get("n_trials"), "status": "registered"})
        elif cid not in out:
            continue                                        # a result with no registration is not a challenger
        elif event == "c1" and "c1" not in out[cid]:
            out[cid]["c1"] = {k: r.get(k) for k in ("passed", "failed_on", "base", "stressed", "control_p",
                                                    "control_mean_r", "span", "data_hash", "n_trials", "t")}
            out[cid]["status"] = "passed" if r.get("passed") else "failed"
        elif event == "admitted" and out[cid]["status"] == "passed":
            out[cid].update(status=LIVE, admitted=r.get("t"))
        elif event == "retired" and out[cid]["status"] == LIVE:
            out[cid].update(status=RETIRED, retired={"t": r.get("t"), "why": r.get("why"), "figures": r.get("figures")})
    return out


def save_state(desk: Desk, state: dict[str, dict[str, Any]], now: float) -> None:
    path = state_path(desk)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps({"updated": _iso(now), "challengers": state}, sort_keys=True))


def load_state(desk: Desk) -> dict[str, dict[str, Any]]:
    """The folded record. From the journal when the file is missing or unreadable: a challenger holding a
    position must never drop out of the bar cycle because a cache was lost."""
    try:
        got = json.loads(state_path(desk).read_text())["challengers"]
        if isinstance(got, dict):
            return got
    except (OSError, ValueError, KeyError):
        pass
    state = state_of(read_journal(desk))
    if state:
        save_state(desk, state, dt.datetime.now(dt.UTC).timestamp())
    return state


def note(desk: Desk, cid: str, event: str, now: float, **fields: Any) -> dict[str, Any]:
    """One `challenger` row in the journal, written while no bar cycle is appending to it."""
    rec = {"id": uuid.uuid4().hex, "kind": "challenger", "event": event, "t": _iso(now), "sleeve": cid, **fields}
    with job_lock(CYCLE_JOB, wait_s=LOCK_WAIT_S, poll_s=2.0) as free:
        if not free:
            raise RuntimeError("the crypto bar cycle held its lock for ten minutes")
        ledger.append(desk.journal, rec, fsync=True)
    return rec


# ---- what the bar cycle runs ----

def learning_off(desk: Desk) -> bool:
    return risk.learning_file(desk).exists()


def active(desk: Desk, cfg: dict[str, Any]) -> tuple[dict[str, rules.Spec], dict[str, str]]:
    """The challengers the bar cycle runs, and the ones among them that may not open a trade (with the reason).
    A live challenger runs; a retired one runs only while it still holds a position, for its exits. The owner's
    switch stops every challenger's entries and no exit."""
    from wt.crypto.book import Book
    specs: dict[str, rules.Spec] = {}
    off: dict[str, str] = {}
    switched_off = learning_off(desk)
    start = Decimal(str(cfg["sleeves"]["common"]["start_equity"]))
    for cid, rec in load_state(desk).items():
        status = rec.get("status")
        if status not in (LIVE, RETIRED):
            continue
        if status == RETIRED and not Book.load(risk.sleeve_dir(desk, cid) / "book.json", start).positions:
            continue
        spec = rules.challenger(cfg, rec["dials"])
        if spec.name != cid:
            raise ValueError(f"challenger {cid}: its recorded rules no longer give its id")
        specs[cid] = spec
        if status == RETIRED:
            off[cid] = RETIRED_CODE
        elif switched_off:
            off[cid] = OFF_CODE
    return specs, off


# ---- which sleeve is best, and what to draw ----

def experiment(cfg: dict[str, Any]) -> dict[str, Any]:
    """The result of the registered sleeves' own gate C1: its span is the challengers' history too."""
    name = str(cfg["learning"]["c1"]["experiment"])
    found = sorted((ROOT / "research" / "experiments").glob(f"{name}-*/result.json"))
    if not found:
        raise FileNotFoundError(f"no result.json for {name}")
    got: dict[str, Any] = json.loads(found[0].read_text())
    return got


def live_r(rows: list[dict[str, Any]], sleeve: str) -> list[float]:
    from wt.crypto.snapshot import exit_r
    fixed = exit_r([r for r in rows if r.get("sleeve") == sleeve])
    return [fixed[str(r.get("id"))] for r in rows
            if r.get("kind") == "exit" and r.get("sleeve") == sleeve and str(r.get("id")) in fixed]


def best(cfg: dict[str, Any], state: dict[str, dict[str, Any]], rows: list[dict[str, Any]],
         exp: dict[str, Any]) -> tuple[str, dict[str, Any], float]:
    """The current sleeve with the highest mean R over its backtest and live trades together: the registered
    three and the live challengers. Returns (name, its dials, that mean)."""
    tested: dict[str, tuple[int, float, dict[str, Any]]] = {}
    for s in exp.get("sleeves", []):
        if s["name"] in cfg["sleeves"] and s["base"].get("mean_r") is not None:
            tested[s["name"]] = (int(s["base"]["trades"]), float(s["base"]["mean_r"]), sleeve_dials(cfg, s["name"]))
    for cid, rec in state.items():
        base = (rec.get("c1") or {}).get("base") or {}
        if rec.get("status") == LIVE and base.get("mean_r") is not None:
            tested[cid] = (int(base["trades"]), float(base["mean_r"]), rules.canonical(rec["dials"]))
    scored = []
    for name, (n, mean, dials) in tested.items():
        live = live_r(rows, name)
        if n + len(live):
            scored.append(((n * mean + sum(live)) / (n + len(live)), name, dials))
    if not scored:
        raise ValueError("no current sleeve has a trade to be judged by")
    mean, name, dials = max(scored, key=lambda x: (x[0], x[1]))
    return name, dials, mean


def draw(cfg: dict[str, Any], state: dict[str, dict[str, Any]], best_name: str, best_dials: dict[str, Any],
         week: str) -> list[dict[str, Any]]:
    """This week's challengers still to register: the neighbour first, then the random one. Empty at a cap. The
    same record and the same week always give the same draw."""
    ch = cfg["learning"]["challengers"]
    live = sum(1 for r in state.values() if r.get("status") in (LIVE, "passed", "registered"))
    room = min(int(ch["per_week"]) - sum(1 for r in state.values() if r.get("week") == week),
               int(ch["max_live"]) - live, int(ch["max_registered"]) - len(state))
    drawn = {r.get("slot") for r in state.values() if r.get("week") == week}
    members = space(cfg)
    taken = set(state) | {rules.challenger_id(sleeve_dials(cfg, n)) for n in rules.NAMES if n in cfg["sleeves"]}
    out: list[dict[str, Any]] = []

    def seeded(slot: str) -> random.Random:
        return random.Random(int(hashlib.sha256(f"{week}:{slot}".encode()).hexdigest()[:16], 16))

    if room > len(out) and "neighbour" not in drawn:
        near = {cid: distance(d, best_dials) for cid, d in members.items()}
        closest = min((n for n in near.values() if n > 0), default=None)
        pool = sorted(cid for cid, n in near.items() if n == closest and cid not in taken)
        if pool:
            cid = seeded("neighbour").choice(pool)
            out.append({"id": cid, "dials": members[cid], "slot": "neighbour", "of": best_name})
            taken.add(cid)
    if room > len(out) and "random" not in drawn:
        pool = sorted(cid for cid in members if cid not in taken)
        if pool:
            cid = seeded("random").choice(pool)
            out.append({"id": cid, "dials": members[cid], "slot": "random", "of": None})
    return out


# ---- gate C1 ----

def gate(cfg: dict[str, Any], dials: dict[str, Any], n_trials: int, hourly: dict[str, list[Bar]],
         infos: dict[str, PairInfo], start: int, end: int, controls: int = CONTROLS, seed: int = SEED) -> dict[str, Any]:
    """DEC-0015's gate for one challenger, through the desk's own code. The figures and the verdict."""
    import numpy as np

    from wt.crypto import backtest
    spec = rules.challenger(cfg, dials)
    only = {spec.name: spec}
    equity = float(cfg["sleeves"]["common"]["start_equity"])
    rows = backtest.run(cfg, hourly, infos, start, end, specs=only)
    hard = backtest.run(cfg, hourly, infos, start, end, slip_mult=1.5, specs=only)
    base = backtest.summary(rows, spec.name, start, end, n_trials, equity)
    stressed = backtest.summary(hard, spec.name, start, end, n_trials, equity)
    p, means = backtest.control(cfg, hourly, infos, start, end, rows, spec.name, controls, seed, specs=only)
    verdict = backtest.verdict(base, stressed, p)
    keep = ("trades", "trades_per_month", "win_rate", "mean_r", "ci_low", "ci_high", "profit_factor", "dsr",
            "max_drawdown_pct", "return_pct", "total_r")
    return {"passed": bool(verdict["passed"]), "failed_on": verdict["failed_on"],
            "base": {k: base.get(k) for k in keep}, "stressed": {k: stressed.get(k) for k in ("mean_r", "ci_low", "ci_high")},
            "control_p": None if p is None else round(float(p), 4),
            "control_mean_r": round(float(np.mean(means)), 4) if means else None,
            "span": [start, end], "n_trials": n_trials, "controls": controls, "seed": seed}


def data_hash(hourly: dict[str, list[Bar]]) -> str:
    h = hashlib.sha256()
    for name in sorted(hourly):
        for b in hourly[name]:
            h.update(f"{name},{b.t},{b.o},{b.h},{b.l},{b.c},{b.v}\n".encode())
    return h.hexdigest()[:16]


def load_history(cfg: dict[str, Any], start: int, end: int) -> tuple[dict[str, list[Bar]], dict[str, PairInfo]]:
    """The traded pairs' hourly history over the span (fetched once, then read from the cache), and the venue's
    lot and tick sizes."""
    from wt.crypto import history
    from wt.crypto.data import CoinbasePublic, KrakenPublic
    pairs: dict[str, str] = dict(cfg["sleeves"]["common"]["pairs"])
    client = CoinbasePublic()
    hourly = {n: history.load_hourly(n, start - history.WARMUP_D * DAY, end, client) for n in pairs}
    return hourly, KrakenPublic().pair_infos(list(pairs.values()))


# ---- retirement ----

def retire_reason(rec: dict[str, Any], rows: list[dict[str, Any]], equity_now: float, now: float,
                  rule: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Why a live challenger retires now, with the figures, or None. DEC-0016, 5: 30 closed trades with the
    upper bound of the 95% interval for mean R below zero; a drawdown beyond 10% of its book; or 60 days with
    fewer than 5 trades."""
    import numpy as np

    from wt.backtest import stats
    cid = str(rec["id"])
    exits = [r for r in rows if r.get("kind") == "exit" and r.get("sleeve") == cid and isinstance(r.get("r"), int | float)]
    if len(exits) >= int(rule["min_trades"]):
        s = stats.summarize(np.array([float(r["r"]) for r in exits]), block="day", days=[str(r.get("t", ""))[:10] for r in exits])
        if s["ci95_expectancy"][1] < 0:
            return "mean_r_below_zero", {"trades": len(exits), "mean_r": round(s["expectancy_R"], 4),
                                         "ci_high": round(s["ci95_expectancy"][1], 4)}
    curve = [float(r["equity"]) for r in rows if r.get("kind") == "sleeve" and r.get("sleeve") == cid and r.get("equity")]
    peak, worst = 0.0, 0.0
    for e in [*curve, equity_now]:
        peak = max(peak, e)
        worst = min(worst, (e - peak) / peak * 100 if peak > 0 else 0.0)
    if worst < -float(rule["max_drawdown_pct"]):
        return "drawdown", {"max_drawdown_pct": round(worst, 2)}
    admitted = _epoch(rec.get("admitted"))
    idle_s = int(rule["idle_days"]) * DAY
    if admitted is not None and now - admitted >= idle_s:
        recent = sum(1 for r in exits if (t := _epoch(r.get("t"))) is not None and t >= now - idle_s)
        if recent < int(rule["idle_min_trades"]):
            return "idle", {"trades_in_window": recent, "days": int(rule["idle_days"])}
    return None


def equity_of(desk: Desk, cfg: dict[str, Any], cid: str) -> float:
    """The challenger's book valued at the last bids the bar cycle read."""
    from wt.crypto.book import Book
    try:
        marks = json.loads((desk.state_dir / "sleeves" / "data.json").read_text()).get("marks") or {}
    except (OSError, ValueError):
        marks = {}
    book = Book.load(risk.sleeve_dir(desk, cid) / "book.json", Decimal(str(cfg["sleeves"]["common"]["start_equity"])))
    return float(book.equity({k: float(v[0]) for k, v in marks.items() if isinstance(v, list) and len(v) == 2}))


# ---- the daily run ----

def run(now: float | None = None, desk: Desk | None = None, cfg: dict[str, Any] | None = None,
        market: tuple[dict[str, list[Bar]], dict[str, PairInfo]] | None = None, alerts: Any = None,
        controls: int = CONTROLS) -> int:
    from wt.ops.alerts import Alerts
    from wt.research.trials import family_trial_count
    now = dt.datetime.now(dt.UTC).timestamp() if now is None else now
    desk, cfg = desk or DESKS["crypto"], cfg or load_yaml("crypto.yaml")
    alerts = alerts or Alerts()
    ch = cfg["learning"]["challengers"]
    common = cfg["sleeves"]["common"]
    if (float(ch["start_equity"]), str(ch["limits"])) != (float(common["start_equity"]), str(common["limits"])):
        print("Refusing: a challenger's book and limits must be the tournament's own (the bar cycle uses those)")
        return 2
    if desk.chain_flag.exists():
        print("Refusing: the crypto journal's hash chain is flagged broken")
        return 2
    rows = read_journal(desk)
    state = state_of(rows)

    # 1. Retirement: only ever stops a challenger opening trades, so it runs whatever the owner's switch says.
    for cid, rec in state.items():
        if rec["status"] != LIVE:
            continue
        why = retire_reason(rec, rows, equity_of(desk, cfg, cid), now, ch["retire"])
        if why is not None:
            note(desk, cid, "retired", now, why=why[0], figures=why[1])
            rec.update(status=RETIRED, retired={"t": _iso(now), "why": why[0], "figures": why[1]})
            alerts.once_per_day(f"crypto:challenger-retired:{cid}", f"Crypto: challenger {cid} retired ({why[0]})",
                        "It opens no more trades. An open position is still managed to its exit.", 3)
            print(f"retired {cid}: {why[0]} {why[1]}")
    save_state(desk, state, now)

    if learning_off(desk):
        print("learning is switched off: nothing drawn, tested or admitted")
        return 0

    exp = experiment(cfg)
    start, end = int(exp["start"]), int(exp["end"])
    family = family_trial_count("C")
    todo = [cid for cid, rec in state.items() if rec["status"] == "registered"]

    # 3 and 4. Draw and register. Every row is in the journal before any backtest of this run starts.
    best_name, best_dials, best_mean = best(cfg, state, rows, exp)
    for pick in draw(cfg, state, best_name, best_dials, week_of(now)):
        n_trials = family + len(state) + 1
        fields = {"dials": pick["dials"], "rules": describe(pick["dials"]), "slot": pick["slot"], "of": pick["of"],
                  "week": week_of(now), "n_trials": n_trials}
        note(desk, pick["id"], "registered", now, **fields)
        state[pick["id"]] = {"id": pick["id"], **{k: fields[k] for k in ("dials", "rules", "slot", "of", "week", "n_trials")},
                             "registered": _iso(now), "status": "registered"}
        todo.append(pick["id"])
        print(f"registered {pick['id']} ({pick['slot']}): {fields['rules']}")
    save_state(desk, state, now)

    # 2 and 5. Gate C1 for everything registered and not yet judged.
    if todo:
        hourly, infos = market or load_history(cfg, start, end)
        digest = data_hash(hourly)
        if digest != exp.get("data_hash"):
            # Recorded with every verdict below. The venue serving a revised candle is not a reason to stop.
            print(f"note: the history's hash {digest} is not the registered sleeves' experiment's ({exp.get('data_hash')})")
        for cid in todo:
            rec = state[cid]
            res = {**gate(cfg, rec["dials"], int(rec["n_trials"]), hourly, infos, start, end, controls), "data_hash": digest}
            row = note(desk, cid, "c1", now, **res)
            rec["c1"] = {k: row.get(k) for k in ("passed", "failed_on", "base", "stressed", "control_p",
                                                 "control_mean_r", "span", "data_hash", "n_trials", "t")}
            rec["status"] = "passed" if res["passed"] else "failed"
            b = res["base"]
            print(f"gate C1 {cid}: {'PASSED' if res['passed'] else 'failed on ' + ', '.join(res['failed_on'])} "
                  f"({b.get('trades')} trades, mean R {b.get('mean_r')})")
            save_state(desk, state, now)

    # 6. Admission, up to the cap.
    for cid, rec in state.items():
        if rec["status"] != "passed":
            continue
        if sum(1 for r in state.values() if r["status"] == LIVE) >= int(ch["max_live"]):
            break
        note(desk, cid, "admitted", now, start_equity=float(ch["start_equity"]), limits=str(ch["limits"]))
        rec.update(status=LIVE, admitted=_iso(now))
        alerts.once_per_day(f"crypto:challenger-admitted:{cid}", f"Crypto: challenger {cid} passed its backtest and joins the tournament",
                    f"{rec.get('rules')}. Paper only, its own US${float(ch['start_equity']):,.0f} book, incubation.", 3)
        print(f"admitted {cid}")
    save_state(desk, state, now)
    counts = {s: sum(1 for r in state.values() if r["status"] == s) for s in ("registered", "failed", "passed", LIVE, RETIRED)}
    print(f"challengers: {len(state)} registered in all {counts}; best current sleeve {best_name} ({best_mean:+.3f}R)")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m wt.crypto.challengers")
    ap.add_argument("--status", action="store_true", help="print the record and change nothing")
    a = ap.parse_args(argv)
    if a.status:
        desk = DESKS["crypto"]
        print("learning is switched OFF" if learning_off(desk) else "learning is on")
        for cid, rec in state_of(read_journal(desk)).items():
            base = (rec.get("c1") or {}).get("base") or {}
            print(f"{cid}  {rec['status']:<10} {rec.get('week')}  {rec.get('slot'):<9} trades {base.get('trades')} "
                  f"mean R {base.get('mean_r')}  {rec.get('rules')}")
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())
