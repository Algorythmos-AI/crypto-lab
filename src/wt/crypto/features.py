"""The 14 features recorded with every observation: one definition, used by the recorder and by training."""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from wt.crypto import indicators
from wt.crypto.data import Bar

COLUMNS = ("rsi", "vwap_distance_pct", "ema8_distance_pct", "macd_histogram", "macd_line", "macd_signal",
           "volume_ratio", "candle_body_pct", "high_low_range_pct", "momentum_5", "momentum_20", "atr_pct",
           "hour_utc", "day_of_week")


def _pct(a: float, b: float | None) -> float | None:
    return None if not b else round((a - b) / b * 100, 6)


def extract(bars: Sequence[Bar]) -> dict[str, float | None]:
    """Features of the last bar. A feature its inputs cannot support is None, never a filled-in number."""
    last, closes = bars[-1], [b.c for b in bars]
    m = indicators.macd(closes)
    prior = [b.v for b in bars[-21:-1]]
    mean_v = sum(prior) / len(prior) if prior else 0.0
    a = indicators.atr(bars)
    when = dt.datetime.fromtimestamp(last.t, dt.UTC)
    r = indicators.rsi(closes)
    out: dict[str, float | None] = {
        "rsi": None if r is None else round(r, 4),
        "vwap_distance_pct": _pct(last.c, indicators.session_vwap(bars)),
        "ema8_distance_pct": _pct(last.c, indicators.ema(closes, 8)),
        "macd_histogram": None if m is None else round(m[2], 6),
        "macd_line": None if m is None else round(m[0], 6),
        "macd_signal": None if m is None else round(m[1], 6),
        "volume_ratio": round(last.v / mean_v, 4) if mean_v > 0 else None,
        "candle_body_pct": _pct(last.c, last.o),
        "high_low_range_pct": round((last.h - last.l) / last.l * 100, 6) if last.l > 0 else None,
        "momentum_5": _pct(last.c, closes[-6]) if len(closes) > 5 else None,
        "momentum_20": _pct(last.c, closes[-21]) if len(closes) > 20 else None,
        "atr_pct": None if a is None or last.c <= 0 else round(a / last.c * 100, 6),
        "hour_utc": float(when.hour),
        "day_of_week": float(when.weekday()),
    }
    assert tuple(out) == COLUMNS
    return out
