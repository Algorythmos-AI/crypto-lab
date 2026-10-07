"""Kraken public market data over REST. No key, no SDK, no CLI.

    OHLC        closed bars only: Kraken's last row is the bar still forming, and `last` names the newest closed one
    Ticker      best bid and ask
    AssetPairs  lot and price precision, minimum order size and cost
    Time        the venue's clock, for a skew check

Every failure (HTTP status, an `error` list, a malformed body, a timeout) is a DataError, so a cycle can treat
"no data" as one case and never act on a guess.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests

BASE = "https://api.kraken.com/0/public"
TIMEOUT_S = 10.0
MIN_INTERVAL_S = 1.1            # the public endpoints allow about one call a second per address


class DataError(Exception):
    pass


@dataclass(frozen=True)
class Bar:
    t: int                      # bar open, epoch seconds UTC
    o: float
    h: float
    l: float                    # noqa: E741 — the market-data name
    c: float
    vwap: float                 # 0.0 when nothing traded
    v: float
    n: int                      # trades in the bar; 0 = the bar is a carried-forward price, not a market

    @property
    def traded(self) -> bool:
        return self.n > 0 and self.v > 0


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    fetched: float              # time.time() when it was read: the ticker carries no timestamp of its own

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid > 0 else float("inf")


@dataclass(frozen=True)
class PairInfo:
    lot_decimals: int
    tick: Decimal
    order_min: Decimal          # base units
    cost_min: Decimal           # quote currency


Get = Callable[[str, dict[str, Any]], Any]


def _http_get(url: str, params: dict[str, Any]) -> Any:
    r = requests.get(url, params=params, timeout=TIMEOUT_S)
    if r.status_code != 200:
        raise DataError(f"HTTP {r.status_code}")
    return r.json()


class KrakenPublic:
    def __init__(self, get: Get | None = None, min_interval_s: float = MIN_INTERVAL_S,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._get, self._gap, self._sleep, self._last = get or _http_get, min_interval_s, sleep, 0.0
        self.calls = 0

    def _call(self, name: str, **params: Any) -> dict[str, Any]:
        wait = self._last + self._gap - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        try:
            body = self._get(f"{BASE}/{name}", params)
        except DataError:
            raise
        except Exception as e:  # noqa: BLE001 — timeouts, DNS, TLS, bad JSON: all "no data"
            raise DataError(f"{name}: {e.__class__.__name__}") from e
        if not isinstance(body, dict) or body.get("error") or not isinstance(body.get("result"), dict):
            err = body.get("error") if isinstance(body, dict) else None
            raise DataError(f"{name}: {err or 'malformed response'}")
        result: dict[str, Any] = body["result"]
        return result

    def time(self) -> int:
        try:
            return int(self._call("Time")["unixtime"])
        except (KeyError, TypeError, ValueError) as e:
            raise DataError("Time: malformed response") from e

    def ohlc(self, pair: str, interval_min: int, since: int | None = None) -> list[Bar]:
        """Closed bars, oldest first. `since` is exclusive, as Kraken defines it."""
        res = self._call("OHLC", pair=pair, interval=interval_min, **({"since": since} if since is not None else {}))
        try:
            last = int(res["last"])
            rows = next(v for k, v in res.items() if k != "last")
            bars = [Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]),
                        int(r[7])) for r in rows if int(r[0]) <= last]
        except (KeyError, StopIteration, TypeError, ValueError, IndexError) as e:
            raise DataError("OHLC: malformed response") from e
        step = interval_min * 60
        if any(b.t % step for b in bars) or any(b2.t <= b1.t for b1, b2 in zip(bars, bars[1:], strict=False)):
            raise DataError("OHLC: bars are not on the interval grid, or not in order")
        return bars

    def ticker(self, pair: str) -> Quote:
        res = self._call("Ticker", pair=pair)
        try:
            row = next(iter(res.values()))
            q = Quote(float(row["b"][0]), float(row["a"][0]), time.time())
        except (StopIteration, KeyError, TypeError, ValueError, IndexError) as e:
            raise DataError("Ticker: malformed response") from e
        if not 0 < q.bid <= q.ask:
            raise DataError("Ticker: crossed or empty book")
        return q

    def pair_infos(self, pairs: list[str]) -> dict[str, PairInfo]:
        """Every online pair of `pairs` in one call, keyed by the name asked for. Kraken keys its answer by its
        own long name (XXBTZUSD for XBTUSD), so rows are matched on `altname`. A pair that is missing or not
        online is left out: the caller refuses the entry."""
        res = self._call("AssetPairs", pair=",".join(pairs))
        out: dict[str, PairInfo] = {}
        for key, row in res.items():
            try:
                name = str(row.get("altname") or key)
                if row.get("status") == "online" and (name in pairs or key in pairs):
                    out[name if name in pairs else key] = PairInfo(
                        int(row["lot_decimals"]), Decimal(str(row["tick_size"])), Decimal(str(row["ordermin"])),
                        Decimal(str(row["costmin"])))
            except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return out

    def pair_info(self, pair: str) -> PairInfo:
        res = self._call("AssetPairs", pair=pair)
        try:
            row = next(iter(res.values()))
            if row.get("status") != "online":
                raise DataError(f"AssetPairs: {pair} is {row.get('status')}")
            return PairInfo(int(row["lot_decimals"]), Decimal(str(row["tick_size"])), Decimal(str(row["ordermin"])),
                            Decimal(str(row["costmin"])))
        except (StopIteration, KeyError, TypeError, ValueError, ArithmeticError) as e:
            raise DataError("AssetPairs: malformed response") from e


# ---------------------------------------------------------------- history for research (DEC-0015)

HIST_BASE = "https://api.exchange.coinbase.com"      # public candles; Kraken serves only its last 720 bars
HIST_MAX = 300                                       # candles per call
HIST_INTERVAL_S = 0.35


def _utc(t: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class CoinbasePublic:
    """Hourly candles from a second public exchange, for the backtest and for training. Never used to trade:
    the desk's prices are Kraken's. No key. Every failure is a DataError."""

    def __init__(self, get: Get | None = None, min_interval_s: float = HIST_INTERVAL_S,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._get, self._gap, self._sleep, self._last = get or _http_get, min_interval_s, sleep, 0.0
        self.calls = 0

    def candles(self, product: str, step_s: int, start: int, end: int) -> list[Bar]:
        """Candles whose open time is in [start, end), oldest first. At most HIST_MAX of them."""
        wait = self._last + self._gap - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        try:
            body = self._get(f"{HIST_BASE}/products/{product}/candles",
                             {"granularity": step_s, "start": _utc(start), "end": _utc(end - step_s)})
        except DataError:
            raise
        except Exception as e:  # noqa: BLE001 — timeouts, DNS, TLS, bad JSON: all "no data"
            raise DataError(f"candles: {e.__class__.__name__}") from e
        if not isinstance(body, list):
            raise DataError("candles: malformed response")
        out: dict[int, Bar] = {}
        try:
            for r in body:                                   # [time, low, high, open, close, volume]
                t, lo, hi, o, c, v = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
                if start <= t < end and t % step_s == 0 and lo > 0 and hi >= lo:
                    out[t] = Bar(t, o, hi, lo, c, (hi + lo + c) / 3, v, 1 if v > 0 else 0)
        except (TypeError, ValueError, IndexError) as e:
            raise DataError("candles: malformed response") from e
        return [out[t] for t in sorted(out)]

    def history(self, product: str, step_s: int, start: int, end: int) -> list[Bar]:
        """Every candle in [start, end), paged."""
        out: list[Bar] = []
        t = start - start % step_s
        while t < end:
            nxt = min(end, t + HIST_MAX * step_s)
            out += self.candles(product, step_s, t, nxt)
            t = nxt
        return out


def fill_grid(bars: list[Bar], step_s: int) -> list[Bar]:
    """`bars` on a full grid from its first to its last: an exchange omits a candle nobody traded in. The filler is
    a flat bar at the last close with no volume, which `Bar.traded` reports as not a market."""
    out: list[Bar] = []
    for b in bars:
        while out and b.t - out[-1].t > step_s:
            p = out[-1]
            out.append(Bar(p.t + step_s, p.c, p.c, p.c, p.c, 0.0, 0.0, 0))
        if not out or b.t > out[-1].t:
            out.append(b)
    return out


def aggregate(bars: list[Bar], step_s: int, into_s: int) -> list[Bar]:
    """Bars of `into_s` built from contiguous bars of `step_s`, on UTC boundaries. Only complete buckets."""
    need = into_s // step_s
    out: list[Bar] = []
    bucket: list[Bar] = []
    for b in bars:
        if bucket and b.t // into_s != bucket[0].t // into_s:
            bucket = []
        bucket.append(b)
        if len(bucket) == need and bucket[0].t % into_s == 0:
            v = sum(x.v for x in bucket)
            out.append(Bar(bucket[0].t, bucket[0].o, max(x.h for x in bucket), min(x.l for x in bucket), bucket[-1].c,
                           sum(x.vwap * x.v for x in bucket) / v if v > 0 else 0.0, v, sum(x.n for x in bucket)))
            bucket = []
    return out
