"""The crypto desk's market monitor: where each traded coin stands on its newest closed bars, how the coins move
together, the state of the market they share, and what the tournament's books hold between them.

Read from the bars the bar cycle already stores (`var/crypto/bars`) and from the books on disk. It never calls
the venue, and nothing here is read by a strategy, a gate, the model or the risk checks: it describes, it does not
decide. The definitions are the desk's own (`wt.crypto.indicators`, `wt.crypto.signals`), so a figure shown here is
the figure a rule would have computed on the same bar.

Every value is a number, a code or a pair name. A value its bars cannot support is None, never a filled-in number.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from wt.crypto import indicators, signals
from wt.crypto.book import Book
from wt.crypto.data import Bar

HIGH_BARS = (20, 30)                    # the look-backs of TREND's and BREAK's "new high" condition
EMA_FAST, EMA_SLOW = 20, 50
VOLUME_BARS = 20
DAY_BARS = 6                            # 4-hour bars in a day
CORR_BARS = 120                         # returns the correlation is measured over: 20 days of 4-hour bars
CORR_MIN = 30                           # fewer shared returns than this and a pair's correlation is not shown
REGIME_SMA = 50                         # daily bars: the average Bitcoin is read against
VOL_DAYS = 30
YEAR_DAYS = 365                         # the market never closes
# R of a closed trade, in bands with fixed edges so that one sleeve can be read against another.
R_EDGES = (-2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0)
UP, DOWN, MIXED = "up", "down", "mixed"


def _pct(a: float, b: float | None) -> float | None:
    return round((a / b - 1) * 100, 4) if b is not None and b > 0 else None


def contiguous(bars: Sequence[Bar], step_s: int) -> list[Bar]:
    """The newest run of bars with no hole in it. An indicator over a gap is not the indicator."""
    out = list(bars[-1:])
    for b in reversed(bars[:-1]):
        if out[0].t - b.t != step_s:
            break
        out.insert(0, b)
    return out


def pair_row(pair: str, bars: Sequence[Bar], tf_min: int, now: float) -> dict[str, Any] | None:
    """Where one coin stands on its newest closed bar. None when it has no bar at all."""
    step = tf_min * 60
    bars = contiguous(bars, step)
    if not bars:
        return None
    closes, last = [b.c for b in bars], bars[-1]
    vols = [b.v for b in bars[-VOLUME_BARS - 1:-1]]
    atr = indicators.atr(bars, 14)
    ema_fast, ema_slow = indicators.ema(closes, EMA_FAST), indicators.ema(closes, EMA_SLOW)
    rsi = indicators.rsi(closes, 14)

    def to_high(n: int) -> float | None:
        """The close against the highest high of the n bars before it: zero or above is a new high."""
        return _pct(last.c, max(b.h for b in bars[-n - 1:-1])) if len(bars) > n else None
    return {
        "pair": pair, "bar": last.t + step, "close": last.c, "bars": len(bars),
        # the newest bar closed more than two bars ago: the figures describe an older market
        "stale": now - (last.t + step) > 2 * step,
        "ret_1": _pct(last.c, bars[-2].c) if len(bars) >= 2 else None,
        "ret_day": _pct(last.c, bars[-1 - DAY_BARS].c) if len(bars) > DAY_BARS else None,
        "ret_30": signals.ret_30(bars),
        "to_high_20_pct": to_high(HIGH_BARS[0]), "to_high_30_pct": to_high(HIGH_BARS[1]),
        "dist_ema20_pct": _pct(last.c, ema_fast), "dist_ema50_pct": _pct(last.c, ema_slow),
        "above_ema20": None if ema_fast is None else last.c > ema_fast,
        "above_ema50": None if ema_slow is None else last.c > ema_slow,
        "rsi": None if rsi is None else round(rsi, 2),
        "atr_pct": round(atr / last.c * 100, 4) if atr is not None and last.c > 0 else None,
        "volume_ratio": round(last.v / (sum(vols) / len(vols)), 4) if len(vols) == VOLUME_BARS and sum(vols) > 0 else None,
    }


def ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The coins ordered by their 30-bar return, strongest first, each with its place. It is the one measure the
    desk has fixed for ordering coins (DEC-0025); a coin without it comes last and has no place."""
    have = sorted((r for r in rows if r["ret_30"] is not None), key=lambda r: (-r["ret_30"], r["pair"]))
    rest = sorted((r for r in rows if r["ret_30"] is None), key=lambda r: r["pair"])
    return [{**r, "rank": i} for i, r in enumerate(have, 1)] + [{**r, "rank": None} for r in rest]


