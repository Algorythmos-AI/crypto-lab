"""Indicators on closed bars. Every function returns None when its input cannot support a value (too few bars,
no trades, a flat series), so the strategy can tell "no signal" from "no information"."""
from __future__ import annotations

from collections.abc import Sequence

from wt.crypto.data import Bar

DAY_S = 86_400


def ema(values: Sequence[float], n: int) -> float | None:
    """Exponential mean seeded with the simple mean of the first n values."""
    if n <= 0 or len(values) < n:
        return None
    k, e = 2 / (n + 1), sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def sma(values: Sequence[float], n: int) -> float | None:
    """Simple mean of the last n values."""
    return sum(values[-n:]) / n if n > 0 and len(values) >= n else None


def rsi(closes: Sequence[float], n: int = 14) -> float | None:
    """Wilder's RSI. None on a series with no movement at all: 0/0 is not 50."""
    if len(closes) < n + 1:
        return None
    d = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    gain, loss = sum(max(x, 0.0) for x in d[:n]) / n, sum(max(-x, 0.0) for x in d[:n]) / n
    for x in d[n:]:
        gain, loss = (gain * (n - 1) + max(x, 0.0)) / n, (loss * (n - 1) + max(-x, 0.0)) / n
    if gain == 0 and loss == 0:
        return None
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def session_vwap(bars: Sequence[Bar]) -> float | None:
    """Volume-weighted price of the last bar's UTC day, from bars that traded. None before the day's first trade."""
    if not bars:
        return None
    day = bars[-1].t // DAY_S
    pv = v = 0.0
    for b in bars:
        if b.t // DAY_S == day and b.traded:
            pv, v = pv + b.vwap * b.v, v + b.v
    return pv / v if v > 0 else None


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[float, float, float] | None:
    """(line, signal, histogram)."""
    if len(closes) < slow + signal:
        return None
    line = [f - s for i in range(slow, len(closes) + 1)
            if (f := ema(closes[:i], fast)) is not None and (s := ema(closes[:i], slow)) is not None]
    sig = ema(line, signal)
    return None if sig is None else (line[-1], sig, line[-1] - sig)


def atr(bars: Sequence[Bar], n: int = 14) -> float | None:
    """Wilder's average true range."""
    if len(bars) < n + 1:
        return None
    tr = [max(b.h - b.l, abs(b.h - a.c), abs(b.l - a.c)) for a, b in zip(bars, bars[1:], strict=False)]
    a = sum(tr[:n]) / n
    for x in tr[n:]:
        a = (a * (n - 1) + x) / n
    return a
