"""Gate C1: the tournament sleeves on history, through the desk's own code (DEC-0015).

A backtest that re-implements a rule proves the re-implementation. This one builds the same `sleeves.Run` the
live cycle builds and calls the same `step_pair` and `finish`: the same entry rules, levels, sizing, limits,
latch, fees and exit resolution, on real `Book`s. What differs is where the bars come from and how fine they are:

  * strategy bars   4-hour and daily, aggregated from hourly history on UTC boundaries
  * fine bars       hourly, not 1-minute: the finest history there is. Stop first when a bar touches both.
  * the quote       the signal bar's close on both sides. Slippage and the fee are charged as on the desk.
  * the loss latch  cleared at the next UTC day. On the desk the owner clears it; history has no owner.

`run` returns the journal rows the desk would have written; `summary` and `verdict` judge them by the rules
DEC-0015 fixed before any result existed.
"""
from __future__ import annotations

import bisect
import dataclasses
import random
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from wt.backtest import stats
from wt.core.desk import DESKS
from wt.crypto import indicators, risk, rules, signals, sleeves
from wt.crypto.book import Book
from wt.crypto.data import Bar, PairInfo, Quote, aggregate, fill_grid

HOUR, DAY = 3600, 86_400
MIN_TRADES, MIN_PF, MIN_DSR, MAX_P = 30, 1.2, 0.95, 0.05      # DEC-0015, section 5


class _Quiet:
    """Alerts go nowhere in a backtest."""

    def fire(self, *a: Any, **k: Any) -> bool:
        return False

    def resolve(self, *a: Any, **k: Any) -> bool:
        return False