def _returns(bars: Sequence[Bar], step_s: int) -> dict[int, float]:
    """Log return of each bar over the one before it, by bar time. A bar after a hole has none."""
    return {b.t: math.log(b.c / a.c) for a, b in zip(bars, bars[1:], strict=False)
            if b.t - a.t == step_s and a.c > 0 and b.c > 0}


def pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    """None when either side did not move: a flat series is correlated with nothing."""
    if len(x) != len(y) or len(x) < 2:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx, syy = sum((a - mx) ** 2 for a in x), sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return max(-1.0, min(1.0, sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / math.sqrt(sxx * syy)))


def correlation(series: Mapping[str, Sequence[Bar]], tf_min: int, bars: int = CORR_BARS) -> dict[str, Any] | None:
    """How the coins' bar-to-bar returns moved together over the newest `bars` bars. Each pair of coins is measured
    on the bars both have; with fewer than CORR_MIN of them its cell is None. `mean` is the average over the
    pairs of different coins that have a value: the nearer one, the less the coins diversify each other."""
    step = tf_min * 60
    rets = {p: _returns(list(b)[-bars - 1:], step) for p, b in series.items()}
    pairs = [p for p in series if rets[p]]
    if len(pairs) < 2:
        return None
    cells: dict[tuple[str, str], float | None] = {}
    for i, a in enumerate(pairs):
        for b in pairs[i + 1:]:
            shared = sorted(rets[a].keys() & rets[b].keys())
            c = pearson([rets[a][t] for t in shared], [rets[b][t] for t in shared]) if len(shared) >= CORR_MIN else None
            cells[a, b] = cells[b, a] = None if c is None else round(c, 3)
    off = [v for (a, b), v in cells.items() if a < b and v is not None]
    return {"bars": bars, "pairs": pairs,
            "rows": [{"pair": a, "with": [1.0 if a == b else cells[a, b] for b in pairs]} for a in pairs],
            "mean": round(statistics.fmean(off), 3) if off else None,
            "low": min(off) if off else None, "high": max(off) if off else None}


def regime(btc_daily: Sequence[Bar], rows: Sequence[Mapping[str, Any]], now: float) -> dict[str, Any]:
    """The market the coins share, described by three readings the desk already uses as inputs: Bitcoin against its
    50-day average, how many of the traded coins are above their own 50-bar average, and how much Bitcoin has
    been moving. The code is "up" when Bitcoin is above its average and at least half the coins are above theirs,
    "down" when neither holds, "mixed" otherwise, and None when either reading is missing."""
    day = 86_400
    daily = contiguous(btc_daily, day)
    closes = [b.c for b in daily]
    sma = indicators.sma(closes, REGIME_SMA)
    moves = [math.log(b / a) for a, b in zip(closes[-VOL_DAYS - 1:], closes[-VOL_DAYS:], strict=False) if a > 0 and b > 0]
    fresh = [r for r in rows if not r.get("stale")]
    above = [r["above_ema50"] for r in fresh if r.get("above_ema50") is not None]
    rising = [r["ret_30"] > 0 for r in fresh if r.get("ret_30") is not None]
    breadth = sum(above) / len(above) if above else None
    btc_up = None if sma is None or not closes else closes[-1] > sma
    code = None
    if btc_up is not None and breadth is not None:
        code = UP if btc_up and breadth >= 0.5 else DOWN if not btc_up and breadth < 0.5 else MIXED
    return {
        "code": code,
        "btc_close": closes[-1] if closes else None, "btc_sma50": None if sma is None else round(sma, 2),
        "btc_vs_sma50_pct": _pct(closes[-1], sma) if closes else None,
        "btc_ret_30d": _pct(closes[-1], closes[-1 - VOL_DAYS]) if len(closes) > VOL_DAYS else None,
        # the standard deviation of Bitcoin's daily log returns over 30 days, as a yearly percentage
        "vol_30d_pct": round(statistics.stdev(moves) * math.sqrt(YEAR_DAYS) * 100, 2) if len(moves) == VOL_DAYS else None,
        "daily_bar": daily[-1].t + day if daily else None,
        "stale": bool(daily) and now - (daily[-1].t + day) > 2 * day,
        "pairs": len(fresh), "above_ema50": sum(above) if above else None, "breadth": None if breadth is None else round(breadth, 4),
        "rising": sum(rising) if rising else None,
        "rising_share": round(sum(rising) / len(rising), 4) if rising else None,
    }


