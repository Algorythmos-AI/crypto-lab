"""Is this pair a market right now? A bar nobody traded in is a carried-forward price: indicators on it mean
nothing and a simulated fill against it is not evidence (DEC-0012, gate C0)."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from wt.crypto.data import Bar, Quote


@dataclass(frozen=True)
class Quality:
    tradable: bool
    reasons: tuple[str, ...]            # codes, never free text
    traded_share: float                 # of the bars given
    spread_pct: float | None
    last_bar_trades: int


def assess(bars: Sequence[Bar], quote: Quote | None, now: float, interval_min: int, cfg: dict[str, Any]) -> Quality:
    reasons: list[str] = []
    if not bars:
        return Quality(False, ("no_bars",), 0.0, None, 0)
    last = bars[-1]
    if now - (last.t + interval_min * 60) > interval_min * 60:
        reasons.append("bar_stale")                         # the newest closed bar is more than one interval old
    if last.n < int(cfg["min_trades_in_bar"]) or not last.traded:
        reasons.append("bar_untraded")
    if quote is None:
        reasons.append("no_quote")
    else:
        if quote.spread_pct > float(cfg["max_spread_pct"]):
            reasons.append("spread_wide")
        if now - quote.fetched > float(cfg["max_quote_age_s"]):
            reasons.append("quote_stale")
    share = sum(b.traded for b in bars) / len(bars)
    return Quality(not reasons, tuple(reasons), round(share, 4), None if quote is None else round(quote.spread_pct, 4),
                   last.n)
