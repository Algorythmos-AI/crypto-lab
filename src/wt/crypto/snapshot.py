"""The crypto desk's published snapshot (ADR 0005): built from the desk's own state, sanitized by the same
allowlist machinery as the stocks snapshot, and sent to the same ingest endpoint, which files it by its `schema`.

    python -m wt.crypto.snapshot              build, validate, send
    python -m wt.crypto.snapshot --dry-run    build and validate only (writes var/dashboard-crypto/outbox)
    python -m wt.crypto.snapshot --verify     also read /api/health back

Everything published is a number, a code, a date or a pair name: there is no free text in this snapshot.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import statistics
import sys
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from wt.core.config import ROOT, STATE_DIR, load_yaml
from wt.core.desk import DESKS, Desk, desk_of_alert
from wt.crypto import cycle, risk
from wt.crypto.book import Book
from wt.ops import publish, safeio
from wt.ops.publish import B, I, Map, N, S

SCHEMA_ID = DESKS["crypto"].snapshot_schema
SCHEMA_VERSION = 1
CYCLE_JOB, CYCLE_WAIT_S = "crypto", 90.0   # the bar cycle's job lock, and how long a publish waits for it
SCHEMA_PATH = ROOT / "dashboard" / "src" / "lib" / "crypto.schema.json"
OUT = STATE_DIR / "dashboard-crypto"
WINDOW_AHEAD_D = 14                     # the off-host watchdog pages while `now` is inside the published window
OBS_DAYS = 7
CURVE_DAYS = 30

POSITION = {"pair": S, "qty": N, "entry_price": N, "stop": N, "target": N, "entry_time": S, "mark": N,
            "unrealised_pct": N}
ALLOW: dict[str, Any] = {
    "schema": S, "schema_version": I, "run_id": S, "as_of": S, "redaction": S, "withheld": I, "truncated": I,
    "expected_windows": [{"session": S, "start": S, "end": S}],
    "config": {"hash": S, "venue": S, "quote_currency": S, "timeframe_min": I, "pairs": [S]},
    "strategy": {"name": S, "rsi_period": I, "rsi_below": N, "ema_period": I, "stop_loss_pct": N,
                 "take_profit_pct": N, "time_stop_bars": I, "taker_fee_pct": N, "slippage_bps": N,
                 "net_win_pct": N, "net_loss_pct": N, "breakeven_win_rate": N},
    "market": [{"pair": S, "bar": S, "close": N, "rsi": N, "vwap_distance_pct": N, "ema8_distance_pct": N,
                "atr_pct": N, "volume_ratio": N, "would_fire": B, "why_not": [S], "tradable": B, "quality": [S],
                "spread_pct": N}],
    "quality": {"days": I, "min_days": I, "min_traded_share": N, "max_median_spread_pct": N,
                "pairs": [{"pair": S, "bars": I, "traded_share": N, "median_spread_pct": N, "p95_spread_pct": N,
                           "passes": B}]},
    "book": {"equity": N, "cash": N, "exposure": N, "start_equity": N, "return_pct": N, "positions": [POSITION]},
    "risk": {"limits": {"max_notional": N, "max_open_exposure": N, "max_entries_per_day": I,
                        "max_orders_per_day": I, "daily_loss_latch": N},
             "today": {"day": S, "entries": I, "orders": I, "realised": N},
             "latched": B, "chain_ok": B},
    "kill": {"on": B, "since": S},
    "perf": {"trades": I, "wins": I, "losses": I, "win_rate": N, "mean_r": N, "total_r": N, "total_pnl": N,
             "fees": N, "by_reason": Map(I), "by_pair": Map({"trades": I, "mean_r": N, "pnl": N}),
             "equity_curve": [{"t": S, "equity": N}],
             "recent": [{"t": S, "pair": S, "reason": S, "r": N, "pnl": N, "held_min": N}]},
    "activity": {"cycles_24h": I, "expected_24h": I, "failed_24h": I, "observations_7d": I, "fires_7d": I,
                 "entries_7d": I, "refused_7d": Map(I),
                 "daily": [{"day": S, "observations": I, "fires": I, "entries": I, "exits": I, "cycles": I}]},
    "distributions": Map([{"lo": N, "hi": N, "n": I}]),
    "ml": {"observations": I, "labelled": I, "model": S, "status": S},
    "jobs": {"last": Map({"status": S, "exit": I, "started": S, "ended": S, "sha": S}),
             "runs": [{"job": S, "status": S, "exit": I, "started": S, "ended": S, "sha": S}]},
    "alerts": {"firing": [{"key": S, "since": S}]},
    # ---- the tournament sleeves (DEC-0015): one strategy and one paper book each, never pooled ----
    "sleeves": [{"name": S, "strategy": S, "stage": S, "tf_min": I, "config": S, "equity": N, "start_equity": N,
                 "return_pct": N, "cash": N, "exposure": N, "open_pnl": N, "latched": B,
                 "pnl": {"today": N, "week": N, "month": N, "total": N, "today_trades": I, "week_trades": I,
                         "month_trades": I},
                 "trades": I, "wins": I, "losses": I, "win_rate": N, "mean_r": N, "total_r": N, "fees": N, "max_dd": N,
                 "positions": [{"pair": S, "qty": N, "entry_price": N, "entry_time": S, "stop": N, "target": N,
                                "mark": N, "mark_time": S, "unrealised": N, "unrealised_pct": N, "unrealised_r": N,
                                "risk": N}],
                 "recent": [{"pair": S, "entry_time": S, "exit_time": S, "entry_price": N, "exit_price": N, "qty": N,
                             "pnl": N, "r": N, "reason": S, "held_min": N}],
                 "signals": [{"t": S, "pair": S, "outcome": S, "why": [S]}],
                 "why_not": [{"pair": S, "bar": S, "fire": B, "why": [S]}],
                 "equity_curve": [{"t": S, "equity": N}]}],
    # ---- the challengers (DEC-0016, 5): every strategy idea tried, with the verdict of its backtest ----
    "challengers": {"learning": S, "week": S, "per_week": I, "max_live": I, "max_registered": I, "registered": I,
                    "live": I, "failed": I, "retired": I, "drawn_this_week": I,
                    "list": [{"id": S, "rules": S, "slot": S, "of": S, "week": S, "registered": S, "status": S,
                              "n_trials": I, "trades": I, "trades_per_month": N, "win_rate": N, "mean_r": N,
                              "ci_low": N, "ci_high": N, "profit_factor": N, "dsr": N, "control_p": N,
                              "max_drawdown_pct": N, "failed_on": [S], "admitted": S, "retired": S,
                              "retired_why": S}]},
}
HISTOGRAMS = ("rsi", "atr_pct", "vwap_distance_pct", "volume_ratio")


def to_schema() -> dict[str, Any]:
    body = publish.to_schema(ALLOW, top=False)
    return {"$schema": "http://json-schema.org/draft-07/schema#", "$id": SCHEMA_ID,
            "title": "Trading Lab crypto desk snapshot (generated from wt.crypto.snapshot.ALLOW)",
            **body, "type": "object", "required": list(publish.REQUIRED)}


def _jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


def observations(desk: Desk, now: dt.datetime, days: int | None = OBS_DAYS) -> list[dict[str, Any]]:
    """Observations, oldest first, one per (pair, bar): a line repeated after a crash is dropped."""
    files = sorted((desk.state_dir / "observations").glob("obs-*.jsonl"))
    if days is not None:
        first = (now - dt.timedelta(days=days)).date().isoformat()
        files = [f for f in files if f.stem[4:] >= first]
    seen: dict[tuple[str, int], dict[str, Any]] = {}
    for f in files:
        for r in _jsonl(f):
            if isinstance(r.get("pair"), str) and isinstance(r.get("t"), int):
                seen[(r["pair"], r["t"])] = r
    return sorted(seen.values(), key=lambda r: (r["t"], r["pair"]))


def histogram(values: list[float], bins: int = 12) -> list[dict[str, Any]]:
    if len(values) < 2:
        return []
    v = sorted(values)
    lo, hi = v[int(len(v) * 0.01)], v[min(len(v) - 1, int(len(v) * 0.99))]       # trim the tails
    if hi <= lo:
        return [{"lo": lo, "hi": hi, "n": len(v)}]
    width = (hi - lo) / bins
    counts = Counter(min(bins - 1, max(0, int((x - lo) / width))) for x in v)
    return [{"lo": round(lo + i * width, 4), "hi": round(lo + (i + 1) * width, 4), "n": counts.get(i, 0)}
            for i in range(bins)]


def quality_gate(obs: list[dict[str, Any]], pairs: list[str], gate: dict[str, Any]) -> dict[str, Any]:
    """Gate C0 (DEC-0012) as it stands on everything recorded so far."""
    days = len({dt.datetime.fromtimestamp(o["t"], dt.UTC).date() for o in obs})
    rows = []
    for pair in pairs:
        mine = [o for o in obs if o["pair"] == pair]
        spreads = sorted(s for o in mine if (s := _f(o.get("spread_pct"))) is not None)
        traded = sum("bar_untraded" not in (o.get("quality") or []) for o in mine) / len(mine) if mine else None
        med = statistics.median(spreads) if spreads else None
        rows.append({"pair": pair, "bars": len(mine), "traded_share": traded, "median_spread_pct": med,
                     "p95_spread_pct": spreads[min(len(spreads) - 1, int(len(spreads) * 0.95))] if spreads else None,
                     "passes": bool(mine) and days >= int(gate["min_days"]) and traded is not None
                               and traded >= float(gate["min_traded_share"]) and med is not None
                               and med <= float(gate["max_median_spread_pct"])})
    return {"days": days, "min_days": int(gate["min_days"]), "min_traded_share": float(gate["min_traded_share"]),
            "max_median_spread_pct": float(gate["max_median_spread_pct"]), "pairs": rows}


def economics(s: dict[str, Any], c: dict[str, Any]) -> dict[str, float]:
    """What a full target and a full stop are worth after two taker fees and slippage both ways."""
    cost = 2 * float(c["taker_fee_pct"]) + 2 * float(c["slippage_bps"]) / 100
    win, loss = float(s["take_profit_pct"]) - cost, -float(s["stop_loss_pct"]) - cost
    return {"net_win_pct": round(win, 4), "net_loss_pct": round(loss, 4),
            "breakeven_win_rate": round(-loss / (win - loss), 4) if win > 0 else 1.0}


def performance(journal: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    exits = [r for r in journal if r.get("kind") == "exit"]
    rs = [r["r"] for r in exits if isinstance(r.get("r"), int | float)]
    pnl = [p for r in exits if (p := _f(r.get("pnl"))) is not None]
    by_pair: dict[str, dict[str, Any]] = {}
    for r in exits:
        d = by_pair.setdefault(str(r.get("pair")), {"trades": 0, "rs": [], "pnl": 0.0})
        d["trades"] += 1
        d["pnl"] += _f(r.get("pnl")) or 0.0
        if isinstance(r.get("r"), int | float):
            d["rs"].append(r["r"])
    cutoff = (now - dt.timedelta(days=CURVE_DAYS)).isoformat()
    hourly: dict[str, float] = {}
    for r in journal:
        if r.get("kind") == "cycle" and str(r.get("t", "")) >= cutoff and (e := _f(r.get("equity"))) is not None:
            hourly[str(r["t"])[:13]] = e                           # the last cycle of each hour
    wins = sum(x > 0 for x in pnl)
    return {"trades": len(exits), "wins": wins, "losses": len(pnl) - wins,
            "win_rate": wins / len(pnl) if pnl else None, "mean_r": statistics.fmean(rs) if rs else None,
            "total_r": sum(rs) if rs else None, "total_pnl": sum(pnl) if pnl else None,
            "fees": sum(f for r in exits if (f := _f(r.get("fees"))) is not None) if exits else None,
            "by_reason": dict(Counter(str(r.get("reason")) for r in exits)),
            "by_pair": {k: {"trades": d["trades"], "mean_r": statistics.fmean(d["rs"]) if d["rs"] else None,
                            "pnl": d["pnl"]} for k, d in by_pair.items()},
            "equity_curve": [{"t": k + ":00:00+00:00", "equity": v} for k, v in sorted(hourly.items())],
            "recent": [{"t": r.get("t"), "pair": r.get("pair"), "reason": r.get("reason"), "r": r.get("r"),
                        "pnl": _f(r.get("pnl")), "held_min": round(r["held_s"] / 60, 1) if "held_s" in r else None}
                       for r in exits[-20:]][::-1]}


def activity(journal: list[dict[str, Any]], obs: list[dict[str, Any]], now: dt.datetime, tf: int) -> dict[str, Any]:
    day_ago, week_ago = (now - dt.timedelta(days=1)).isoformat(), (now - dt.timedelta(days=7)).isoformat()
    cycles = [r for r in journal if r.get("kind") == "cycle"]
    recent = [r for r in journal if str(r.get("t", "")) >= week_ago]
    daily: dict[str, Counter[str]] = {}
    for o in obs:
        d = daily.setdefault(dt.datetime.fromtimestamp(o["t"], dt.UTC).date().isoformat(), Counter())
        d["observations"] += 1
        d["fires"] += bool(o.get("would_fire"))
    for r in recent:
        kind = {"entry": "entries", "exit": "exits", "cycle": "cycles"}.get(str(r.get("kind")))
        if kind:
            daily.setdefault(str(r.get("t", ""))[:10], Counter())[kind] += 1
    # A desk younger than a day is held to the cycles it could have run, not to a full day's worth.
    full = 24 * 60 // tf
    try:
        first = dt.datetime.fromisoformat(str(cycles[0]["t"])) if cycles else now
        expected = max(1, min(full, int((now - first).total_seconds() // (tf * 60)) + 1)) if cycles else 0
    except (ValueError, TypeError, KeyError):
        expected = full
    return {"cycles_24h": sum(str(r.get("t", "")) >= day_ago and not r.get("failed") for r in cycles),
            "expected_24h": expected,
            "failed_24h": sum(str(r.get("t", "")) >= day_ago and bool(r.get("failed")) for r in cycles),
            "observations_7d": len(obs), "fires_7d": sum(bool(o.get("would_fire")) for o in obs),
            "entries_7d": sum(r.get("kind") == "entry" for r in recent),
            "refused_7d": dict(Counter(w for r in recent if r.get("kind") == "refused" for w in r.get("why") or [])),
            "daily": [{"day": k, **{f: v.get(f, 0) for f in ("observations", "fires", "entries", "exits", "cycles")}}
                      for k, v in sorted(daily.items())[-14:]]}


def exit_r(rows: list[dict[str, Any]]) -> dict[str, float]:
    """R of every sleeve exit, by row id, measured against the risk its entry row recorded.

    An exit booked before the book kept that risk divided by the stop as it stood at the exit, which overstates a
    trailed-stop trade. The journal is never rewritten; the entry row has the risk, so R is worked out from it.
    An exit with no matching entry keeps the R it was booked with."""
    risk = {(r.get("sleeve"), r.get("pair"), r.get("t")): v for r in rows
            if r.get("kind") == "entry" and r.get("sleeve") and (v := _f(r.get("risk")))}
    out: dict[str, float] = {}
    for r in rows:
        if r.get("kind") != "exit" or not r.get("sleeve"):
            continue
        unit, pnl = risk.get((r.get("sleeve"), r.get("pair"), r.get("entry_t"))), _f(r.get("pnl"))
        if r.get("risk0") is not None and isinstance(r.get("r"), int | float):
            out[str(r.get("id"))] = float(r["r"])               # booked against the entry risk already
        elif unit and pnl is not None:
            out[str(r.get("id"))] = round(pnl / unit, 3)
        elif isinstance(r.get("r"), int | float):
            out[str(r.get("id"))] = float(r["r"])
    return out


def sleeves_view(desk: Desk, cfg: dict[str, Any], rows: list[dict[str, Any]], now: dt.datetime) -> list[dict[str, Any]]:
    """One entry per tournament sleeve, from its own book and its own rows of the journal. Open positions are
    valued at the last bid the cycle read (the snapshot never calls the venue). Nothing is summed across sleeves."""
    from wt.crypto import rules
    from wt.crypto import sleeves as engine
    sc = cfg.get("sleeves") or {}
    if not sc:
        return []
    try:
        state = json.loads((desk.state_dir / "sleeves" / "data.json").read_text())
    except (OSError, ValueError):
        state = {}
    marks = {k: v for k, v in (state.get("marks") or {}).items() if isinstance(v, list) and len(v) == 2}
    today = now.date()
    week, month = (today - dt.timedelta(days=today.weekday())).isoformat(), today.replace(day=1).isoformat()
    cutoff = (now - dt.timedelta(days=CURVE_DAYS)).isoformat()
    fixed = exit_r(rows)
    # The registered sleeves, then the challengers the bar cycle runs (DEC-0016, 5): each passed gate C1 before
    # its first trade. (name, what it trades, stage, bar length, config)
    shown = [(n, str(sc[n].get("hypothesis")), engine.STAGE, int(sc["common"]["timeframe_min"]), engine.sleeve_hash(cfg, n))
             for n in rules.NAMES if n in sc]
    try:
        from wt.crypto import challengers
        record = challengers.state_of(rows)
        specs, off = challengers.active(desk, cfg, record)
        shown += [(cid, ("Retired challenger: " if off.get(cid) == challengers.RETIRED_CODE else "Challenger: ")
                   + challengers.describe(rules.canonical(record[cid]["dials"])),
                   "passed", s.c.timeframe_min, cid) for cid, s in specs.items()]
    except Exception as fault:  # noqa: BLE001 — the registered sleeves are shown whatever the challengers' record says
        print(f"challengers left out of the snapshot ({fault.__class__.__name__})", file=sys.stderr)
    out = []
    for name, strategy, stage, tf_min, config in shown:
        mine = [r for r in rows if r.get("sleeve") == name]
        folder = risk.sleeve_dir(desk, name)
        book = Book.load(folder / "book.json", Decimal(str(sc["common"]["start_equity"])))
        equity = float(book.equity({k: float(v[0]) for k, v in marks.items()}))
        exits = [r for r in mine if r.get("kind") == "exit"]
        pnl = [(str(r.get("t", ""))[:10], v) for r in exits if (v := _f(r.get("pnl"))) is not None]
        rs = [fixed[str(r.get("id"))] for r in exits if str(r.get("id")) in fixed]
        wins = sum(v > 0 for _, v in pnl)
        run = peak = dd = 0.0
        for _, v in pnl:
            run += v
            peak, dd = max(peak, run), min(dd, run - peak)
        positions: list[dict[str, Any]] = []
        for held in book.positions.values():
            mark = marks.get(held.pair)
            open_ = float(held.qty) * (float(mark[0]) - float(held.entry_price)) - float(held.entry_fee) if mark else None
            positions.append({
                "pair": held.pair, "qty": float(held.qty), "entry_price": float(held.entry_price), "entry_time": _iso(held.entry_t),
                "stop": float(held.stop), "target": float(held.target) if held.target.is_finite() else None,
                "mark": float(mark[0]) if mark else None, "mark_time": _iso(mark[1]) if mark else None,
                "unrealised": open_, "risk": float(held.unit),
                "unrealised_pct": (float(mark[0]) / float(held.entry_price) - 1) * 100 if mark else None,
                "unrealised_r": open_ / float(held.unit) if open_ is not None and held.unit > 0 else None})
        last_eval = next((r for r in reversed(mine) if r.get("kind") == "sleeve" and r.get("pairs")), None)
        curve = {str(r["t"])[:13]: e for r in mine if r.get("kind") == "sleeve" and str(r.get("t", "")) >= cutoff
                 and (e := _f(r.get("equity"))) is not None}
        curve[now.isoformat()[:13]] = equity
        opens: list[float] = [float(x["unrealised"]) for x in positions if x["unrealised"] is not None]

        def total(since: str, pnl: list[tuple[str, float]] = pnl) -> tuple[float, int]:
            got = [v for d, v in pnl if d >= since]
            return sum(got), len(got)
        out.append({
            "name": name, "strategy": strategy, "stage": stage, "tf_min": tf_min, "config": config,
            "equity": equity, "start_equity": float(book.start_equity),
            "return_pct": (equity / float(book.start_equity) - 1) * 100 if book.start_equity else None,
            "cash": float(book.cash), "exposure": float(book.exposure()), "open_pnl": sum(opens) if opens else None,
            "latched": (folder / "latch").exists(),
            "pnl": {"today": total(today.isoformat())[0], "week": total(week)[0], "month": total(month)[0],
                    "total": total("")[0], "today_trades": total(today.isoformat())[1], "week_trades": total(week)[1],
                    "month_trades": total(month)[1]},
            "trades": len(exits), "wins": wins, "losses": len(pnl) - wins,
            "win_rate": wins / len(pnl) if pnl else None, "mean_r": statistics.fmean(rs) if rs else None,
            "total_r": sum(rs) if rs else None,
            "fees": sum(f for r in exits if (f := _f(r.get("fees"))) is not None) if exits else None,
            "max_dd": dd if pnl else None, "positions": positions,
            "recent": [{"pair": r.get("pair"), "entry_time": r.get("entry_t"), "exit_time": r.get("t"),
                        "entry_price": _f(r.get("entry_price")), "exit_price": _f(r.get("exit_price")),
                        "qty": _f(r.get("qty")), "pnl": _f(r.get("pnl")), "r": fixed.get(str(r.get("id"))),
                        "reason": r.get("reason"),
                        "held_min": round(r["held_s"] / 60, 1) if "held_s" in r else None} for r in exits[-20:]][::-1],
            "signals": [{"t": r.get("t"), "pair": r.get("pair"),
                         "outcome": "entered" if r["kind"] == "entry" else "refused", "why": r.get("why") or []}
                        for r in mine if r.get("kind") in ("entry", "refused")][-12:][::-1],
            "why_not": [{"pair": pair, "bar": _iso(v["bar"]) if isinstance(v.get("bar"), int) else None,
                         "fire": bool(v.get("fire")), "why": v.get("why") or []}
                        for pair, v in ((last_eval or {}).get("pairs") or {}).items() if isinstance(v, dict)],
            "equity_curve": [{"t": k + ":00:00+00:00", "equity": v} for k, v in sorted(curve.items())]})
    return out


def challengers_view(desk: Desk, cfg: dict[str, Any], rows: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any] | None:
    """Every challenger registered, newest first, with the figures its gate C1 was judged on. The interval is the
    one at 1.5x slippage: that is the one the gate reads. None when the config has no challengers."""
    from wt.crypto import challengers
    ch = (cfg.get("learning") or {}).get("challengers")
    if not ch:
        return None
    state = challengers.state_of(rows)
    week = challengers.week_of(now.timestamp())
    out = []
    for rec in reversed(list(state.values())):
        c1 = rec.get("c1") or {}
        base, hard, gone = c1.get("base") or {}, c1.get("stressed") or {}, rec.get("retired") or {}
        out.append({"id": rec["id"], "rules": rec.get("rules"), "slot": rec.get("slot"), "of": rec.get("of"),
                    "week": rec.get("week"), "registered": rec.get("registered"), "status": rec.get("status"),
                    "n_trials": rec.get("n_trials"), "trades": base.get("trades"),
                    "trades_per_month": base.get("trades_per_month"), "win_rate": base.get("win_rate"),
                    "mean_r": base.get("mean_r"), "ci_low": hard.get("ci_low"), "ci_high": hard.get("ci_high"),
                    "profit_factor": base.get("profit_factor"), "dsr": base.get("dsr"), "control_p": c1.get("control_p"),
                    "max_drawdown_pct": base.get("max_drawdown_pct"), "failed_on": c1.get("failed_on") or [],
                    "admitted": rec.get("admitted"), "retired": gone.get("t"), "retired_why": gone.get("why")})

    def count(status: str) -> int:
        return sum(1 for r in state.values() if r.get("status") == status)
    return {"learning": "off" if risk.learning_file(desk).exists() else "on", "week": week,
            "per_week": int(ch["per_week"]), "max_live": int(ch["max_live"]), "max_registered": int(ch["max_registered"]),
            "registered": len(state), "live": count("live"), "failed": count("failed"), "retired": count("retired"),
            "drawn_this_week": sum(1 for r in state.values() if r.get("week") == week), "list": out}


PUBLISH_JOB = "dashboard-crypto"


def _last_jobs(last: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The last run of each job. The publish job is the one building this snapshot, so its own heartbeat says
    "running": show its previous, finished run instead (as the stocks publisher does), or nothing on the first."""
    prev = next((r for r in reversed(runs) if r.get("job") == PUBLISH_JOB), None)
    out = {k: v for k, v in last.items() if k != PUBLISH_JOB}
    if prev is not None:
        out[PUBLISH_JOB] = prev
    return out


