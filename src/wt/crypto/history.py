"""Hourly history for research (DEC-0015, DEC-0016): candles from the second public exchange, cached on disk.

The cache is `data/crypto/history/<PRODUCT>-3600.jsonl` (not in git): one candle a line. It is read whole and
extended at either end when a longer span is asked for. Never used to trade.
"""
from __future__ import annotations

import json

from wt.core.config import DATA_DIR
from wt.crypto.data import Bar, CoinbasePublic

HOUR, DAY = 3600, 86_400
CACHE = DATA_DIR / "crypto" / "history"
WARMUP_D = 75                   # the rules need 60 closed daily bars before the first signal


def product(pair: str) -> str:
    return pair.replace("/", "-")


def load_hourly(pair: str, start: int, end: int, client: CoinbasePublic | None) -> list[Bar]:
    """Hourly candles in [start, end) from the cache, fetching what it lacks at either end. With no client the
    cache is all there is."""
    path = CACHE / f"{product(pair)}-3600.jsonl"
    have: dict[int, Bar] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            have[int(r[0])] = Bar(int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], int(r[7]))
    lo, hi = (min(have), max(have) + HOUR) if have else (end, end)
    fresh: list[Bar] = []
    if client is not None:
        if start < lo:
            fresh += client.history(product(pair), HOUR, start, min(lo, end))
        if hi < end:
            fresh += client.history(product(pair), HOUR, max(hi, start), end)
    if fresh:
        for b in fresh:
            have[b.t] = b
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps([b.t, b.o, b.h, b.l, b.c, b.vwap, b.v, b.n]) + "\n"
                                for b in (have[t] for t in sorted(have))))
    return [have[t] for t in sorted(have) if start <= t < end]
