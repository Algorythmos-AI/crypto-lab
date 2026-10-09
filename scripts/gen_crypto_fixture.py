"""Writes dashboard/test/fixtures/crypto.v1.json: a crypto desk snapshot built by the real builder
(wt.crypto.snapshot.collect) from a synthetic, seeded desk state. No real prices, trades or account data.

    python scripts/gen_crypto_fixture.py            # rewrite the fixture
    python scripts/gen_crypto_fixture.py --check    # exit 1 if the committed fixture is stale (CI)
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from wt.core import ledger  # noqa: E402
from wt.core.desk import DESKS  # noqa: E402
from wt.crypto import cycle, features, snapshot  # noqa: E402
from wt.crypto.book import Book  # noqa: E402
from wt.crypto.data import PairInfo  # noqa: E402
from wt.ops import publish  # noqa: E402

FIXTURE = ROOT / "dashboard" / "test" / "fixtures" / "crypto.v1.json"
NOW = dt.datetime(2026, 10, 2, 20, 0, 40, tzinfo=dt.UTC)
BASE = {"BTC/USD": 120_000.0, "ETH/USD": 3_900.0, "SOL/USD": 175.0}
INFO = PairInfo(8, Decimal("0.01"), Decimal("0.00001"), Decimal("1"))


def synthetic(state: Path, days: int = 5, seed: int = 7) -> None:
    """A desk state on disk: observations every 15 minutes, a handful of closed trades, one open position."""
    rng = random.Random(seed)
    cfg = yaml.safe_load((ROOT / "config" / "crypto.yaml").read_text())
    h = cycle.config_hash(cfg)
    journal = state / "crypto_journal.jsonl"
    book = Book(Decimal(10000), Decimal(10000))
    start = int((NOW - dt.timedelta(days=days)).timestamp()) // 900 * 900
    price = dict(BASE)
    for i in range(days * 96):
        t = start + i * 900
        when = dt.datetime.fromtimestamp(t + 910, dt.UTC)
        seen: dict[str, Any] = {}
        for pair in BASE:
            price[pair] *= 1 + rng.gauss(0, 0.0015)
            thin = pair != "BTC/USD" and rng.random() < (0.45 if pair == "ETH/USD" else 0.3)
            spread = {"BTC/USD": 0.08, "ETH/USD": 0.24, "SOL/USD": 0.31}[pair] * rng.uniform(0.7, 1.4)
            fv = {k: None for k in features.COLUMNS}
            fv.update(rsi=round(min(95, max(5, rng.gauss(50, 14))), 2), atr_pct=round(abs(rng.gauss(0.12, 0.05)), 4),
                      vwap_distance_pct=round(rng.gauss(0, 0.25), 4), ema8_distance_pct=round(rng.gauss(0, 0.1), 4),
                      volume_ratio=None if thin else round(abs(rng.gauss(1, 0.6)), 3),
                      hour_utc=float(when.hour), day_of_week=float(when.weekday()))
            fire = fv["rsi"] < 32 and fv["vwap_distance_pct"] > 0 and fv["ema8_distance_pct"] > 0
            quality = (["bar_untraded"] if thin else []) + (["spread_wide"] if spread > 0.15 else [])
            snapshot_obs = {"t": t, "pair": pair, "close": round(price[pair], 2), "features": fv, "would_fire": fire,
                            "why_not": [] if fire else ["rsi_high"], "tradable": not quality, "quality": quality,
                            "traded_share": 0.9, "spread_pct": round(spread, 4), "config": h}
            f = state / "observations" / f"obs-{dt.datetime.fromtimestamp(t, dt.UTC).date()}.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a") as fh:
                fh.write(json.dumps(snapshot_obs, sort_keys=True) + "\n")
            seen[pair] = {"bar": t, "tradable": not quality, "quality": quality, "fire": fire}
        if i % 60 == 20 and "BTC/USD" not in book.positions:                # a trade every 15 hours or so
            pos = book.buy("BTC/USD", Decimal(50), price["BTC/USD"], INFO, 0.40, 5, t + 910, t, 0.5, 1.0)
            ledger.append(journal, {"id": f"e{i}", "kind": "entry", "t": when.isoformat(), "pair": "BTC/USD", "bar": t,
                                    "qty": str(pos.qty), "price": str(pos.entry_price), "config": h})
        elif i % 60 == 27 and "BTC/USD" in book.positions and i < days * 96 - 60:
            pos = book.positions["BTC/USD"]
            win = rng.random() < 0.45
            fill = book.sell("BTC/USD", float(pos.target if win else pos.stop), 0.40, 0 if win else 5, t + 910)
            ledger.append(journal, {"id": f"x{i}", "kind": "exit", "t": when.isoformat(), "pair": "BTC/USD",
                                    "reason": "target" if win else "stop", "config": h, **fill})
        elif i % 97 == 50:
            ledger.append(journal, {"id": f"r{i}", "kind": "refused", "t": when.isoformat(), "pair": "ETH/USD",
                                    "bar": t, "why": ["spread_wide", "kill"][: 1 + i % 2], "config": h})
        ledger.append(journal, {"id": f"c{i}", "kind": "cycle", "t": when.isoformat(), "config": h, "pairs": seen,
                                "failed": {} if i % 131 else {"SOL/USD": "HTTP 502"}, "kill": True,
                                "open": sorted(book.positions),
                                "equity": str(book.equity({k: v for k, v in price.items()}).quantize(Decimal("0.01")))})
    book.save(state / "book.json")
    sleeves_fixture(state, journal, cfg, rng)
    (state / "KILL").write_text("fixture\n")
    import os
    stamp = (NOW - dt.timedelta(days=2)).timestamp()
    os.utime(state / "KILL", (stamp, stamp))


def sleeves_fixture(state: Path, journal: Path, cfg: dict[str, Any], rng: random.Random) -> None:
    """The tournament sleeves as the engine leaves them: TREND with closed trades and an open position, BREAK
    with closed trades, DIP untouched. Invented prices on the pairs the decision record names."""
    from wt.crypto import risk
    from wt.crypto import sleeves as engine
    info = PairInfo(8, Decimal("0.001"), Decimal("0.5"), Decimal("0.5"))
    end = int(NOW.timestamp()) // 14_400 * 14_400
    plan = {"trend": [("AVAX/USD", 11.5, 1.9), ("ADA/USD", 0.62, -1.0), ("LINK/USD", 14.2, 2.6)],
            "break": [("AVAX/USD", 11.5, 2.0), ("SOL/USD", 175.0, -1.0)], "dip": []}
    marks: dict[str, list[float]] = {}
    for name, trades in plan.items():
        extra = {"sleeve": name, "strategy": cfg["sleeves"][name]["hypothesis"], "tf": 240, "stage": engine.STAGE,
                 "config": engine.sleeve_hash(cfg, name)}
        book = Book(Decimal(10_000), Decimal(10_000))
        folder = risk.sleeve_dir(_desk(state), name)
        for k, (pair, px, r) in enumerate(trades):
            t = end - 14_400 * (30 - 6 * k)
            price, atr = Decimal(str(px)), Decimal(str(px)) * Decimal("0.02")
            stop = price - 2 * atr
            qty = (Decimal(100) / (price - stop)).quantize(Decimal("0.00000001"))
            qty = min(qty, (Decimal(3_000) / price).quantize(Decimal("0.00000001")))
            pos = book.buy_qty(pair, qty, price, info, 0.40, t + 10, t - 14_400, stop, price + 4 * atr, name, atr)
            ledger.append(journal, {"id": f"s{name}{k}e", "kind": "entry", "t": _iso(t + 10), "pair": pair, "bar": t - 14_400,
                                    "qty": str(pos.qty), "price": str(price), "stop": str(stop), **extra})
            out = float(price + (price - stop) * Decimal(str(r)))
            fill = book.sell(pair, out, 0.40, 5 if r < 0 else 0, t + 10 + 3_600 * rng.randint(5, 40))
            ledger.append(journal, {"id": f"s{name}{k}x", "kind": "exit", "t": _iso(t + 10 + fill["held_s"]), "pair": pair,
                                    "reason": "stop" if r < 0 else ("trend_exit" if name == "trend" else "target"),
                                    "qty": str(pos.qty), "entry_price": str(price), "entry_t": _iso(t + 10), **extra, **fill})
        if name == "trend":
            price = Decimal("2.41")
            pos = book.buy_qty("XRP/USD", Decimal("1041.66666666"), price, info, 0.40, end + 10, end - 14_400,
                               Decimal("2.314"), Decimal("Infinity"), name, Decimal("0.048"))
            ledger.append(journal, {"id": "strendopen", "kind": "entry", "t": _iso(end + 10), "pair": "XRP/USD",
                                    "bar": end - 14_400, "qty": str(pos.qty), "price": str(price), **extra})
            marks["XRP/USD"] = [2.447, end + 910]
        ledger.append(journal, {"id": f"s{name}refused", "kind": "refused", "t": _iso(end + 10), "pair": "DOGE/USD",
                                "bar": end - 14_400, "why": ["stop_too_tight"], **extra})
        ledger.append(journal, {"id": f"s{name}eval", "kind": "sleeve", "t": _iso(end + 10), "open": sorted(book.positions),
                                "equity": str(book.equity({}).quantize(Decimal("0.01"))), "kill": False,
                                "pairs": {p: {"bar": end - 14_400, "fire": p == "DOGE/USD",
                                              "why": [] if p == "DOGE/USD" else ["no_new_high"]}
                                          for p in cfg["sleeves"]["common"]["pairs"]}, **extra})
        book.save(folder / "book.json")
    (state / "sleeves" / "data.json").write_text(json.dumps({"marks": marks}))
    challengers_fixture(journal, end)
    learning_fixture(state, journal, end)
    # The desk-wide limit at work (DEC-0019): BREAK's signal on a coin TREND already holds is refused.
    ledger.append(journal, {"id": "sbreakdesk", "kind": "refused", "t": _iso(end + 10), "pair": "XRP/USD", "bar": end - 14_400,
                            "why": ["desk_coin"], "sleeve": "break", "strategy": cfg["sleeves"]["break"]["hypothesis"],
                            "tf": 240, "stage": "incubation"})


def learning_fixture(state: Path, journal: Path, end: int) -> None:
    """A model in shadow as the learning job leaves it: registered a week ago, one checkpoint looked at and not
    passed, with the signals it scored and their outcomes. Invented figures."""
    models = state / "models"
    models.mkdir(parents=True, exist_ok=True)
    lineage, version = "m1:C=0.1", "m1-20260925-fixture0"
    (models / "current.json").write_text(json.dumps({"version": version, "lineage": lineage, "kind": "logistic",
                                                     "trained_at": _iso(end - 7 * 86_400),
                                                     "inputs": ["btc_above_sma50", "stop_pct", "volume_ratio"],
                                                     "sha256": "f1x7ure0" * 8, "features": "fixture00001",
                                                     "calibration": [0.94, -0.03], "cutoff": 0.31, "half_below": 0.36}))
    importance = [{"input": "btc_above_sma50", "weight": 0.31}, {"input": "stop_pct", "weight": -0.22},
                  {"input": "volume_ratio", "weight": 0.12}, {"input": "rsi", "weight": -0.07},
                  {"input": "is_trend", "weight": 0.05}, {"input": "hour_utc", "weight": 0.01}]
    for v, when, n in ((version, end - 7 * 86_400, 8873), ("m2-20260918-fixture9", end - 14 * 86_400, 8790)):
        (models / v).mkdir(exist_ok=True)
        (models / v / "card.json").write_text(json.dumps({
            "version": v, "kind": "logistic" if v == version else "lightgbm", "trained_at": _iso(when), "examples": n,
            "lineage": lineage if v == version else "m2:n_estimators=100,num_leaves=4", "importance": importance,
            "by_sleeve": {"trend": {"n": 4102, "mean_r": -0.071}, "break": {"n": 3644, "mean_r": -0.118},
                          "dip": {"n": 1127, "mean_r": -0.102}}}))
    (models / "promotion.json").write_text(json.dumps({
        "lineage": lineage, "version": version, "state": "shadow", "checkpoints": 1, "finished": 74, "next_checkpoint": 120,
        "since": _iso(end - 7 * 86_400),
        "past": {"m2:n_estimators=100,num_leaves=4": {"state": "shadow", "checkpoints": 0, "finished": 12,
                                                      "since": _iso(end - 14 * 86_400)}},
        "drift": {"score_psi": 0.062, "inputs": {}, "drifted": False},
        "looks": [{"checkpoint": 1, "signals": 60, "spread": 0.142, "lower": -0.318, "alpha": 0.05 / 6, "brier": 0.2231,
                   "brier_base": 0.2254, "passed": False, "t": _iso(end - 2 * 86_400)}]}))
    fold = {"log_loss": 0.5914, "log_loss_se": 0.0225, "kept": 7408, "kept_mean_r": -0.147, "dropped": 0,
            "dropped_mean_r": None, "spread": None, "spread_ci": None}
    (models / "last_train.json").write_text(json.dumps({
        "t": _iso(end - 7 * 86_400), "chosen": "m1", "decision": "DEC-0018", "examples": 8873, "pairs": 30, "effective_n": 2005.8,
        "win_rate": 0.344, "mean_r": -0.094, "attempt": 3, "m0": fold, "start": end - 737 * 86_400, "end": end - 7 * 86_400,
        "data_hash": "fixture0c0ffee00",
        "best": {"m1": {**fold, "settings": {"C": 0.1}, "log_loss": 0.5868, "kept": 4313, "kept_mean_r": -0.103, "dropped": 3095,
                        "dropped_mean_r": -0.207, "spread": 0.104, "spread_ci": [-0.113, 0.32]},
                 "m2": {**fold, "settings": {"n_estimators": 300, "num_leaves": 4}, "log_loss": 0.6077, "kept": 4666,
                        "kept_mean_r": -0.182, "dropped": 2742, "dropped_mean_r": -0.086, "spread": -0.096,
                        "spread_ci": [-0.346, 0.128]}},
        "importance": [{"input": "btc_above_sma50", "weight": 0.31}, {"input": "stop_pct", "weight": -0.22},
                       {"input": "volume_ratio", "weight": 0.12}]}))
    for k, (event, when) in enumerate((("lineage", end - 7 * 86_400), ("checkpoint", end - 2 * 86_400))):
        ledger.append(journal, {"id": f"model{k}", "kind": "model", "event": event, "t": _iso(when), "model": version,
                                "lineage": lineage})
    plan = [("trend", "AVAX/USD", 0.42, 1.9), ("break", "AVAX/USD", 0.38, 2.0), ("trend", "ADA/USD", 0.27, -1.0),
            ("break", "SOL/USD", 0.33, -1.0), ("dip", "LINK/USD", 0.24, 0.6), ("trend", "XRP/USD", 0.44, None)]
    for k, (sleeve, pair, score, r) in enumerate(plan):
        t = end - 14_400 * (30 - 4 * k)
        sid = f"{sleeve}|{pair}|{t - 14_400}"
        ledger.append(journal, {"id": f"sig{k}", "kind": "signal", "sid": sid, "sleeve": sleeve, "pair": pair, "bar": t - 14_400,
                                "t": _iso(t + 10), "taken": True, "why": [], "score": score, "cutoff": 0.31, "half_below": 0.36,
                                "model": version, "lineage": lineage, "inputs": {}})
        if r is not None:
            ledger.append(journal, {"id": f"out{k}", "kind": "outcome", "sid": sid, "sleeve": sleeve, "pair": pair,
                                    "t": _iso(t + 86_400), "exit_t": _iso(t + 50_000), "reason": "target" if r > 0 else "stop",
                                    "r": r})


def challengers_fixture(journal: Path, end: int) -> None:
    """Three challengers as the daily run records them: one that failed its backtest, one that passed and is live,
    one registered and not yet judged. Invented figures."""
    from wt.crypto import challengers, rules
    base = {"base": "break", "timeframe_min": 1440, "high_bars": 20, "stop_atr": 3.0, "target_atr": 6.0, "trail_atr": 2.0,
            "min_stop_pct": 2.0, "btc_filter": True, "volume_filter": True, "skip_held": False}
    figures = {"trades": 96, "trades_per_month": 4.0, "win_rate": 0.4375, "mean_r": 0.212, "ci_low": 0.031, "ci_high": 0.402,
               "profit_factor": 1.41, "dsr": 0.962, "max_drawdown_pct": -7.4, "return_pct": 19.8, "total_r": 20.35}
    plan = [({**base, "base": "trend", "timeframe_min": 240, "target_atr": "none", "trail_atr": 4.0}, "random", None, 8,
             {"passed": False, "failed_on": ["ci_not_above_zero_at_1.5x_slippage", "deflated_sharpe", "profit_factor"],
              "base": {**figures, "trades": 212, "trades_per_month": 8.83, "win_rate": 0.33, "mean_r": -0.118, "profit_factor": 0.84,
                       "dsr": 0.004, "max_drawdown_pct": -31.2},
              "stressed": {"mean_r": -0.131, "ci_low": -0.262, "ci_high": 0.018}, "control_p": 0.41,
              "costs_r": {"trades": 212, "cost_mean_r": 0.124, "cost_median_r": 0.118, "gross_mean_r": 0.006, "gross_se_r": 0.081}}),
            (base, "neighbour", "break", 9,
             {"passed": True, "failed_on": [], "base": figures,
              "stressed": {"mean_r": 0.198, "ci_low": 0.019, "ci_high": 0.389}, "control_p": 0.01,
              "confirm": {"passed": True, "trades": 88, "mean_r": 0.131, "profit_factor": 1.22},
              "costs_r": {"trades": 96, "cost_mean_r": 0.071, "cost_median_r": 0.066, "gross_mean_r": 0.283, "gross_se_r": 0.094}}),
            ({**base, "base": "dip", "stop_atr": 4.0, "target_atr": 8.0}, "random", None, 10, None)]
    for k, (dials, slot, of, n_trials, c1) in enumerate(plan):
        d = rules.canonical(dials)
        cid = rules.challenger_id(d)
        t = end - 86_400 * (8 if k < 2 else 1) + 60 * k
        common = {"kind": "challenger", "sleeve": cid}
        ledger.append(journal, {"id": f"ch{k}r", "event": "registered", "t": _iso(t), "dials": d, "rules": challengers.describe(d),
                                "slot": slot, "of": of, "week": challengers.week_of(t), "n_trials": n_trials, **common})
        if c1 is not None:
            ledger.append(journal, {"id": f"ch{k}c", "event": "c1", "t": _iso(t + 300), "span": [1728273600, 1791345600],
                                    "n_trials": n_trials, "controls": 100, "seed": 7, "data_hash": "fixture", **c1, **common})
            if c1["passed"]:
                ledger.append(journal, {"id": f"ch{k}a", "event": "admitted", "t": _iso(t + 301), **common})


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


def _desk(state: Path) -> Any:
    return dataclasses.replace(DESKS["crypto"], state_dir=state)


def build() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "crypto"
        state.mkdir()
        synthetic(state)
        desk = dataclasses.replace(DESKS["crypto"], state_dir=state, kill_file=state / "KILL",
                                   ledgers=(("crypto", state / "crypto_journal.jsonl"),),
                                   chain_flag=state / "chain-broken")
        from wt.ops import alerts, heartbeat
        alerts.ALERT_DIR = heartbeat.HEARTBEAT_DIR = Path(tmp) / "none"   # nothing from this machine's own state
        raw = snapshot.collect(NOW, desk)
        raw["jobs"] = {"last": {"crypto": {"status": "ok", "exit": 0, "started": "2026-10-02T20:00:10+00:00",
                                           "ended": "2026-10-02T20:00:19+00:00", "sha": "abc1234"},
                                "dashboard-crypto": {"status": "ok", "exit": 0, "started": "2026-10-02T19:46:00+00:00",
                                                     "ended": "2026-10-02T19:46:04+00:00", "sha": "abc1234"}},
                       "runs": []}
        raw["alerts"] = {"firing": []}
    san = publish.Sanitizer(lambda x: x, None, strict=False)
    snap = snapshot.build(raw, san, "20261002T200040Z-fixture", NOW)
    problems = snapshot.validate(snap)
    assert not problems, problems
    return snap


def main(argv: list[str]) -> int:
    text = json.dumps(build(), indent=1, sort_keys=True) + "\n"
    if "--check" in argv:
        if not FIXTURE.exists() or FIXTURE.read_text() != text:
            print(f"{FIXTURE.relative_to(ROOT)} is stale: run python scripts/gen_crypto_fixture.py")
            return 1
        return 0
    FIXTURE.write_text(text)
    print(f"wrote {FIXTURE.relative_to(ROOT)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
