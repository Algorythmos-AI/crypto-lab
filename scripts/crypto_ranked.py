"""EXP-0022 (DEC-0025, HYP-0024): the TREND rule with one bar's signals entered strongest first, through gate C1.

    python scripts/crypto_ranked.py --offline --out research/experiments/EXP-0022-crypto-trend-ranked

The variant alone on its own book over EXP-0016's span, data and costs; the stress run at 1.5x slippage; the
random-entry control, ordered by the same measure; DEC-0015's verdict. Beside it, for reading and not for the
verdict: the registered TREND on the same span and the difference between the two, and DEC-0022's lines. If the
verdict is a pass, and only then, one run on the two years before (DEC-0021). None of this is an option.
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

from crypto_backtest import LIMITS, data_hash, pair_infos  # noqa: E402
from wt.core.config import load_yaml  # noqa: E402
from wt.crypto import backtest, challengers, rules  # noqa: E402
from wt.crypto.data import CoinbasePublic  # noqa: E402
from wt.crypto.history import WARMUP_D, load_hourly  # noqa: E402
from wt.research.trials import family_trial_count  # noqa: E402

DAY = 86_400
SEED, N_BOOT = 7, 5000


def difference(a: list[dict[str, Any]], b: list[dict[str, Any]], seed: int = SEED, n_boot: int = N_BOOT) -> dict[str, Any]:
    """Mean R of trades `a` minus mean R of trades `b`, with a 95% interval from a bootstrap over the UTC days
    either closed a trade on: the two sleeves trade the same market on the same days."""
    if not a or not b:
        return {"diff": None, "ci": None}
    days = sorted({str(x["t"])[:10] for x in (*a, *b)})
    at = {d: i for i, d in enumerate(days)}
    s = np.zeros((2, len(days)))
    n = np.zeros((2, len(days)))
    for k, rows in enumerate((a, b)):
        for x in rows:
            s[k, at[str(x["t"])[:10]]] += float(x["r"])
            n[k, at[str(x["t"])[:10]]] += 1
    pick = np.random.default_rng(seed).integers(0, len(days), size=(n_boot, len(days)))
    na, nb = n[0][pick].sum(axis=1), n[1][pick].sum(axis=1)
    ok = (na > 0) & (nb > 0)
    boots = s[0][pick].sum(axis=1)[ok] / na[ok] - s[1][pick].sum(axis=1)[ok] / nb[ok]
    return {"diff": round(float(s[0].sum() / n[0].sum() - s[1].sum() / n[1].sum()), 4),
            "ci": [round(float(np.percentile(boots, 2.5)), 4), round(float(np.percentile(boots, 97.5)), 4)]}


def confirm(cfg: dict[str, Any], spec: rules.Spec, hourly: dict[str, list[Any]], infos: dict[str, Any], start: int,
            end: int) -> dict[str, Any]:
    """DEC-0021's confirmation for this variant: the same thresholds as a challenger's."""
    rows = backtest.run(cfg, hourly, infos, start, end, specs={spec.name: spec})
    s = backtest.summary(rows, spec.name, start, end, 1, float(cfg["sleeves"]["common"]["start_equity"]))
    trades, mean_r, pf = int(s["trades"]), s.get("mean_r"), s.get("profit_factor")
    made_money = mean_r is not None and mean_r > 0 and (pf is None or pf > 1)
    return {"passed": bool(trades >= challengers.MIN_CONFIRM_TRADES and made_money), "trades": trades, "mean_r": mean_r,
            "profit_factor": pf, "span": [start, end], "pairs": sorted(hourly), "data_hash": data_hash(hourly)}


def report(res: dict[str, Any]) -> str:
    day = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).date().isoformat()      # noqa: E731
    b, h, v, t, d, c, bm = res["base"], res["stressed"], res["verdict"], res["trend"], res["difference"], res["costs_r"], res["benchmark"]
    lines = [f"# {res['id']}: crypto desk, TREND with ranked entries (HYP-0024), gate C1", "",
             f"- Decision: DEC-0025. Family C trial count used for the deflated Sharpe: {res['n_trials']}.",
             f"- Span: {day(res['start'])} to {day(res['end'])}, {len(res['pairs'])} pairs (data hash `{res['data_hash']}`), "
             "costs as EXP-0016; the stress run uses 1.5x slippage.",
             f"- Random-entry control: {res['controls']} runs, same exits, sizing, costs and ordering.",
             f"- The registered TREND rerun beside it reproduces EXP-0016: **{'yes' if res['reproduces_exp_0016'] else 'NO'}**.", "",
             "## Trades per month (reported first)", "",
             f"{b['trades']} trades from {b.get('signals', 0)} signals: {b['trades_per_month']} a month.", "",
             "## Result", "",
             "| | Ranked (HYP-0024) | Registered TREND |", "|---|---|---|",
             f"| Trades | {b['trades']} | {t['trades']} |",
             f"| Win rate | {b['win_rate'] * 100:.1f}% | {t['win_rate'] * 100:.1f}% |",
             f"| Mean R after costs | {b['mean_r']:+.3f} | {t['mean_r']:+.3f} |",
             f"| CI95 at 1.5x slippage | [{h['ci_low']:+.3f}, {h['ci_high']:+.3f}] | - |",
             f"| Profit factor | {b['profit_factor']} | {t['profit_factor']} |",
             f"| Deflated Sharpe | {b['dsr']:.3f} | - |",
             f"| Random-entry control p | {res['control_p']} | - |",
             f"| Largest drawdown | {b['max_drawdown_pct']}% | {t['max_drawdown_pct']}% |", "",
             f"**Verdict (DEC-0015): {'PASSED' if v['passed'] else 'FAILED: ' + ', '.join(v['failed_on'])}**", "",
             "## For reading the result (never part of the verdict)", "",
             f"- Ranked minus registered TREND, mean R: {d['diff']:+.3f} (95% interval {d['ci'][0]:+.3f} to {d['ci'][1]:+.3f})."
             if d["diff"] is not None else "- Ranked minus registered TREND: not computable.",
             f"- Cost per trade: {c['cost_mean_r']:.3f}R mean ({c['cost_median_r']:.3f}R median). Mean R before costs: "
             f"{c['gross_mean_r']:+.3f} (standard error {c['gross_se_r']:.3f}).",
             f"- Holding the {bm['pairs']} pairs in equal weight over the span, no costs: {bm['return_pct']:+.1f}%, "
             f"largest drawdown {bm['max_drawdown_pct']:.1f}%.",
             "- Entries by pair, ranked: " + ", ".join(f"{k} {x['trades']}" for k, x in b["by_pair"].items()) + ".", ""]
    cf = res.get("confirmation")
    lines += ["## Confirmation on the two years before (DEC-0021)", "",
              "Not run: only a variant that passes gate C1 touches that history." if cf is None else
              f"{'CONFIRMED' if cf['passed'] else 'NOT CONFIRMED'}: {cf['trades']} trades, mean R {cf['mean_r']}, "
              f"profit factor {cf['profit_factor']}, on {len(cf['pairs'])} pairs.", "", *LIMITS]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crypto_ranked")
    ap.add_argument("--out", default="research/experiments/EXP-0022-crypto-trend-ranked")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--controls", type=int, default=challengers.CONTROLS)
    a = ap.parse_args(argv)
    cfg = load_yaml("crypto.yaml")
    ref = json.loads((ROOT / "research/experiments/EXP-0016-crypto-c1/result.json").read_text())
    start, end = int(ref["start"]), int(ref["end"])
    pairs: dict[str, str] = dict(cfg["sleeves"]["common"]["pairs"])
    client = None if a.offline else CoinbasePublic()
    hourly = {n: load_hourly(n, start - WARMUP_D * DAY, end, client) for n in pairs}
    infos = pair_infos(pairs, a.offline)
    spec = rules.trend_ranked(cfg)
    only = {spec.name: spec}
    n_trials, equity = family_trial_count("C"), float(cfg["sleeves"]["common"]["start_equity"])
    rows = backtest.run(cfg, hourly, infos, start, end, specs=only)
    hard = backtest.run(cfg, hourly, infos, start, end, slip_mult=1.5, specs=only)
    base = backtest.summary(rows, spec.name, start, end, n_trials, equity)
    stressed = backtest.summary(hard, spec.name, start, end, n_trials, equity)
    p, means = backtest.control(cfg, hourly, infos, start, end, rows, spec.name, a.controls, SEED, specs=only)
    verdict = backtest.verdict(base, stressed, p)
    plain = backtest.run(cfg, hourly, infos, start, end, names=["trend"])
    trend = backtest.summary(plain, "trend", start, end, n_trials, equity)
    was = next(s["base"] for s in ref["sleeves"] if s["name"] == "trend")
    same = data_hash(hourly) == ref["data_hash"] and (trend["trades"], trend["mean_r"]) == (was["trades"], was["mean_r"])
    folder = ROOT / a.out
    res: dict[str, Any] = {
        "id": folder.name.split("-crypto")[0], "decision": "DEC-0025", "hypothesis": "HYP-0024", "start": start, "end": end,
        "pairs": list(pairs), "costs": cfg["costs"], "n_trials": n_trials, "controls": a.controls, "seed": SEED,
        "data_hash": data_hash(hourly), "order": spec.order, "base": base, "stressed": stressed,
        "control_p": None if p is None else round(float(p), 4),
        "control_mean_r": round(float(np.mean(means)), 4) if means else None, "verdict": verdict,
        "trend": trend, "reproduces_exp_0016": bool(same),
        "difference": difference(backtest.trades(rows, spec.name), backtest.trades(plain, "trend")),
        "costs_r": backtest.cost_in_r(rows, spec.name, float(cfg["costs"]["slippage_bps"])),
        "benchmark": backtest.buy_and_hold(hourly, start, end), "confirmation": None}
    if verdict["passed"]:
        span = end - start
        earlier = {n: load_hourly(n, start - span - WARMUP_D * DAY, start, client) for n in pairs}
        earlier = {k: v for k, v in earlier.items() if v}
        res["confirmation"] = confirm(cfg, spec, earlier, infos, start - span, start)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    (folder / "report.md").write_text(report(res))
    print(json.dumps({"verdict": verdict, "mean_r": base.get("mean_r"), "reproduces": same}), f"\nwrote {folder.relative_to(ROOT)}")
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