def collect(now: dt.datetime, desk: Desk | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The raw (unsanitized) snapshot body from the desk's state on disk. Reads only; never calls the venue."""
    from wt.ops.alerts import Alerts
    from wt.ops.heartbeat import last_runs, recent_runs
    from wt.ops.schedule import JOBS
    desk, cfg = desk or DESKS["crypto"], cfg or load_yaml("crypto.yaml")
    pairs, tf = list(cfg["pairs"]), int(cfg["timeframe_min"])
    # Rows that name a sleeve belong to the tournament (DEC-0015): the keys below `sleeves` describe the baseline.
    every = _jsonl(desk.journal)
    journal = [r for r in every if not r.get("sleeve")]
    obs = observations(desk, now)
    book = Book.load(desk.state_dir / "book.json", Decimal(str(cfg["account"]["start_equity"])))
    limits = risk.load_limits(desk.strategy)
    latest = {o["pair"]: o for o in obs}
    marks = {p: o["close"] for p, o in latest.items() if isinstance(o.get("close"), int | float)}
    equity = float(book.equity(marks))
    day = now.date().isoformat()

    def mine(job: object) -> bool:
        return isinstance(job, str) and job in JOBS and JOBS[job].desk == desk.name
    runs = [r for r in recent_runs() if mine(r.get("job"))]
    kill = desk.kill_file
    return {
        "expected_windows": [{"session": "always", "start": (now - dt.timedelta(days=1)).isoformat(),
                              "end": (now + dt.timedelta(days=WINDOW_AHEAD_D)).isoformat()}],
        "config": {"hash": cycle.config_hash(cfg), "venue": cfg["venue"], "quote_currency": cfg["quote_currency"],
                   "timeframe_min": tf, "pairs": pairs},
        "strategy": {**cfg["strategy"], **cfg["costs"], **economics(cfg["strategy"], cfg["costs"])},
        "market": [{"pair": p, "bar": _iso(o["t"]), "close": o.get("close"), **{k: (o.get("features") or {}).get(k)
                    for k in ("rsi", "vwap_distance_pct", "ema8_distance_pct", "atr_pct", "volume_ratio")},
                    "would_fire": o.get("would_fire"), "why_not": o.get("why_not"), "tradable": o.get("tradable"),
                    "quality": o.get("quality"), "spread_pct": o.get("spread_pct")}
                   for p in pairs if (o := latest.get(p)) is not None],
        "quality": quality_gate(observations(desk, now, days=None), pairs, cfg["gates"]["c0"]),
        "book": {"equity": equity, "cash": float(book.cash), "exposure": float(book.exposure()),
                 "start_equity": float(book.start_equity),
                 "return_pct": (equity / float(book.start_equity) - 1) * 100 if book.start_equity else None,
                 "positions": [{"pair": p.pair, "qty": float(p.qty), "entry_price": float(p.entry_price),
                                "stop": float(p.stop), "target": float(p.target), "entry_time": _iso(p.entry_t),
                                "mark": marks.get(p.pair),
                                "unrealised_pct": (marks[p.pair] / float(p.entry_price) - 1) * 100
                                if p.pair in marks else None} for p in book.positions.values()]},
        "risk": {"limits": {"max_notional": float(limits.max_notional),
                            "max_open_exposure": float(limits.max_open_exposure),
                            "max_entries_per_day": limits.max_entries_per_day,
                            "max_orders_per_day": limits.max_orders_per_day,
                            "daily_loss_latch": float(limits.daily_loss_latch)},
                 "today": {"day": day, "entries": book.entries.get(day, 0), "orders": book.orders.get(day, 0),
                           "realised": float(book.realised.get(day, Decimal(0)))},
                 "latched": risk.latch_file(desk).exists(), "chain_ok": not desk.chain_flag.exists()},
        "kill": {"on": kill.exists(),
                 "since": dt.datetime.fromtimestamp(kill.stat().st_mtime, dt.UTC).isoformat() if kill.exists() else None},
        "perf": performance(journal, now),
        "activity": activity(journal, obs, now, tf),
        "distributions": {k: histogram([v for o in obs if (v := _f((o.get("features") or {}).get(k))) is not None])
                          for k in HISTOGRAMS},
        "ml": {"observations": len(obs), "labelled": 0, "model": None, "status": "not_trained"},
        "jobs": {"last": _last_jobs({k: v for k, v in last_runs().items() if mine(k)}, runs), "runs": runs[-200:]},
        "alerts": {"firing": [{"key": k, "since": v.get("since")} for k, v in sorted(Alerts().firing().items())
                              if desk_of_alert(k) == desk.name]},
        "sleeves": _guarded(lambda: sleeves_view(desk, cfg, every, now), []),
        "challengers": _guarded(lambda: challengers_view(desk, cfg, every, now), None),
    }


def _guarded(view: Any, empty: Any) -> Any:
    """The tournament's views must never stop the baseline's snapshot: a fault leaves the key empty, and says so."""
    try:
        return view()
    except Exception as e:  # noqa: BLE001
        print(f"tournament view failed ({e.__class__.__name__}); left out", file=sys.stderr)
        return empty


def build(raw: dict[str, Any], san: publish.Sanitizer, run_id: str, now: dt.datetime) -> dict[str, Any]:
    clean: dict[str, Any] = san.apply(ALLOW, raw)
    clean.update(schema=SCHEMA_ID, schema_version=SCHEMA_VERSION, run_id=run_id,
                 as_of=now.isoformat(timespec="seconds"), redaction="strict" if san.strict else "standard",
                 withheld=san.withheld, truncated=san.truncated)
    return clean


def validate(snapshot: dict[str, Any]) -> list[str]:
    import jsonschema
    v = jsonschema.Draft7Validator(to_schema())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in v.iter_errors(snapshot)][:20]


