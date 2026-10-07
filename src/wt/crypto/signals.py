"""What is recorded about every signal, and how a signal is followed to its outcome (DEC-0016, 2).

A signal is a bar on which a sleeve's rule was met, whether or not it was bought. Its row carries the inputs
`config/crypto.yaml` lists under `learning.inputs`, as they stood on that bar, and the levels a trade at that
moment has. Rows go into the desk's own hash-chained journal (kind `signal`), so they are verified, backed up and
anchored with everything else.

`outcome` follows one signal through later bars with the sleeve's own exit rules and costs. It is how a signal
that was refused or skipped still gets a result (a shadow trade), and how history is labelled for training: one
definition for both.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from wt.crypto import indicators, rules
from wt.crypto.data import Bar
from wt.crypto.strategy import find_exit

INPUTS = ("stop_pct", "atr_pct", "dist_ema20_pct", "dist_ema50_pct", "volume_ratio", "rsi", "ret_30",
          "btc_above_sma50", "btc_ret_1d", "breadth", "held_elsewhere", "spread_pct", "hour_utc", "day_of_week")


def _pct(a: float, b: float | None) -> float | None:
    return round((a / b - 1) * 100, 4) if b else None


def market(btc_daily: Sequence[Bar]) -> dict[str, float | None]:
    """The state of the wider market on a bar, from Bitcoin's closed daily bars."""
    closes = [b.c for b in btc_daily]
    avg = indicators.sma(closes, 50)
    return {"btc_above_sma50": None if avg is None else float(closes[-1] > avg),
            "btc_ret_1d": _pct(closes[-1], closes[-2]) if len(closes) >= 2 else None}


def inputs(bars: Sequence[Bar], price: float, stop: float, atr: float, spread_pct: float | None,
           context: dict[str, float | None], held_elsewhere: bool, tf_min: int) -> dict[str, float | None]:
    """The inputs of a signal on the newest bar of `bars`. One definition for the recorder, the backtest and
    training. An input its data cannot support is None, never a filled-in number. `breadth` is set by the caller
    once every pair of the cycle has been evaluated."""
    closes, last = [b.c for b in bars], bars[-1]
    vols = [b.v for b in bars[-21:-1]]
    close_t = dt.datetime.fromtimestamp(last.t + tf_min * 60, dt.UTC)
    out: dict[str, float | None] = {
        "stop_pct": round((price - stop) / price * 100, 4) if price > 0 else None,
        "atr_pct": round(atr / last.c * 100, 4) if last.c > 0 else None,
        "dist_ema20_pct": _pct(last.c, indicators.ema(closes, 20)),
        "dist_ema50_pct": _pct(last.c, indicators.ema(closes, 50)),
        "volume_ratio": round(last.v / (sum(vols) / len(vols)), 4) if len(vols) == 20 and sum(vols) > 0 else None,
        "rsi": (lambda r: None if r is None else round(r, 2))(indicators.rsi(closes, 14)),
        "ret_30": _pct(last.c, closes[-31]) if len(closes) >= 31 else None,
        "btc_above_sma50": context.get("btc_above_sma50"), "btc_ret_1d": context.get("btc_ret_1d"),
        "breadth": None, "held_elsewhere": float(held_elsewhere),
        "spread_pct": None if spread_pct is None else round(spread_pct, 4),
        "hour_utc": float(close_t.hour), "day_of_week": float(close_t.weekday())}
    assert tuple(out) == INPUTS
    return out


