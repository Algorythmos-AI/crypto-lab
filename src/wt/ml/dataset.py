"""The training set (DEC-0016, 3): every signal of the registered rules over history, with its inputs and the
outcome the sleeve's own exits give it.

The rules, the inputs and the outcome are the desk's own functions (`wt.crypto.rules`, `wt.crypto.signals`), so an
example from history is the same thing as a signal the desk records. Each signal is followed on its own, whether
or not a book would have had room for it: the model's question is "is this signal worth taking", not "what did
the portfolio do".
"""
from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np

from wt.crypto import rules, signals
from wt.crypto.data import Bar, aggregate, fill_grid

HOUR = 3600


@dataclass(frozen=True)
class Example:
    sid: str
    sleeve: str
    pair: str
    t: int                      # when the signal bar closed: the moment the signal existed
    exit_t: int                 # when its outcome was known
    inputs: dict[str, float | None]
    r: float
    reason: str

    @property
    def y(self) -> int:
        return int(self.r > 0)


@dataclass(frozen=True)
class Scan:
    """Every signal of the registered rules over a history, with the series it was found on."""
    found: list[tuple[str, str, int, int, float, float, float | None, float]]   # sleeve, pair, bar index, close time,
    fired: dict[tuple[str, int], int]                                           # price, stop, target, ATR
    h4: dict[str, list[Bar]]
    d1: dict[str, list[Bar]]
    d1_close: dict[str, list[int]]
    grids: dict[str, list[Bar]]
    fine_t: dict[str, list[int]]


def scan(cfg: dict[str, Any], hourly: dict[str, list[Bar]], start: int, end: int) -> Scan:
    """The signals in [start, end) on every pair of `hourly`, with a market entry's levels, and how many of the
    traded pairs fired with each. One scan for the training set and for any study of the same signals."""
    sc = cfg["sleeves"]
    c = rules.Common.of(sc["common"])
    tf_s, daily_s = c.timeframe_min * 60, c.daily_min * 60
    names = [n for n in rules.NAMES if n in sc]
    slip = float(cfg["costs"]["slippage_bps"]) / 10_000
    grids = {k: fill_grid(sorted(v, key=lambda b: b.t), HOUR) for k, v in hourly.items() if v}
    h4 = {k: aggregate(g, HOUR, tf_s) for k, g in grids.items()}
    d1 = {k: aggregate(g, HOUR, daily_s) for k, g in grids.items()}
    fine_t = {k: [b.t for b in g] for k, g in grids.items()}
    d1_close = {k: [b.t + daily_s for b in v] for k, v in d1.items()}
    traded = set(sc["common"]["pairs"])
    fired: dict[tuple[str, int], int] = {}
    found: list[tuple[str, str, int, int, float, float, float | None, float]] = []
    for pair, bars in h4.items():
        for i in range(c.bars - 1, len(bars)):
            close = bars[i].t + tf_s
            if not start <= close < end:
                continue
            j = bisect.bisect_right(d1_close[pair], close)
            if j < c.daily_bars:
                continue
            window, daily = bars[i + 1 - c.bars:i + 1], d1[pair][j - c.daily_bars:j]
            if any(b.t - a.t != tf_s for a, b in zip(window, window[1:], strict=False)):
                continue
            for n in names:
                fire, _, atr = rules.entry(n, window, daily, c, sc[n])
                if fire and pair in traded:
                    fired[(n, close)] = fired.get((n, close), 0) + 1     # as the desk counts: before the levels
                if not fire or atr is None:
                    continue
                price = window[-1].c * (1 + slip)
                stop, target, skip = rules.levels(price, atr, c, sc[n])
                if skip is None:
                    found.append((n, pair, i, close, price, stop, target, atr))
    return Scan(found, fired, h4, d1, d1_close, grids, fine_t)


