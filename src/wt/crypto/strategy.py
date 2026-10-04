"""rsi_vwap_ema_pullback (HYP-0020): long only, on closed bars, with the exits the original bot never had.

Entry: close above the UTC session VWAP, close above EMA, RSI below the threshold. All three, on a bar that traded.
Exit: a fixed stop, a fixed target, or a time stop, whichever comes first.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from wt.crypto import indicators
from wt.crypto.data import Bar


@dataclass(frozen=True)
class Params:
    rsi_period: int
    rsi_below: float
    ema_period: int
    stop_loss_pct: float
    take_profit_pct: float
    time_stop_bars: int

    @classmethod
    def of(cls, cfg: dict[str, Any]) -> Params:
        return cls(int(cfg["rsi_period"]), float(cfg["rsi_below"]), int(cfg["ema_period"]),
                   float(cfg["stop_loss_pct"]), float(cfg["take_profit_pct"]), int(cfg["time_stop_bars"]))


@dataclass(frozen=True)
class Reading:
    close: float
    rsi: float | None
    ema: float | None
    vwap: float | None


def read(bars: Sequence[Bar], p: Params) -> Reading:
    closes = [b.c for b in bars]
    return Reading(closes[-1], indicators.rsi(closes, p.rsi_period), indicators.ema(closes, p.ema_period),
                   indicators.session_vwap(bars))


def entry(r: Reading, p: Params) -> tuple[bool, tuple[str, ...]]:
    """(all conditions hold, codes of the ones that don't). A missing indicator is a failed condition."""
    why = []
    if r.vwap is None:
        why.append("no_vwap")
    elif r.close <= r.vwap:
        why.append("below_vwap")
    if r.ema is None:
        why.append("no_ema")
    elif r.close <= r.ema:
        why.append("below_ema")
    if r.rsi is None:
        why.append("no_rsi")
    elif r.rsi >= p.rsi_below:
        why.append("rsi_high")
    return not why, tuple(why)


@dataclass(frozen=True)
class Exit:
    reason: str                 # stop | target
    price: float
    t: int                      # open time of the bar it happened in


def find_exit(bars: Sequence[Bar], stop: float, target: float) -> Exit | None:
    """The first bar, in time order, that reaches a level. Bars nobody traded in are skipped.

    - Stop: touched or gapped through; a gap fills at the bar's open, which is worse than the stop.
    - Target: only when the bar trades through it (a touch is not a fill), at the target.
    - Both in one bar: the stop. The order inside a bar is unknown, so the worse outcome is taken.
    """
    for b in bars:
        if not b.traded:
            continue
        if b.l <= stop:
            return Exit("stop", min(stop, b.o), b.t)
        if b.h > target:
            return Exit("target", target, b.t)
    return None