def outcome(sleeve: str, price: float, stop: float, target: float | None, atr: float, signal_bar: int,
            bars: Sequence[Bar], fine: Sequence[Bar], c: rules.Common, p: dict[str, Any], costs: dict[str, Any],
            fine_s: int, *, after: int | None = None, entry_fee_pct: float | None = None,
            target_fee_pct: float | None = None) -> dict[str, Any] | None:
    """Follow one signal, entered at `price` when its bar closed, until the sleeve's rules take it out.

    `bars` are the strategy bars (they may start before the signal bar: the trend exit needs their history) and
    `fine` the finer bars the stop and target are resolved on, both oldest first. Stop first when a fine bar
    touches both, a gap fills at the open, a target must be traded through: `find_exit`, as on the desk. Returns
    None while the signal has not finished within the bars given.

    R is the result per unit, after the fee on both sides and slippage on a market exit, over the distance from
    the entry to the stop the trade opened with.

    The keyword arguments are for an entry that is not the desk's market order (`limit_outcome`): `after` is the
    open time of the fine bar the entry was filled in (only later fine bars are examined), and the two fees
    replace the taker fee on the entry and on an exit at the target. Without them nothing changes.
    """
    tf_s, unit = c.timeframe_min * 60, price - stop
    if unit <= 0:
        return None
    fee, slip = float(costs["taker_fee_pct"]) / 100, float(costs["slippage_bps"]) / 10_000
    level, high = stop, price
    checked = signal_bar + tf_s - fine_s                    # the fine bar that opens at the signal bar's close is first
    if after is not None:
        checked = after
    top = float("inf") if target is None else target

    def done(reason: str, px: float, t: int, market_exit: bool) -> dict[str, Any]:
        out = px * (1 - slip) if market_exit else px
        if entry_fee_pct is None and target_fee_pct is None:
            net = out - price - fee * (price + out)
        else:
            fee_in = fee if entry_fee_pct is None else entry_fee_pct / 100
            fee_out = fee if market_exit or target_fee_pct is None else target_fee_pct / 100
            net = out - price - fee_in * price - fee_out * out
        return {"reason": reason, "exit_t": t, "exit_price": round(out, 8), "r": round(net / unit, 4),
                "net_pct": round(net / price * 100, 4), "held_bars": max(0, (t - signal_bar - tf_s) // tf_s)}

    for i, b in enumerate(bars):
        if b.t <= signal_bar:
            continue
        close = b.t + tf_s
        window = [f for f in fine if checked < f.t and f.t + fine_s <= close]
        x = find_exit(window, level, top)
        if x is not None:
            return done(x.reason, x.price, x.t + fine_s, x.reason == "stop")
        if window:
            checked = window[-1].t
        reason, level, high = rules.bar_exit(sleeve, bars[max(0, i + 1 - c.bars):i + 1], signal_bar, level, atr, high, c, p)
        if reason is not None:
            return done(reason, b.c, close, True)
    return None


def limit_outcome(sleeve: str, limit: float, atr: float, signal_bar: int, bars: Sequence[Bar], fine: Sequence[Bar],
                  c: rules.Common, p: dict[str, Any], costs: dict[str, Any], fine_s: int, maker_fee_pct: float,
                  wait_bars: int = 1) -> dict[str, Any] | None:
    """A signal entered by a buy limit resting at `limit` from the signal bar's close (DEC-0020), followed to its
    outcome. A study of the registered rules' signals; the desk does not enter this way.

    - Filled at `limit`, with no slippage, by the first traded fine bar within `wait_bars` strategy bars whose low
      is below it. A touch does not fill. No such bar: `{"filled": False}`, a missed signal.
    - The stop and target are the sleeve's own, from the fill price and the signal's ATR; a stop distance the
      sleeve would skip gives `{"filled": False, "skipped": reason}`.
    - If the fill bar's own low reaches the stop, the trade is stopped out in it. A target is never credited in
      the fill bar: its high may have come before the fill.
    - Afterwards it is `outcome`, with the maker fee on the entry and on an exit at the target.

    None while the fill window or the outcome is not yet covered by the bars given.
    """
    tf_s = c.timeframe_min * 60
    start, end = signal_bar + tf_s, signal_bar + tf_s + wait_bars * tf_s
    window = [f for f in fine if start <= f.t < end]
    hit = next((f for f in window if f.traded and f.l < limit), None)
    if hit is None:
        covered = bool(fine) and fine[-1].t + fine_s >= end
        return {"filled": False} if covered else None
    stop, target, skip = rules.levels(limit, atr, c, p)
    if skip is not None:
        return {"filled": False, "skipped": skip}
    if hit.l <= stop:
        fee, slip = float(costs["taker_fee_pct"]) / 100, float(costs["slippage_bps"]) / 10_000
        out = min(stop, hit.o) * (1 - slip)
        net = out - limit - maker_fee_pct / 100 * limit - fee * out
        res: dict[str, Any] | None = {"reason": "stop", "exit_t": hit.t + fine_s, "exit_price": round(out, 8),
                                      "r": round(net / (limit - stop), 4), "net_pct": round(net / limit * 100, 4),
                                      "held_bars": 0}
    else:
        res = outcome(sleeve, limit, stop, target, atr, signal_bar, bars, fine, c, p, costs, fine_s, after=hit.t,
                      entry_fee_pct=maker_fee_pct, target_fee_pct=maker_fee_pct)
    return None if res is None else {**res, "filled": True, "fill_t": hit.t + fine_s, "price": limit, "stop": stop}