def build(cfg: dict[str, Any], hourly: dict[str, list[Bar]], start: int, end: int) -> list[Example]:
    """Every signal in [start, end) on every pair of `hourly`, oldest first. A signal whose outcome is not known
    by the end of the data is left out: it has no label yet.

    `breadth` is what the desk records (`wt.crypto.sleeves.finish`): how many of the *traded* pairs met the
    sleeve's rule on that bar, whether or not their stop distance let them trade. A pair used for training only
    is never counted, so the input means the same thing in history as it does live; on such a pair's own signal
    it can be 0, which a live signal never sees."""
    sc = cfg["sleeves"]
    c = rules.Common.of(sc["common"])
    tf_s = c.timeframe_min * 60
    got = scan(cfg, hourly, start, end)
    found, fired, h4, d1, d1_close, grids, fine_t = (got.found, got.fired, got.h4, got.d1, got.d1_close, got.grids,
                                                     got.fine_t)
    btc = d1.get("BTC/USD")
    out: list[Example] = []
    for n, pair, i, close, price, stop, target, atr in found:
        bars = h4[pair]
        horizon = int(sc[n]["time_stop_bars"]) + 2
        later = bars[max(0, i + 1 - c.bars):i + 1 + horizon]
        lo = bisect.bisect_left(fine_t[pair], close)
        hi = bisect.bisect_right(fine_t[pair], close + horizon * tf_s)
        res = signals.outcome(n, price, stop, target, atr, bars[i].t, later, grids[pair][lo:hi], c, sc[n],
                              cfg["costs"], HOUR)
        if res is None:
            continue
        context: dict[str, float | None] = {}
        if btc is not None:
            k = bisect.bisect_right(d1_close["BTC/USD"], close)
            if k >= c.daily_bars:
                context = signals.market(btc[k - c.daily_bars:k])
        x = signals.inputs(bars[i + 1 - c.bars:i + 1], price, stop, atr, None, context, False, c.timeframe_min)
        x["breadth"] = float(fired.get((n, close), 0))
        out.append(Example(f"{n}|{pair}|{bars[i].t}", n, pair, close, int(res["exit_t"]), x, float(res["r"]),
                           str(res["reason"])))
    return sorted(out, key=lambda e: (e.t, e.sleeve, e.pair))


def uniqueness(examples: list[Example], step_s: int) -> np.ndarray:
    """A weight per example: how much of its holding period it has to itself (Lopez de Prado's average
    uniqueness). Trades open at the same time share one stretch of market, so each counts for less."""
    if not examples:
        return np.zeros(0)
    t0 = min(e.t for e in examples)
    span = []
    for e in examples:
        a = (e.t - t0) // step_s
        # Up to the step its outcome falls in, and never empty: a trade stopped out inside its first bar still
        # shared that bar with every other trade open then. (An empty span once gave such a trade full weight,
        # twenty times a normal one.)
        span.append((a, max(a + 1, -(-(e.exit_t - t0) // step_s))))
    count = np.zeros(max(b for _, b in span) + 1)
    for a, b in span:
        count[a:b] += 1
    w = np.array([float(np.mean(1.0 / count[a:b])) for a, b in span])
    return w / w.mean()


def effective_n(w: np.ndarray) -> float:
    """How many equally weighted examples the weights are worth (Kish)."""
    return float(w.sum() ** 2 / (w ** 2).sum()) if len(w) and (w ** 2).sum() > 0 else 0.0


def matrix(examples: list[Example], names: list[str]) -> np.ndarray:
    """Inputs as numbers, NaN where an input is missing. The sleeve is three 0/1 columns named `is_<sleeve>`."""
    rows = []
    for e in examples:
        rows.append([float(e.sleeve == n[3:]) if n.startswith("is_") else
                     (float("nan") if e.inputs.get(n) is None else float(e.inputs[n])) for n in names])    # type: ignore[arg-type]
    return np.array(rows, dtype=float).reshape(len(examples), len(names))


def usable_inputs(examples: list[Example]) -> list[str]:
    """The inputs a model may use: those history can supply and that vary. Spread is not in candle history and a
    signal followed on its own is never "held elsewhere", so neither can be learned from history."""
    names = [*signals.INPUTS, *(f"is_{n}" for n in rules.NAMES)]
    x = matrix(examples, names)
    return [n for k, n in enumerate(names) if np.isfinite(x[:, k]).mean() > 0.5 and np.nanstd(x[:, k]) > 0]


def data_hash(examples: list[Example]) -> str:
    h = hashlib.sha256()
    for e in examples:
        h.update(f"{e.sid},{e.t},{e.exit_t},{e.r},{sorted(e.inputs.items())}\n".encode())
    return h.hexdigest()[:16]
