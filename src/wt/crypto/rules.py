"""The tournament strategies (DEC-0015): HYP-0021 TREND, HYP-0022 BREAK, HYP-0023 DIP.

Pure functions on closed bars. The live desk (`wt.crypto.sleeves`) and the backtest call exactly these, so a rule
cannot mean one thing in research and another in a session. Every number comes from `config/crypto.yaml`,
`sleeves`; nothing here is tuned.

  entry(...)     is the newest closed bar a signal bar, and if not, which conditions failed
  levels(...)    the stop and target for a fill at a given price
  bar_exit(...)  what a newly closed bar does to an open position: raise the stop, leave on trend or on time
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from wt.crypto import indicators
from wt.crypto.data import Bar

NAMES = ("trend", "break", "dip")


@dataclass(frozen=True)
class Common:
    timeframe_min: int
    daily_min: int
    bars: int
    daily_bars: int
    atr_period: int
    stop_atr: float
    min_stop_pct: float
    max_stop_pct: float

    @classmethod
    def of(cls, c: dict[str, Any]) -> Common:
        return cls(int(c["timeframe_min"]), int(c["daily_min"]), int(c["bars"]), int(c["daily_bars"]),
                   int(c["atr_period"]), float(c["stop_atr"]), float(c["min_stop_pct"]), float(c["max_stop_pct"]))


@dataclass(frozen=True)
class Spec:
    """One sleeve as the engine runs it: a base rule, its numbers, its bar length, and its filters. The three
    registered sleeves are specs whose name is their base; a challenger (DEC-0016, 5) is a base rule with some
    dials turned."""
    name: str
    base: str                   # trend | break | dip
    c: Common
    p: dict[str, Any]
    hypothesis: str = ""
    btc_filter: bool = False    # only while Bitcoin's last daily close is above its 50-day average
    volume_filter: bool = False  # only when the signal bar's volume is above its 20-bar mean
    skip_held: bool = False     # not when another sleeve already holds the pair


def registered(cfg: dict[str, Any]) -> dict[str, Spec]:
    """The sleeves DEC-0015 registered, in their order."""
    sc = cfg["sleeves"]
    c = Common.of(sc["common"])
    return {n: Spec(n, n, c, dict(sc[n]), str(sc[n].get("hypothesis", ""))) for n in NAMES if n in sc}


DIALS = ("base", "timeframe_min", "high_bars", "stop_atr", "target_atr", "trail_atr", "min_stop_pct", "btc_filter",
         "volume_filter", "skip_held")


def canonical(dials: dict[str, Any]) -> dict[str, Any]:
    """A challenger's dials with the ones that do nothing for it set to None, so two settings that are the same
    strategy are the same challenger: the dip rule has no lookback high, a target makes the trail distance
    idle, and without a target there is only the trail."""
    d = {k: dials.get(k) for k in DIALS}
    if d["base"] == "dip":
        d["high_bars"] = None
    if d["target_atr"] in (None, "none"):
        d["target_atr"] = None
    else:
        d["trail_atr"] = None
    if d["base"] == "break":
        d["volume_filter"] = True                            # the breakout rule already requires it
    return d


def challenger_id(dials: dict[str, Any]) -> str:
    return "ch-" + hashlib.sha256(json.dumps(canonical(dials), sort_keys=True).encode()).hexdigest()[:8]


def challenger(cfg: dict[str, Any], dials: dict[str, Any]) -> Spec:
    """The spec of a challenger: the registered base rule with the dials' values put in place of its own."""
    d, sc = canonical(dials), cfg["sleeves"]
    base = str(d["base"])
    c = dataclasses.replace(Common.of(sc["common"]), timeframe_min=int(d["timeframe_min"]), stop_atr=float(d["stop_atr"]),
                            min_stop_pct=float(d["min_stop_pct"]))
    p = {k: v for k, v in sc[base].items() if k not in ("target_atr", "trail_atr", "hypothesis")}
    if d["high_bars"] is not None and "high_bars" in p:
        p["high_bars"] = int(d["high_bars"])
    if d["target_atr"] is not None:
        p["target_atr"] = float(d["target_atr"])
        p.pop("exit_below_ema", None)                        # a fixed target and stop: no trend exit
    else:
        p["trail_atr"] = float(d["trail_atr"])
    return Spec(challenger_id(d), base, c, p, f"challenger of {sc[base].get('hypothesis', base)}",
                bool(d["btc_filter"]), bool(d["volume_filter"]) and base != "break", bool(d["skip_held"]))


Entry = Callable[..., "tuple[bool, tuple[str, ...], float | None]"]


def evaluate(spec: Spec, bars: Sequence[Bar], daily: Sequence[Bar], market: dict[str, float | None],
             held_elsewhere: bool, entry_rule: Entry | None = None) -> tuple[bool, tuple[str, ...], float | None]:
    """A spec's verdict on the newest closed bar: its base rule, then its filters. A filter whose input is unknown
    does not pass: no data, no action."""
    fire, why, atr = (entry_rule or entry)(spec.base, bars, daily, spec.c, spec.p)
    more = list(why)
    if spec.btc_filter and market.get("btc_above_sma50") != 1.0:
        more.append("btc_not_in_uptrend")
    if spec.volume_filter:
        vols = [b.v for b in bars[-21:-1]]
        if len(vols) < 20 or not bars or bars[-1].v <= sum(vols) / len(vols):
            more.append("low_volume")
    if spec.skip_held and held_elsewhere:
        more.append("held_elsewhere")
    return fire and len(more) == len(why), tuple(more), atr


