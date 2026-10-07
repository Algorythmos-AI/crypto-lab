"""EXP-0020 (DEC-0019): what the desk-wide limits do to the three registered sleeves on EXP-0016's history.

    python scripts/crypto_desk_limits.py --offline --out research/experiments/EXP-0020-crypto-desk-limits

Two runs through the desk's own code over the span and data of EXP-0016: each book alone (which must reproduce
EXP-0016's trades), and all three under `config/risk.yaml`, key CD. The limits are adopted by DEC-0019, not by
this result; the report says what they cost and what they bound.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from crypto_backtest import data_hash, pair_infos  # noqa: E402
from wt.backtest import stats  # noqa: E402
from wt.core.config import load_yaml  # noqa: E402
from wt.crypto import backtest, risk, rules  # noqa: E402
from wt.crypto.data import CoinbasePublic  # noqa: E402
from wt.crypto.history import WARMUP_D, load_hourly  # noqa: E402

DAY = 86_400
CODES = ("desk_coin", "desk_risk")


def _epoch(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def mean_r(rows: list[dict[str, Any]], sleeve: str | None = None) -> dict[str, Any]:
    """Trades, mean R and its 95% interval (bootstrap over the UTC days the trades closed on)."""
    tr = [r for r in rows if r.get("kind") == "exit" and isinstance(r.get("r"), int | float)
          and (sleeve is None or r.get("sleeve") == sleeve)]
    if not tr:
        return {"trades": 0, "mean_r": None, "ci": None, "pnl": 0.0}
    r = np.array([float(x["r"]) for x in tr])
    days = [str(x["t"])[:10] for x in tr]
    ci = stats.bootstrap_ci(r, block="day", days=days) if len(set(days)) > 1 else None
    return {"trades": len(tr), "mean_r": round(float(r.mean()), 4), "ci": None if ci is None else [round(v, 4) for v in ci],
            "pnl": round(sum(float(x["pnl"]) for x in tr), 2)}


def desk(rows: list[dict[str, Any]], names: list[str], start_equity: float) -> dict[str, Any]:
    """The three books together, cycle by cycle: largest drawdown, worst UTC day, and how often one coin sat in
    two books at once."""
    equity = dict.fromkeys(names, start_equity)
    held: dict[str, list[str]] = {n: [] for n in names}
    by_t: dict[int, tuple[float, bool]] = {}
    for r in rows:
        if r.get("kind") != "sleeve" or r.get("sleeve") not in equity:
            continue
        equity[r["sleeve"]], held[r["sleeve"]] = float(r["equity"]), list(r.get("open") or [])
        coins = [c for v in held.values() for c in v]
        by_t[_epoch(r["t"])] = (sum(equity.values()), len(coins) != len(set(coins)))
    ts = sorted(by_t)
    curve = np.array([by_t[t][0] for t in ts])
    peak = np.maximum.accumulate(curve)
    day_end: dict[int, float] = {}
    for t, v in zip(ts, curve, strict=True):
        day_end[t // DAY] = float(v)
    ends = [day_end[d] for d in sorted(day_end)]
    moves = [(b / a - 1) * 100 for a, b in zip([start_equity * len(names), *ends[:-1]], ends, strict=True)]
    return {"cycles": len(ts), "end_equity": round(float(curve[-1]), 2),
            "max_drawdown_pct": round(float(((curve - peak) / peak).min() * 100), 2),
            "worst_day_pct": round(min(moves), 2),
            "same_coin_twice_share": round(float(np.mean([by_t[t][1] for t in ts])), 4)}


def one(rows: list[dict[str, Any]], names: list[str], start_equity: float) -> dict[str, Any]:
    refused = [c for r in rows if r.get("kind") == "refused" for c in r.get("why", [])]
    return {"desk": {**mean_r(rows), **desk(rows, names, start_equity)},
            "sleeves": {n: mean_r(rows, n) for n in names},
            "entries": sum(1 for r in rows if r.get("kind") == "entry"),
            "refused_by": {c: refused.count(c) for c in CODES}}


def report(res: dict[str, Any]) -> str:
    day = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).date().isoformat()      # noqa: E731
    a, b = res["alone"], res["limited"]

    def r(d: dict[str, Any]) -> str:
        if d["mean_r"] is None:
            return "-"
        ci = "" if d["ci"] is None else f" ({d['ci'][0]:+.3f} to {d['ci'][1]:+.3f})"
        return f"{d['mean_r']:+.3f}{ci}"
    lines = [f"# {res['id']}: crypto tournament, the desk-wide limits", "",
             f"- Decision: DEC-0019. Span: {day(res['start'])} to {day(res['end'])}, {len(res['pairs'])} pairs "
             f"(data hash `{res['data_hash']}`), costs as EXP-0016.",
             f"- Limits: one position per coin across the books; open risk at most "
             f"{res['limits']['max_open_risk_pct']}% of their combined equity.",
             f"- The run without limits reproduces EXP-0016's trades: **{'yes' if res['reproduces_exp_0016'] else 'NO'}**.", "",
             "## The desk (three books together)", "",
             "| | Each book alone | Under the limits |", "|---|---|---|",
             f"| Entries | {a['entries']} | {b['entries']} |",
             f"| Refused: coin already held by another book | - | {b['refused_by']['desk_coin']} |",
             f"| Refused: open risk | - | {b['refused_by']['desk_risk']} |",
             f"| Closed trades | {a['desk']['trades']} | {b['desk']['trades']} |",
             f"| Mean R (95% interval over days) | {r(a['desk'])} | {r(b['desk'])} |",
             f"| Profit and loss, US$ | {a['desk']['pnl']:+,.0f} | {b['desk']['pnl']:+,.0f} |",
             f"| Largest drawdown | {a['desk']['max_drawdown_pct']}% | {b['desk']['max_drawdown_pct']}% |",
             f"| Worst UTC day | {a['desk']['worst_day_pct']}% | {b['desk']['worst_day_pct']}% |",
             f"| Share of bars with one coin in two books | {a['desk']['same_coin_twice_share'] * 100:.1f}% | "
             f"{b['desk']['same_coin_twice_share'] * 100:.1f}% |", "",
             "## Per sleeve", "", "| Sleeve | Trades alone | Mean R alone | Trades limited | Mean R limited |",
             "|---|---|---|---|---|"]
    for n in res["names"]:
        lines.append(f"| {n.upper()} | {a['sleeves'][n]['trades']} | {r(a['sleeves'][n])} | {b['sleeves'][n]['trades']} | "
                     f"{r(b['sleeves'][n])} |")
    lines += ["", "## Reading it", "",
              "- The limits are a risk control adopted by DEC-0019; this result does not decide them.",
              "- It changes no gate C1 verdict. The sleeves failed C1 (EXP-0016) and remain incubation.",
              "- Sleeves are evaluated in a fixed order, so the first to fire on a coin takes it.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crypto_desk_limits")
    ap.add_argument("--out", default="research/experiments/EXP-0020-crypto-desk-limits")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_yaml("crypto.yaml")
    ref = json.loads((ROOT / "research/experiments/EXP-0016-crypto-c1/result.json").read_text())
    start, end = int(ref["start"]), int(ref["end"])
    pairs: dict[str, str] = dict(cfg["sleeves"]["common"]["pairs"])
    client = None if a.offline else CoinbasePublic()
    hourly = {n: load_hourly(n, start - WARMUP_D * DAY, end, client) for n in pairs}
    infos = pair_infos(pairs, a.offline)
    lim = risk.load_desk_limits()
    if lim is None:
        print("no desk limits in config/risk.yaml (key CD)")
        return 1
    names = [n for n in rules.NAMES if n in cfg["sleeves"]]
    equity = float(cfg["sleeves"]["common"]["start_equity"])
    alone = backtest.run(cfg, hourly, infos, start, end)
    limited = backtest.run(cfg, hourly, infos, start, end, desk_lim=lim)
    was = {s["name"]: (s["base"]["trades"], s["base"]["mean_r"]) for s in ref["sleeves"]}
    got = one(alone, names, equity)
    same = data_hash(hourly) == ref["data_hash"] and all(
        (got["sleeves"][n]["trades"], got["sleeves"][n]["mean_r"]) == was[n] for n in names)
    folder = ROOT / a.out
    res = {"id": folder.name.split("-crypto")[0], "decision": "DEC-0019", "start": start, "end": end, "pairs": list(pairs),
           "names": names, "costs": cfg["costs"], "data_hash": data_hash(hourly), "reproduces_exp_0016": bool(same),
           "limits": {"one_position_per_coin": lim.one_position_per_coin, "max_open_risk_pct": float(lim.max_open_risk_pct)},
           "alone": got, "limited": one(limited, names, equity)}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    (folder / "report.md").write_text(report(res))
    print(f"reproduces EXP-0016: {same}; wrote {folder.relative_to(ROOT)}")
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
