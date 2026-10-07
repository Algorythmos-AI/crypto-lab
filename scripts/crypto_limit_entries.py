"""EXP-0021 (DEC-0020): would the registered rules' signals do better entered by a resting buy limit?

    python scripts/crypto_limit_entries.py --offline --out research/experiments/EXP-0021-crypto-limit-entries
    python scripts/crypto_limit_entries.py --confirm trend      # only for a sleeve that passed, once (DEC-0020, 2)

Signal by signal on the eight traded pairs over EXP-0016's two years: the desk's market entry against a limit at
the signal bar's close, both followed by `wt.crypto.signals`. No book, sizing or limit is involved on either
side. The fill rule, the fees and the verdict are DEC-0020's and are not options here.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from crypto_backtest import data_hash  # noqa: E402
from wt.backtest import stats  # noqa: E402
from wt.core.config import load_yaml  # noqa: E402
from wt.crypto import rules, signals  # noqa: E402
from wt.crypto.data import CoinbasePublic  # noqa: E402
from wt.crypto.history import WARMUP_D, load_hourly  # noqa: E402
from wt.ml import dataset  # noqa: E402

HOUR, DAY = 3600, 86_400
MIN_TRADES = 30
FEES = {"primary": {"taker_fee_pct": 0.40, "maker_fee_pct": 0.22, "slip_mult": 1.0},
        "stress": {"taker_fee_pct": 0.40, "maker_fee_pct": 0.22, "slip_mult": 1.5},
        "first_tier": {"taker_fee_pct": 0.80, "maker_fee_pct": 0.40, "slip_mult": 1.0}}


def follow(cfg: dict[str, Any], hourly: dict[str, list[Any]], start: int, end: int, fees: dict[str, float]) -> list[dict[str, Any]]:
    """One row per signal: its market entry's result and its limit entry's."""
    sc = cfg["sleeves"]
    c = rules.Common.of(sc["common"])
    tf_s = c.timeframe_min * 60
    slip_bps = float(cfg["costs"]["slippage_bps"]) * fees["slip_mult"]
    costs = {"taker_fee_pct": fees["taker_fee_pct"], "slippage_bps": slip_bps}
    got = dataset.scan(cfg, hourly, start, end)
    rows = []
    for n, pair, i, close, _price, _stop, _target, atr in got.found:
        bars = got.h4[pair]
        horizon = int(sc[n]["time_stop_bars"]) + 3
        later = bars[max(0, i + 1 - c.bars):i + 1 + horizon]
        lo = bisect.bisect_left(got.fine_t[pair], close)
        hi = bisect.bisect_right(got.fine_t[pair], close + horizon * tf_s)
        fine = got.grids[pair][lo:hi]
        price = bars[i].c * (1 + slip_bps / 10_000)
        stop, target, skip = rules.levels(price, atr, c, sc[n])
        market = None if skip is not None else signals.outcome(n, price, stop, target, atr, bars[i].t, later, fine, c,
                                                               sc[n], costs, HOUR)
        limit = signals.limit_outcome(n, bars[i].c, atr, bars[i].t, later, fine, c, sc[n], costs, HOUR,
                                      fees["maker_fee_pct"])
        if market is None or limit is None:
            continue                                     # one side has no outcome yet: the signal is in neither
        rows.append({"sleeve": n, "pair": pair, "t": close, "market_r": market["r"], "filled": bool(limit["filled"]),
                     "skipped": limit.get("skipped"), "limit_r": limit.get("r"),
                     "limit_day": None if not limit["filled"] else limit["exit_t"] // DAY, "market_day": market["exit_t"] // DAY})
    return rows


def _mean(r: list[float], days: list[int]) -> dict[str, Any]:
    if not r:
        return {"n": 0, "mean_r": None, "ci": None, "win_rate": None}
    a = np.array(r)
    ci = stats.bootstrap_ci(a, block="day", days=days) if len(set(days)) > 1 else None
    return {"n": len(r), "mean_r": round(float(a.mean()), 4), "ci": None if ci is None else [round(v, 4) for v in ci],
            "win_rate": round(float((a > 0).mean()), 4)}


def table(rows: list[dict[str, Any]], names: list[str]) -> dict[str, Any]:
    out = {}
    for n in names:
        mine = [r for r in rows if r["sleeve"] == n]
        fil = [r for r in mine if r["filled"]]
        miss = [r for r in mine if not r["filled"] and r["skipped"] is None]
        out[n] = {"signals": len(mine), "filled": len(fil), "missed": len(miss),
                  "skipped": sum(1 for r in mine if r["skipped"] is not None),
                  "limit": _mean([r["limit_r"] for r in fil], [r["limit_day"] for r in fil]),
                  "market": _mean([r["market_r"] for r in mine], [r["market_day"] for r in mine]),
                  # The same signals at the market, split by whether the limit order would have caught them: what
                  # the resting order selects for.
                  "market_of_filled": _mean([r["market_r"] for r in fil], [r["market_day"] for r in fil]),
                  "market_of_missed": _mean([r["market_r"] for r in miss], [r["market_day"] for r in miss])}
    return out


def passed(primary: dict[str, Any], stress: dict[str, Any]) -> dict[str, Any]:
    why = []
    if primary["filled"] < MIN_TRADES:
        why.append("under_30_trades")
    for name, t in (("primary", primary), ("stress", stress)):
        ci = t["limit"]["ci"]
        if t["limit"]["mean_r"] is None or t["limit"]["mean_r"] <= 0 or ci is None or ci[0] <= 0:
            why.append(f"ci_not_above_zero_{name}")
    return {"passed": not why, "failed_on": why}


def report(res: dict[str, Any]) -> str:
    day = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).date().isoformat()      # noqa: E731

    def r(d: dict[str, Any]) -> str:
        if d["mean_r"] is None:
            return "-"
        ci = "" if d["ci"] is None else f" ({d['ci'][0]:+.3f} to {d['ci'][1]:+.3f})"
        return f"{d['mean_r']:+.3f}{ci}"
    lines = [f"# {res['id']}: crypto desk, resting-limit entries screened on the registered rules' signals", "",
             f"- Decision: DEC-0020. Span: {day(res['start'])} to {day(res['end'])}, {len(res['pairs'])} pairs "
             f"(data hash `{res['data_hash']}`).",
             "- A buy limit at the signal bar's close, filled only by an hourly bar that trades below it within "
             "4 hours. Signal by signal; no book or sizing on either side.",
             "- Mean R is per trade, with a 95% interval from a bootstrap over days.", ""]
    for key, title in (("primary", "Primary fees: maker 0.22%, taker 0.40%, 5 bps slippage"),
                       ("stress", "Stress: the same fees, slippage at 1.5 times"),
                       ("first_tier", "Reported only: the venue's first tier, maker 0.40%, taker 0.80%")):
        lines += [f"## {title}", "",
                  "| Sleeve | Signals | Filled | Missed | Limit entry, mean R | Market entry, mean R | Market R of the filled | "
                  "Market R of the missed |", "|---|---|---|---|---|---|---|---|"]
        for n in res["names"]:
            t = res["fees"][key][n]
            lines.append(f"| {n.upper()} | {t['signals']} | {t['filled']} | {t['missed']} | {r(t['limit'])} | {r(t['market'])} | "
                         f"{r(t['market_of_filled'])} | {r(t['market_of_missed'])} |")
        lines.append("")
    lines += ["## Verdict (DEC-0020, section 2)", ""]
    for n in res["names"]:
        v = res["verdict"][n]
        lines.append(f"- **{n.upper()}: {'passes the screen' if v['passed'] else 'fails'}**"
                     + ("" if v["passed"] else f" ({', '.join(v['failed_on'])})"))
    lines += ["", "## Reading it", "",
              "- The last two columns are the same signals bought at the market, split by whether the resting order "
              "would have been filled. They show what a resting order selects for.",
              "- Passing is not gate C1, and a sleeve that fails is not tried again with another fill rule.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crypto_limit_entries")
    ap.add_argument("--out", default="research/experiments/EXP-0021-crypto-limit-entries")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--confirm", default=None, help="a sleeve that passed: run it once on the two years before the span")
    a = ap.parse_args(argv)
    cfg = load_yaml("crypto.yaml")
    ref = json.loads((ROOT / "research/experiments/EXP-0016-crypto-c1/result.json").read_text())
    start, end = int(ref["start"]), int(ref["end"])
    if a.confirm:
        start, end = start - (end - start), start
    pairs = list(cfg["sleeves"]["common"]["pairs"])
    client = None if a.offline else CoinbasePublic()
    hourly = {n: load_hourly(n, start - WARMUP_D * DAY, end, client) for n in pairs}
    hourly = {k: v for k, v in hourly.items() if v}
    names = [n for n in rules.NAMES if n in cfg["sleeves"] and (a.confirm is None or n == a.confirm)]
    fees = {k: table(follow(cfg, hourly, start, end, f), names) for k, f in FEES.items() if not a.confirm or k == "primary"}
    verdict = {n: passed(fees["primary"][n], fees.get("stress", fees["primary"])[n]) for n in names}
    folder = ROOT / (a.out + (f"-confirm-{a.confirm}" if a.confirm else ""))
    res = {"id": folder.name.split("-crypto")[0], "decision": "DEC-0020", "start": start, "end": end, "pairs": sorted(hourly),
           "names": names, "data_hash": data_hash(hourly), "fee_sets": FEES, "fees": fees, "verdict": verdict,
           "confirmation_of": a.confirm}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    (folder / "report.md").write_text(report(res) if not a.confirm else json.dumps(verdict, indent=1) + "\n")
    print(json.dumps(verdict), f"\nwrote {folder.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