@dataclasses.dataclass
class Market:
    """One pair's history, ready to be read as of any moment, at every bar length a sleeve uses."""
    hourly: list[Bar]
    hourly_t: list[int]
    by_tf: dict[int, tuple[list[Bar], list[int]]]       # minutes -> (bars, their close times)

    @classmethod
    def of(cls, hourly: list[Bar], *tfs_s: int) -> Market:
        grid = fill_grid(sorted(hourly, key=lambda b: b.t), HOUR)
        by = {}
        for tf_s in sorted(set(tfs_s)):
            bars = aggregate(grid, HOUR, tf_s)
            by[tf_s // 60] = (bars, [b.t + tf_s for b in bars])
        return cls(grid, [b.t for b in grid], by)

    def closed(self, tf_min: int, now: int, keep: int) -> list[Bar]:
        """The last `keep` bars of a length that had closed by `now`."""
        bars, closes = self.by_tf[tf_min]
        i = bisect.bisect_right(closes, now)
        return bars[max(0, i - keep):i]

    # The 4-hour and daily series by name, as the tests and the report read them.
    @property
    def h4(self) -> list[Bar]:
        return self.by_tf[240][0]

    @property
    def h4_close(self) -> list[int]:
        return self.by_tf[240][1]

    @property
    def d1(self) -> list[Bar]:
        return self.by_tf[1440][0]

    @property
    def d1_close(self) -> list[int]:
        return self.by_tf[1440][1]


def run(cfg: dict[str, Any], hourly: dict[str, list[Bar]], infos: dict[str, PairInfo], start: int, end: int,
        slip_mult: float = 1.0, lim: risk.SleeveLimits | None = None,
        entry: Callable[..., tuple[bool, tuple[str, ...], float | None]] | None = None,
        names: list[str] | None = None, specs: dict[str, rules.Spec] | None = None) -> list[dict[str, Any]]:
    """Every 4-hour close in [start, end) as one cycle of the sleeves. `hourly` is keyed by our pair name.
    `specs` are the sleeves to run (the registered three when not given; `names` picks among them). `entry`
    replaces the entry rule (the random-entry control); everything after the entry is unchanged."""
    common = cfg["sleeves"]["common"]
    base = rules.Common.of(common)
    specs = specs or rules.registered(cfg)
    if names is not None:
        specs = {n: specs[n] for n in names}
    names = list(specs)
    step_s, daily_s = base.timeframe_min * 60, base.daily_min * 60
    tfs = {step_s, daily_s, *(s.c.timeframe_min * 60 for s in specs.values())}
    pairs: dict[str, str] = {k: v for k, v in common["pairs"].items() if k in hourly}
    costs = {**cfg["costs"], "slippage_bps": float(cfg["costs"]["slippage_bps"]) * slip_mult}
    lim = lim or risk.load_sleeve_limits(str(common["limits"]))
    markets = {k: Market.of(v, *tfs) for k, v in hourly.items() if k in pairs}
    closes = sorted({t for m in markets.values() for t in m.by_tf[base.timeframe_min][1] if start <= t < end})
    keep_daily = max([base.daily_bars, *(s.c.bars for s in specs.values() if s.c.timeframe_min == base.daily_min)])
    out: list[dict[str, Any]] = []

    def flush(book: Book, _path: Path, _journal: Path) -> None:
        out.extend(book.outbox)
        book.outbox = []

    with tempfile.TemporaryDirectory() as tmp:
        desk = dataclasses.replace(DESKS["crypto"], state_dir=Path(tmp), kill_file=Path(tmp) / "KILL",
                                   ledgers=(("crypto", Path(tmp) / "journal.jsonl"),),
                                   chain_flag=Path(tmp) / "chain-broken")
        books = {n: Book(Decimal(str(common["start_equity"])), Decimal(str(common["start_equity"]))) for n in names}
        extra = sleeves.extras(cfg, specs)
        last_day = ""
        for close in closes:
            now = float(close + 10)
            cycle = sleeves.Run(now, desk, specs, costs, cfg["quality"], lim, tuple(pairs), books, infos, extra,
                                _Quiet(), step_s=HOUR)       # type: ignore[arg-type]
            if entry is not None:
                cycle.entry = entry
            if (btc := markets.get("BTC/USD")) is not None:
                d = btc.closed(base.daily_min, close, base.daily_bars)
                if len(d) >= base.daily_bars:
                    cycle.market = signals.market(d)
            if cycle.day != last_day:                               # a new UTC day: the owner would have reset it
                last_day = cycle.day
                for n in names:
                    (risk.sleeve_dir(desk, n) / "latch").unlink(missing_ok=True)
            for name, kraken_pair in pairs.items():
                m = markets[name]
                bars = m.closed(base.timeframe_min, close, base.bars)
                daily = m.closed(base.daily_min, close, keep_daily)
                if len(bars) < base.bars or len(daily) < base.daily_bars or bars[-1].t + step_s != close:
                    continue                                        # not enough history yet, or no bar this close
                price = bars[-1].c

                def quote_of(price: float = price, now: float = now) -> Quote:
                    return Quote(price, price, now)

                def minutes_of(since: int, m: Market = m, close: int = close) -> list[Bar]:
                    lo = bisect.bisect_right(m.hourly_t, since)
                    hi = bisect.bisect_right(m.hourly_t, close - HOUR)
                    return m.hourly[lo:hi]

                def bars_of(tf: int, m: Market = m, close: int = close) -> list[Bar]:
                    return m.closed(tf, close, max(base.bars, keep_daily))
                try:
                    sleeves.step_pair(cycle, name, kraken_pair, bars_of, daily[-base.daily_bars:], quote_of, minutes_of)
                except Exception as e:  # noqa: BLE001 — recorded, as on the desk
                    cycle.failed[name] = e.__class__.__name__
            sleeves.finish(cycle, flush, save=False)
    return out


def random_entry(p_fire: dict[str, float], seed: int) -> Callable[..., tuple[bool, tuple[str, ...], float | None]]:
    """An entry "rule" that fires at random, as often as the real one did, with the real ATR. The control for
    "did the rule find anything, or would any entry with these exits have done as well"."""
    rng = random.Random(seed)

    def entry(name: str, bars: list[Bar], daily: list[Bar], c: rules.Common,
              p: dict[str, Any]) -> tuple[bool, tuple[str, ...], float | None]:
        atr = indicators.atr(bars, c.atr_period)
        fire = bool(bars) and bars[-1].traded and atr is not None and atr > 0 and rng.random() < p_fire.get(name, 0.0)
        return fire, (() if fire else ("random",)), atr
    return entry


def trades(rows: list[dict[str, Any]], sleeve: str) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("kind") == "exit" and r.get("sleeve") == sleeve
            and isinstance(r.get("r"), int | float)]


def fire_rate(rows: list[dict[str, Any]], sleeve: str) -> float:
    """Share of evaluated pair-bars on which the sleeve's rule fired."""
    seen = fired = 0
    for r in rows:
        if r.get("kind") == "sleeve" and r.get("sleeve") == sleeve:
            for v in (r.get("pairs") or {}).values():
                seen += 1
                fired += bool(v.get("fire"))
    return fired / seen if seen else 0.0