def _prior_high(bars: Sequence[Bar], n: int) -> float | None:
    """The highest high of the n bars before the newest one."""
    return max(b.h for b in bars[-(n + 1):-1]) if len(bars) >= n + 1 else None


def _trend(bars: Sequence[Bar], p: dict[str, Any]) -> list[str]:
    closes = [b.c for b in bars]
    fast, slow = indicators.ema(closes, int(p["ema_fast"])), indicators.ema(closes, int(p["ema_slow"]))
    high = _prior_high(bars, int(p["high_bars"]))
    if fast is None or slow is None or high is None:
        return ["no_history"]
    why = []
    if fast <= slow:
        why.append("no_uptrend")
    if closes[-1] <= fast:
        why.append("below_ema")
    if closes[-1] <= high:
        why.append("no_new_high")
    return why


def _break(bars: Sequence[Bar], p: dict[str, Any]) -> list[str]:
    high, n = _prior_high(bars, int(p["high_bars"])), int(p["volume_bars"])
    if high is None or len(bars) < n + 1:
        return ["no_history"]
    why = []
    if bars[-1].c <= high:
        why.append("no_new_high")
    if bars[-1].v <= sum(b.v for b in bars[-(n + 1):-1]) / n:
        why.append("low_volume")
    return why


def _dip(bars: Sequence[Bar], daily: Sequence[Bar], p: dict[str, Any]) -> list[str]:
    closes = [b.c for b in bars]
    look, period, ema_n = int(p["rsi_lookback"]), int(p["rsi_period"]), int(p["ema_period"])
    avg = indicators.sma([b.c for b in daily], int(p["daily_sma"]))
    now, before = indicators.ema(closes, ema_n), indicators.ema(closes[:-1], ema_n)
    lows = [indicators.rsi(closes[:len(closes) - k], period) for k in range(1, look + 1)]
    if avg is None or now is None or before is None or len(closes) < period + look + 1:
        return ["no_history"]
    why = []
    if daily[-1].c <= avg:
        why.append("below_daily_average")
    if not any(x is not None and x < float(p["rsi_below"]) for x in lows):      # a flat series has no RSI: no dip
        why.append("no_dip")
    if not (closes[-1] > now and closes[-2] <= before):
        why.append("not_reclaimed")
    return why


def entry(name: str, bars: Sequence[Bar], daily: Sequence[Bar], c: Common,
          p: dict[str, Any]) -> tuple[bool, tuple[str, ...], float | None]:
    """(the newest closed bar is a signal bar, the conditions that failed, ATR of that bar)."""
    if not bars:
        return False, ("no_history",), None
    why = {"trend": lambda: _trend(bars, p), "break": lambda: _break(bars, p), "dip": lambda: _dip(bars, daily, p)}[name]()
    atr = indicators.atr(bars, c.atr_period)
    if not bars[-1].traded:
        why.append("bar_untraded")
    if atr is None or atr <= 0:
        why.append("no_atr")
    return not why, tuple(why), atr


def levels(price: float, atr: float, c: Common, p: dict[str, Any]) -> tuple[float, float | None, str | None]:
    """(stop, target or None, the reason the signal is skipped or None) for a fill at `price`."""
    stop = price - c.stop_atr * atr
    pct = (price - stop) / price * 100 if price > 0 else 0.0
    target = price + float(p["target_atr"]) * atr if "target_atr" in p else None
    if pct < c.min_stop_pct:
        return stop, target, "stop_too_tight"
    if pct > c.max_stop_pct:
        return stop, target, "stop_too_wide"
    return stop, target, None


def bar_exit(name: str, bars: Sequence[Bar], entry_bar: int, stop: float, atr: float, high: float, c: Common,
             p: dict[str, Any]) -> tuple[str | None, float, float]:
    """What the newest closed bar does to a position whose signal bar opened at `entry_bar`.

    Returns (exit reason or None, the stop from now on, the highest high since entry). The stop never moves down.
    Decided on closed bars only, so the desk and the backtest decide the same thing from the same data.
    """
    since = [b for b in bars if b.t > entry_bar]
    if not since:
        return None, stop, high
    high = max(high, max(b.h for b in since))
    reason = None
    if "trail_atr" in p:
        stop = max(stop, high - float(p["trail_atr"]) * atr)
    if "exit_below_ema" in p:
        e = indicators.ema([b.c for b in bars], int(p["exit_below_ema"]))
        if e is not None and bars[-1].c < e:
            reason = "trend_exit"
    if reason is None and (bars[-1].t - entry_bar) // (c.timeframe_min * 60) >= int(p["time_stop_bars"]):
        reason = "time"
    return reason, stop, high