def verify_stored(snap: dict[str, Any], url: str, bypass: str | None) -> tuple[bool, str]:
    """/api/health reports each desk's stored snapshot under `desks`."""
    h, why = publish.read_health(url, bypass)
    if h is None:
        return False, why
    crypto = (h.get("desks") or {}).get("crypto") if isinstance(h.get("desks"), dict) else None
    got = crypto.get("run_id") if isinstance(crypto, dict) else None
    return got == snap["run_id"], f"health serves crypto run {got}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.crypto.snapshot")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args(argv)
    from wt.ops.locks import job_lock
    with job_lock("publish-crypto", wait_s=publish.LOCK_WAIT_S) as got:
        if not got:
            print("another crypto publish is still running; not starting a second one")
            return 0
        # A bar cycle that is still running has not saved its books yet: wait for it rather than publish half
        # of it. After the wait the snapshot goes out whatever happened; it only reads.
        with job_lock(CYCLE_JOB, wait_s=CYCLE_WAIT_S, poll_s=2.0):
            pass
        return _publish(a)


def _publish(a: argparse.Namespace) -> int:
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    deadline = time.monotonic() + publish.PUBLISH_BUDGET_S
    env_file = os.environ.get("WT_ENV_FILE")
    if env_file and not publish._nonempty_readable(Path(env_file)):
        print(f"WT_ENV_FILE is set but {env_file} is missing, unreadable or empty: not publishing", file=sys.stderr)
        return 2
    secrets = safeio.env_secret_values([Path(env_file)] if env_file else [ROOT / ".env"])
    san = publish.Sanitizer(safeio.Redactor(secrets | safeio.secret_env_values()), None, strict=False)
    run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + hashlib.sha1(os.urandom(8)).hexdigest()[:6]
    snap = build(collect(now), san, run_id, now)
    if problems := validate(snap):
        print("crypto snapshot failed schema validation:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    body = json.dumps(snap, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > publish.MAX_BODY:
        print(f"crypto snapshot is {len(body)} bytes, over the {publish.MAX_BODY} cap", file=sys.stderr)
        return 2
    (OUT / "outbox").mkdir(parents=True, exist_ok=True)
    safeio.atomic_write(OUT / "outbox" / "snapshot.json", body.decode(), OUT)
    print(f"crypto snapshot {run_id}: {len(body)} bytes")
    if a.dry_run:
        return 0
    url, secret = os.environ.get("DASHBOARD_INGEST_URL"), os.environ.get("DASHBOARD_INGEST_SECRET")
    if (why := publish.shadow_key_problem()) is not None:
        print(why, file=sys.stderr)
        return 2
    if not url or not secret:
        print("DASHBOARD_INGEST_URL / DASHBOARD_INGEST_SECRET not set: not sending")
        return 0
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET")
    ok, detail = publish.send(body, url, secret, bypass, tries=publish.SEND_TRIES, deadline=deadline)
    st = publish.record_outcome(ok, detail, now, OUT / "publish_state.json")
    print(f"sent: {ok} ({publish.error_code(detail) if not ok else detail}); "
          f"consecutive failures {st['consecutive_failures']}")
    if ok and a.verify:
        good, why = verify_stored(snap, url, bypass)
        print(f"verify: {'ok' if good else 'FAILED'}: {why}")
        return 0 if good else 1
    if not ok and st["consecutive_failures"] < publish.ALERT_AFTER_FAILURES:
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
