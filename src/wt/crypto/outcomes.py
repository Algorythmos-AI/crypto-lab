"""Every signal followed to its outcome (DEC-0016, 2): the label a model is tested on.

A `signal` row says what a sleeve's rule saw on a bar and the levels a trade at that moment has. This module
follows each one through the bars that came after it, with the sleeve's own exits and costs, whether or not it
was bought, and writes one `outcome` row for it into the desk's journal.

The outcome is worked out exactly as the training set's is (`wt.ml.dataset`): `wt.crypto.signals.outcome` on
bars aggregated from the second exchange's hourly history, stops and targets resolved on hourly bars. So a live
signal's label and a historical example's label are the same thing. It is not the book's own result for a bought
signal (that is the `exit` row): a signal is judged on its own, as the model's question asks.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import uuid
from collections.abc import Callable
from typing import Any

from wt.core import ledger
from wt.core.desk import Desk
from wt.crypto import rules, signals
from wt.crypto.data import Bar, aggregate, fill_grid
from wt.ops.locks import job_lock

HOUR, DAY = 3600, 86_400
CYCLE_JOB, LOCK_WAIT_S = "crypto", 600.0
MAX_AGE_D = 400                 # the longest holding period is 180 four-hour bars; a daily challenger's, 180 days


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


def append(desk: Desk, rec: dict[str, Any]) -> dict[str, Any]:
    """One row in the desk's journal, written while no bar cycle is appending to it."""
    rec = {"id": uuid.uuid4().hex, **rec}
    with job_lock(CYCLE_JOB, wait_s=LOCK_WAIT_S, poll_s=2.0) as free:
        if not free:
            raise RuntimeError("the crypto bar cycle held its lock for ten minutes")
        ledger.append(desk.journal, rec, fsync=True)
    return rec


def specs_of(cfg: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, rules.Spec]:
    """Every sleeve a signal can come from: the registered three and every challenger ever registered."""
    out = dict(rules.registered(cfg))
    for r in rows:
        if r.get("kind") == "challenger" and r.get("event") == "registered" and isinstance(r.get("dials"), dict):
            try:
                spec = rules.challenger(cfg, r["dials"])
            except (KeyError, TypeError, ValueError):
                continue
            out.setdefault(spec.name, spec)
    return out


def pending(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Signal rows with no outcome row yet, oldest first. A signal recorded twice is followed once."""
    done = {r.get("sid") for r in rows if r.get("kind") == "outcome"}
    seen: set[Any] = set()
    out = []
    for r in rows:
        if r.get("kind") == "signal" and r.get("sid") not in done and r.get("sid") not in seen:
            seen.add(r.get("sid"))
            out.append(r)
    return out


def follow(rec: dict[str, Any], spec: rules.Spec, grid: list[Bar], bars: list[Bar], costs: dict[str, Any]) -> dict[str, Any] | None:
    """The outcome of one signal, or None while it has not finished (or its bar is not in the history yet)."""
    c = spec.c
    tf_s = c.timeframe_min * 60
    try:
        bar, price, stop, atr = int(rec["bar"]), float(rec["price"]), float(rec["stop"]), float(rec["atr"])
        target = None if rec.get("target") is None else float(rec["target"])
    except (KeyError, TypeError, ValueError):
        return None
    times = [b.t for b in bars]
    i = bisect.bisect_left(times, bar)
    if i >= len(bars) or bars[i].t != bar:
        return None
    horizon = int(spec.p["time_stop_bars"]) + 2
    close = bar + tf_s
    fine_t = [b.t for b in grid]
    fine = grid[bisect.bisect_left(fine_t, close):bisect.bisect_right(fine_t, close + horizon * tf_s)]
    return signals.outcome(spec.base, price, stop, target, atr, bar, bars[max(0, i + 1 - c.bars):i + 1 + horizon], fine,
                           c, spec.p, costs, HOUR)


def run(desk: Desk, cfg: dict[str, Any], now: float, rows: list[dict[str, Any]],
        load: Callable[[str, int, int], list[Bar]]) -> list[dict[str, Any]]:
    """Follow every pending signal as far as the history goes; append and return the outcomes found.
    `load(pair, start, end)` gives hourly bars."""
    todo = pending(rows)
    if not todo:
        return []
    specs = specs_of(cfg, rows)
    end = int(now) // HOUR * HOUR
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for rec in todo:
        if rec.get("sleeve") in specs and isinstance(rec.get("bar"), int) and isinstance(rec.get("pair"), str):
            by_pair.setdefault(rec["pair"], []).append(rec)
    out: list[dict[str, Any]] = []
    for pair, recs in by_pair.items():
        # A signal too old to finish any more (its bar is not in the history, or its levels are wrong) must not
        # make every run read years of bars: nothing older than the window is asked for.
        recs = [r for r in recs if int(r["bar"]) >= end - MAX_AGE_D * DAY]
        if not recs:
            continue
        try:
            need = max((specs[r["sleeve"]].c.bars + 2) * specs[r["sleeve"]].c.timeframe_min * 60 for r in recs)
            hourly = load(pair, min(int(r["bar"]) for r in recs) - need, end)
            grid = fill_grid(sorted(hourly, key=lambda b: b.t), HOUR) if hourly else []
        except Exception as e:  # noqa: BLE001 — one pair's history must not stop the others: it is tried again tomorrow
            print(f"outcomes: {pair} left for the next run ({e.__class__.__name__})")
            continue
        if not grid:
            continue
        agg: dict[int, list[Bar]] = {}
        for rec in recs:
            spec = specs[rec["sleeve"]]
            tf_s = spec.c.timeframe_min * 60
            if tf_s not in agg:
                agg[tf_s] = aggregate(grid, HOUR, tf_s)
            res = follow(rec, spec, grid, agg[tf_s], cfg["costs"])
            if res is None:
                continue
            out.append(append(desk, {
                "kind": "outcome", "t": dt.datetime.fromtimestamp(now, dt.UTC).isoformat(timespec="seconds"),
                "sid": rec["sid"], "sleeve": rec["sleeve"], "pair": pair, "bar": rec["bar"], "taken": bool(rec.get("taken")),
                "exit_t": dt.datetime.fromtimestamp(int(res["exit_t"]), dt.UTC).isoformat(timespec="seconds"),
                "reason": res["reason"], "r": res["r"], "net_pct": res["net_pct"], "held_bars": res["held_bars"],
                "fine": "hourly"}))
    return out