def view(bars_dir: Path, pairs: Mapping[str, str], tf_min: int, daily_min: int, now: float,
         read: Any) -> dict[str, Any] | None:
    """The monitor from the stored bars of every traded pair. `pairs` maps our pair name to the venue's; `read`
    is the desk's own bar reader. None when no pair has a stored bar (a desk that has not run its sleeves yet)."""
    series = {name: read(bars_dir / f"{venue}-{tf_min}m.jsonl") for name, venue in pairs.items()}
    rows = [r for name, bars in series.items() if (r := pair_row(name, bars, tf_min, now)) is not None]
    if not rows:
        return None
    btc = pairs.get("BTC/USD")
    daily = read(bars_dir / f"{btc}-{daily_min}m.jsonl") if btc else []
    return {"tf_min": tf_min, "bar": max(r["bar"] for r in rows), "regime": regime(daily, rows, now),
            "pairs": ranked(rows), "correlation": correlation({p: b for p, b in series.items() if b}, tf_min)}


def exposure(books: Mapping[str, Book], marks: Mapping[str, float]) -> dict[str, Any]:
    """What the tournament's books hold between them, by coin and by book, valued at the last price the cycle
    read (a position with no such price is valued at its entry). `risk` is the loss if every stop were hit at
    its price. Shares are of the books' equity together; the books are never pooled, so this is a reading of
    how concentrated the desk is, not an account."""
    equity = float(sum((b.equity(dict(marks)) for b in books.values()), Decimal(0)))
    coins: dict[str, dict[str, Any]] = {}
    per_book = []
    for name, book in books.items():
        value = at_risk = 0.0
        for held in book.positions.values():
            price = marks.get(held.pair, float(held.entry_price))
            notional = float(held.qty) * price
            risk = max(0.0, float(held.qty) * (price - float(held.stop)))
            c = coins.setdefault(held.pair, {"pair": held.pair, "books": [], "notional": 0.0, "risk": 0.0, "unrealised": 0.0})
            c["books"].append(name)
            c["notional"] += notional
            c["risk"] += risk
            c["unrealised"] += float(held.qty) * (price - float(held.entry_price)) - float(held.entry_fee)
            value, at_risk = value + notional, at_risk + risk
        own = float(book.equity(dict(marks)))
        per_book.append({"name": name, "equity": own, "positions": len(book.positions), "notional": value, "risk": at_risk,
                         "notional_pct": value / own * 100 if own > 0 else None,
                         "risk_pct": at_risk / own * 100 if own > 0 else None})
    gross = sum(c["notional"] for c in coins.values())
    rows = sorted(coins.values(), key=lambda c: (-c["notional"], c["pair"]))
    return {
        "equity": equity, "gross": gross, "gross_pct": gross / equity * 100 if equity > 0 else None,
        "risk": sum(c["risk"] for c in rows), "largest_share_pct": rows[0]["notional"] / gross * 100 if gross > 0 else None,
        "coins": [{**c, "books": sorted(c["books"]), "equity_pct": c["notional"] / equity * 100 if equity > 0 else None,
                   "share_pct": c["notional"] / gross * 100 if gross > 0 else None,
                   "risk_pct": c["risk"] / equity * 100 if equity > 0 else None} for c in rows],
        "books": per_book,
    }


def r_bands(rs: Sequence[float]) -> list[dict[str, Any]]:
    """Closed trades counted by their result in R, in fixed bands. A band includes its lower edge; the first has
    no lower edge and the last no upper. Empty when there is no trade."""
    if not rs:
        return []
    edges: list[float | None] = [None, *R_EDGES, None]
    counts = [0] * (len(edges) - 1)
    for r in rs:
        counts[sum(1 for e in R_EDGES if r >= e)] += 1
    return [{"lo": edges[i], "hi": edges[i + 1], "n": counts[i]} for i in range(len(counts))]


def r_summary(rs: Sequence[float]) -> dict[str, Any]:
    """Wins against losses in R: how many of each, what each is worth on average, and the two together."""
    wins, losses = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    mean_win = statistics.fmean(wins) if wins else None
    mean_loss = statistics.fmean(losses) if losses else None
    return {"mean_win_r": mean_win, "mean_loss_r": mean_loss,
            "payoff": mean_win / -mean_loss if mean_win is not None and mean_loss is not None and mean_loss < 0 else None,
            "best_r": max(rs) if rs else None, "worst_r": min(rs) if rs else None,
            "median_r": statistics.median(rs) if rs else None, "r_bands": r_bands(rs)}
