"""Gate C1 for the tournament sleeves (DEC-0015): two years of history through the desk's own code.

    python scripts/crypto_backtest.py                      # fetch what is missing, run, write the experiment
    python scripts/crypto_backtest.py --years 2 --controls 100 --out research/experiments/EXP-0016-crypto-c1

History is hourly candles from a second public exchange (Kraken serves only its last 720 bars), cached under
data/crypto/history (not in git). The report states trades per month first, then the registered tests. It is run
once per frozen config: the config hash of every sleeve is recorded in the result.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from wt.core.config import load_yaml  # noqa: E402
from wt.crypto import backtest, rules, sleeves  # noqa: E402
from wt.crypto.history import CACHE, WARMUP_D, load_hourly  # noqa: E402
from wt.crypto.data import Bar, CoinbasePublic, DataError, KrakenPublic, PairInfo, aggregate, fill_grid  # noqa: E402
from wt.research.trials import family_trial_count  # noqa: E402

HOUR, H4, DAY = 3600, 14_400, 86_400


def pair_infos(pairs: dict[str, str], offline: bool) -> dict[str, PairInfo]:
    path = CACHE / "pair_info.json"
    if not offline:
        got = KrakenPublic().pair_infos(list(pairs.values()))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: [v.lot_decimals, str(v.tick), str(v.order_min), str(v.cost_min)]
                                    for k, v in got.items()}, sort_keys=True))
    raw = json.loads(path.read_text())
    return {k: PairInfo(int(v[0]), Decimal(v[1]), Decimal(v[2]), Decimal(v[3])) for k, v in raw.items()}


def agreement(pairs: dict[str, str], hourly: dict[str, list[Bar]]) -> dict[str, Any]:
    """How far the history's 4-hour closes are from Kraken's own, over the bars Kraken still serves."""
    out: dict[str, Any] = {}
    api = KrakenPublic()
    for name, kraken_pair in pairs.items():
        try:
            theirs = {b.t: b.c for b in api.ohlc(kraken_pair, 240)}
        except DataError as e:
            out[name] = {"error": str(e)[:60]}
            continue
        ours = {b.t: b.c for b in aggregate(fill_grid(hourly[name], HOUR), HOUR, H4)}
        diff = [abs(ours[t] / theirs[t] - 1) * 100 for t in theirs if t in ours and theirs[t] > 0]
        out[name] = {"bars": len(diff), "median_pct": round(float(np.median(diff)), 4),
                     "p95_pct": round(float(np.percentile(diff, 95)), 4), "max_pct": round(max(diff), 4)} if diff else {"bars": 0}
    return out


def data_hash(hourly: dict[str, list[Bar]]) -> str:
    h = hashlib.sha256()
    for name in sorted(hourly):
        for b in hourly[name]:
            h.update(f"{name},{b.t},{b.o},{b.h},{b.l},{b.c},{b.v}\n".encode())
    return h.hexdigest()[:16]


def report(res: dict[str, Any]) -> str:
    day = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).date().isoformat()      # noqa: E731
    lines = [f"# {res['id']}: crypto tournament sleeves, gate C1", "",
             f"- Decision: DEC-0015. Family C trial count used for the deflated Sharpe: {res['n_trials']}.",
             f"- Span: {day(res['start'])} to {day(res['end'])}, {len(res['pairs'])} pairs, hourly history "
             f"(data hash `{res['data_hash']}`).",
             f"- Costs: {res['costs']['taker_fee_pct']}% taker each way, {res['costs']['slippage_bps']} bps slippage; "
             "the stress run uses 1.5x slippage.",
             f"- Random-entry control: {res['controls']} runs per sleeve, same exits, sizing and costs.", "",
             "## Trades per month (reported first)", "", "| Sleeve | Signals | Trades | Per month |", "|---|---|---|---|"]
    for s in res["sleeves"]:
        b = s["base"]
        lines.append(f"| {s['name']} | {b.get('signals', 0)} | {b['trades']} | {b['trades_per_month']} |")
    lines += ["", "## Result", "",
              "| Sleeve | Win rate | Mean R | CI95 (1.5x slip) | Profit factor | Deflated Sharpe | Control p | Max DD | Verdict |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s in res["sleeves"]:
        b, h, v = s["base"], s["stressed"], s["verdict"]
        if b["trades"] == 0:
            lines.append(f"| {s['name']} | - | - | - | - | - | - | - | FAILED (no trades) |")
            continue
        lines.append(f"| {s['name']} | {b['win_rate'] * 100:.1f}% | {b['mean_r']:+.3f} | "
                     f"[{h.get('ci_low', float('nan')):+.3f}, {h.get('ci_high', float('nan')):+.3f}] | "
                     f"{b['profit_factor']} | {b['dsr']:.3f} | {s['control_p']} | {b['max_drawdown_pct']}% | "
                     f"{'PASSED' if v['passed'] else 'FAILED: ' + ', '.join(v['failed_on'])} |")
    lines += ["", "## Agreement between the history and Kraken (4-hour closes)", "",
              "| Pair | Bars compared | Median | 95th percentile | Largest |", "|---|---|---|---|---|"]
    for name, a in res["agreement"].items():
        lines.append(f"| {name} | {a.get('bars', 0)} | {a.get('median_pct', '-')}% | {a.get('p95_pct', '-')}% | {a.get('max_pct', '-')}% |")
    lines += ["", "## Notes", "",
              "- The backtest calls the desk's own `sleeves.step_pair`: entry rules, levels, sizing, limits and exit",
              "  resolution are the code that trades. Stops and targets resolve on hourly bars, stop first.",
              "- The daily-loss latch is cleared at the next UTC day; on the desk the owner clears it.",
              "- A sleeve that fails stops opening paper trades unless the owner records otherwise (DEC-0014).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crypto_backtest")
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--controls", type=int, default=100)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="research/experiments/EXP-0016-crypto-c1")
    ap.add_argument("--offline", action="store_true", help="use the cache only; skip the agreement check")
    ap.add_argument("--end", type=int, default=None, help="end of the span, epoch seconds (default: now)")
    a = ap.parse_args(argv)
    cfg = load_yaml("crypto.yaml")
    pairs: dict[str, str] = dict(cfg["sleeves"]["common"]["pairs"])
    end = (a.end or int(time.time())) // H4 * H4
    start = end - int(a.years * 365 * DAY)
    client = None if a.offline else CoinbasePublic()
    hourly = {}
    for name in pairs:
        hourly[name] = load_hourly(name, start - WARMUP_D * DAY, end, client)
        print(f"history {name}: {len(hourly[name])} hourly bars")
    infos = pair_infos(pairs, a.offline)
    names = [n for n in rules.NAMES if n in cfg["sleeves"]]
    n_trials, equity = family_trial_count("C"), float(cfg["sleeves"]["common"]["start_equity"])
    t0 = time.time()
    base = backtest.run(cfg, hourly, infos, start, end)
    print(f"base run: {time.time() - t0:.0f}s, {sum(1 for r in base if r.get('kind') == 'exit')} closed trades")
    hard = backtest.run(cfg, hourly, infos, start, end, slip_mult=1.5)
    out = []
    for n in names:
        b = backtest.summary(base, n, start, end, n_trials, equity)
        h = backtest.summary(hard, n, start, end, n_trials, equity)
        t1 = time.time()
        p, means = backtest.control(cfg, hourly, infos, start, end, base, n, a.controls, a.seed)
        print(f"{n}: {b['trades']} trades ({b['trades_per_month']}/month); control {time.time() - t1:.0f}s p={p}")
        out.append({"name": n, "hypothesis": cfg["sleeves"][n]["hypothesis"], "config": sleeves.sleeve_hash(cfg, n),
                    "base": b, "stressed": h, "control_p": None if p is None else round(p, 4),
                    "control_mean_r": round(float(np.mean(means)), 4) if means else None,
                    "verdict": backtest.verdict(b, h, p)})
    folder = ROOT / a.out
    res = {"id": folder.name.split("-crypto")[0], "decision": "DEC-0015", "start": start, "end": end,
           "pairs": list(pairs), "costs": cfg["costs"], "n_trials": n_trials, "controls": a.controls, "seed": a.seed,
           "data_hash": data_hash(hourly), "agreement": {} if a.offline else agreement(pairs, hourly), "sleeves": out}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    (folder / "report.md").write_text(report(res))
    print(f"wrote {folder.relative_to(ROOT)}/result.json and report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