def summary(rows: list[dict[str, Any]], sleeve: str, start: int, end: int, n_trials: int,
            start_equity: float) -> dict[str, Any]:
    """The figures DEC-0015 asks for, for one sleeve. Trades per month comes first: it is reported before any
    return figure."""
    tr = trades(rows, sleeve)
    r = np.array([float(x["r"]) for x in tr], dtype=float)
    months = max((end - start) / (30.4375 * DAY), 1e-9)
    out: dict[str, Any] = {"sleeve": sleeve, "trades": len(tr), "trades_per_month": round(len(tr) / months, 2),
                           "signals": sum(1 for x in rows if x.get("sleeve") == sleeve and x.get("kind") in ("entry", "refused")),
                           "refused": sum(1 for x in rows if x.get("sleeve") == sleeve and x.get("kind") == "refused")}
    if len(r) == 0:
        return out
    days = [str(x.get("t", ""))[:10] for x in tr]
    s = stats.summarize(r, block="day", days=days)
    equity = [float(x["equity"]) for x in rows if x.get("kind") == "sleeve" and x.get("sleeve") == sleeve]
    peak, dd = start_equity, 0.0
    for e in equity:
        peak = max(peak, e)
        dd = min(dd, (e - peak) / peak * 100)
    pnl = sum(float(x["pnl"]) for x in tr)
    out.update(win_rate=round(s["win_rate"], 4), mean_r=round(s["expectancy_R"], 4),
               ci_low=round(s["ci95_expectancy"][0], 4), ci_high=round(s["ci95_expectancy"][1], 4),
               profit_factor=None if s["profit_factor"] == float("inf") else round(s["profit_factor"], 3),
               max_drawdown_r=round(s["max_drawdown_R"], 3), max_drawdown_pct=round(dd, 2),
               total_r=round(float(r.sum()), 3), pnl=round(pnl, 2), return_pct=round(pnl / start_equity * 100, 2),
               sharpe=round(s["per_trade_sharpe"], 4),
               dsr=round(stats.deflated_sharpe_prob(s["per_trade_sharpe"], len(r), n_trials, s["skew"], s["kurtosis"]), 4),
               by_reason={k: sum(1 for x in tr if x.get("reason") == k) for k in sorted({str(x.get("reason")) for x in tr})},
               by_pair={k: {"trades": sum(1 for x in tr if x.get("pair") == k),
                            "mean_r": round(float(np.mean([float(x["r"]) for x in tr if x.get("pair") == k])), 3)}
                        for k in sorted({str(x.get("pair")) for x in tr})})
    return out


def verdict(base: dict[str, Any], stressed: dict[str, Any], control_p: float | None) -> dict[str, Any]:
    """DEC-0015's falsification rules. `stressed` is the same sleeve at 1.5x slippage."""
    why = []
    if base.get("trades", 0) < MIN_TRADES:
        why.append("too_few_trades")
    if stressed.get("ci_low") is None or stressed["ci_low"] <= 0:
        why.append("ci_not_above_zero_at_1.5x_slippage")
    if base.get("dsr") is None or base["dsr"] <= MIN_DSR:
        why.append("deflated_sharpe")
    if control_p is None or control_p >= MAX_P:
        why.append("no_better_than_random_entry")
    if base.get("profit_factor") is not None and base["profit_factor"] < MIN_PF:
        why.append("profit_factor")
    return {"passed": not why, "failed_on": why}


def control(cfg: dict[str, Any], hourly: dict[str, list[Bar]], infos: dict[str, PairInfo], start: int, end: int,
            base_rows: list[dict[str, Any]], sleeve: str, runs: int, seed: int) -> tuple[float | None, list[float]]:
    """The random-entry control for one sleeve: `runs` histories with the same exits, sizing and costs and entries
    drawn at the sleeve's own rate. Returns (p-value of the real mean R, the control means)."""
    real = [float(x["r"]) for x in trades(base_rows, sleeve)]
    rate = fire_rate(base_rows, sleeve)
    if not real or rate <= 0:
        return None, []
    means = []
    for k in range(runs):
        rows = run(cfg, hourly, infos, start, end, entry=random_entry({sleeve: rate}, seed + k), names=[sleeve])
        rs = [float(x["r"]) for x in trades(rows, sleeve)]
        if rs:
            means.append(float(np.mean(rs)))
    if not means:
        return None, []
    return stats.random_control_pvalue(float(np.mean(real)), np.array(means)), means
