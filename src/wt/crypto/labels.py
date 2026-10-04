"""What would have happened to an entry at a bar's close: the synthetic label, with costs.

The original labels ignored fees. Here a label is decided on the price levels, as the strategy's own exits are, and
the net return after two taker fees and slippage on both sides is recorded beside it.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from wt.crypto.data import Bar
from wt.crypto.strategy import Params, find_exit


def label(entry_price: float, later: Sequence[Bar], p: Params, fee_pct: float, slip_bps: float) -> dict[str, Any]:
    """`later`: the bars after the entry bar, oldest first. `pending` until a level is hit or the time stop passes."""
    window = later[:p.time_stop_bars]
    stop, target = entry_price * (1 - p.stop_loss_pct / 100), entry_price * (1 + p.take_profit_pct / 100)
    x = find_exit(window, stop, target)
    if x is None and len(later) < p.time_stop_bars:
        return {"label": "pending", "exit_bars": None, "gross_pct": None, "net_pct": None}
    if x is None:
        name, price, n = "neither", window[-1].c, len(window)
    else:
        name, price = ("win" if x.reason == "target" else "loss"), x.price
        n = next(i for i, b in enumerate(window, 1) if b.t == x.t)
    gross = (price - entry_price) / entry_price * 100
    cost = 2 * fee_pct + 2 * slip_bps / 100
    return {"label": name, "exit_bars": n, "gross_pct": round(gross, 4), "net_pct": round(gross - cost, 4)}
