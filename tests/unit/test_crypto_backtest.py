"""Gate C1 (DEC-0015): the backtest drives the desk's own code, so the same bars give the same trades."""
from __future__ import annotations

import json
from decimal import Decimal

import numpy as np
import pytest

from wt.crypto import backtest, risk
from wt.crypto.data import Bar, CoinbasePublic, DataError, aggregate, fill_grid

from test_crypto_sleeves import B0, CFG, DAY, H4, INFO, Venue, crypto, row  # noqa: F401

HOUR = 3600


def hourly_series(end: int, closes_4h: list[tuple[float, float, float, float, float]]) -> list[Bar]:
    """Hourly bars whose 4-hour aggregate is the given (open, high, low, close, volume) list, ending at `end`."""
    out: list[Bar] = []
    t = end - H4 * (len(closes_4h) - 1)
    for o, h, l, c, v in closes_4h:                                      # noqa: E741
        path = [(o, max(o, (o + c) / 2)), ((o + c) / 2, h), (h, l), (l, c)]
        for k, (a, b) in enumerate(path):
            hi, lo = max(a, b, h if k == 1 else 0), min(a, b, l if k == 2 else 10**12)
            out.append(Bar(t + k * HOUR, a, hi, lo, b, (hi + lo + b) / 3, v / 4, 3))
        t += H4
    return out


def breakout_history(after: list[tuple[float, float, float, float, float]]) -> list[Bar]:
    """Quiet at 100 for 400 bars (the rules need 60 closed daily bars), a breakout bar that closes at 104 on
    volume (it opens at B0), then `after`."""
    quiet = [(100.0, 100.5, 99.5, 100.0, 1.0)] * 400
    return hourly_series(B0 + H4 * len(after), [*quiet, (100.0, 104.2, 99.9, 104.0, 5.0), *after])


def test_candles_are_parsed_paged_and_put_in_order():
    calls = []

    def get(url, params):
        calls.append(params)
        t0 = 1_700_000_000 - 1_700_000_000 % HOUR
        return [[t0 + HOUR, 9.0, 11.0, 10.0, 10.5, 3.0], [t0, 9.5, 10.5, 10.0, 10.0, 0.0], [t0 + 7, 1, 2, 1, 2, 1]]
    c = CoinbasePublic(get=get, min_interval_s=0)
    t0 = 1_700_000_000 - 1_700_000_000 % HOUR
    bars = c.candles("BTC-USD", HOUR, t0, t0 + 2 * HOUR)
    assert [b.t for b in bars] == [t0, t0 + HOUR] and bars[1].c == 10.5 and bars[0].traded is False
    assert calls[0]["granularity"] == HOUR
    c.history("BTC-USD", HOUR, t0, t0 + 700 * HOUR)
    assert len(calls) == 1 + 3                                           # 700 candles in pages of 300
    with pytest.raises(DataError):
        CoinbasePublic(get=lambda u, p: {"message": "nope"}, min_interval_s=0).candles("X", HOUR, 0, HOUR)
    with pytest.raises(DataError):
        CoinbasePublic(get=lambda u, p: (_ for _ in ()).throw(OSError("down")), min_interval_s=0).candles("X", HOUR, 0, HOUR)


def test_a_missing_candle_is_filled_as_untraded_and_only_whole_buckets_aggregate():
    bars = [Bar(t, 10, 11, 9, 10 + i, 10.0, 2.0, 3) for i, t in enumerate(range(0, 8 * HOUR, HOUR))]
    del bars[5]
    grid = fill_grid(bars, HOUR)
    assert len(grid) == 8 and grid[5].traded is False and grid[5].c == grid[4].c
    four = aggregate(grid, HOUR, H4)
    assert [(b.t, b.o, b.c, b.v) for b in four] == [(0, 10, 13, 8.0), (H4, 10, 17, 6.0)]
    assert aggregate(grid[1:], HOUR, H4)[0].t == H4                      # a bucket missing its first hour is dropped
    assert four[0].h == 11 and four[0].l == 9 and four[1].n == 9


def _economics(rows: list[dict], sleeve: str) -> list[tuple]:
    return [(r["kind"], r["pair"], r.get("qty"), r.get("price"), r.get("stop"), r.get("target"), r.get("reason"),
             r.get("exit_price"), r.get("pnl"), r.get("r"))
            for r in rows if r.get("sleeve") == sleeve and r["kind"] in ("entry", "exit")]


def test_the_backtest_and_the_live_cycle_make_the_same_trades_from_the_same_bars(crypto):  # noqa: F811
    d, _, _ = crypto
    after = [(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 112.0, 105.0, 111.0, 3.0), (111.0, 111.5, 110.0, 110.5, 1.0)]
    history = {"BTC/USD": breakout_history(after)}
    end = B0 + H4 * (len(after) + 1) + 1                                 # `end` is exclusive: include the last close
    infos = {"XBTUSD": INFO["XBTUSD"]}
    cfg = {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": {"BTC/USD": "XBTUSD"}}}}
    tested = backtest.run(cfg, history, infos, B0, end)
    kinds = [r["kind"] for r in tested if r.get("sleeve") == "break" and r["kind"] in ("entry", "exit")]
    assert kinds[:2] == ["entry", "exit"]                                # bought the breakout, sold at its target

    m = backtest.Market.of(history["BTC/USD"], H4, DAY)                  # the same bars, served as a venue would
    v = Venue()
    for close in [c for c in m.h4_close if B0 < c <= end]:
        v.now = close + 10
        i = m.h4_close.index(close) + 1
        j = len([c for c in m.d1_close if c <= close])
        as_rows = lambda bars: [row(b.t, b.o, b.h, b.l, b.c, b.v, b.n) for b in bars]      # noqa: E731
        v.ohlc[("XBTUSD", 240)] = (as_rows(m.h4[max(0, i - 130):i]) + [row(close, 1, 1, 1, 1)], close - H4)
        v.ohlc[("XBTUSD", 1440)] = (as_rows(m.d1[max(0, j - 70):j]) + [row(m.d1[j - 1].t + DAY, 1, 1, 1, 1)], m.d1[j - 1].t)
        fine = [b for b in m.hourly if b.t + HOUR <= close]
        v.ohlc[("XBTUSD", 1)] = (as_rows(fine), fine[-1].t)
        v.ohlc[("XBTUSD", 15)] = ([row(close - 900 * (60 - k), 100, 100.1, 99.9, 100) for k in range(60)], close - 900)
        price = m.h4[i - 1].c
        v.quote["XBTUSD"] = (price, price)
        for k in ("ETHUSD", "SOLUSD"):
            v.ohlc[(k, 15)] = v.ohlc[("XBTUSD", 15)]
            v.quote[k] = (100.0, 100.0)
        import wt.crypto.cycle as cycle_mod
        cycle_mod.run(now=v.now, api=v.api(), desk=d, cfg=cfg, limits=risk.load_limits("C"), alerts=crypto[1])
    live = [json.loads(x) for x in d.journal.read_text().splitlines()]
    assert _economics(live, "break") == _economics(tested, "break") and len(_economics(live, "break")) >= 2
    assert _economics(live, "trend") == _economics(tested, "trend")


def test_costs_at_one_and_a_half_times_slippage_cost_more():
    after = [(104.0, 104.5, 96.0, 97.0, 2.0)]                            # straight down through the stop
    history = {"BTC/USD": breakout_history(after)}
    infos, end = {"XBTUSD": INFO["XBTUSD"]}, B0 + 2 * H4 + 1
    cfg = {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": {"BTC/USD": "XBTUSD"}}}}
    base = backtest.trades(backtest.run(cfg, history, infos, B0, end), "break")
    hard = backtest.trades(backtest.run(cfg, history, infos, B0, end, slip_mult=1.5), "break")
    assert len(base) == len(hard) == 1 and base[0]["reason"] == "stop"
    assert Decimal(hard[0]["pnl"]) < Decimal(base[0]["pnl"]) < 0


def test_the_random_control_trades_at_the_rules_rate_with_the_same_exits():
    history = {"BTC/USD": breakout_history([(104.0, 106.0, 103.5, 105.5, 2.0)] * 40)}
    infos, end = {"XBTUSD": INFO["XBTUSD"]}, B0 + 41 * H4 + 1
    cfg = {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": {"BTC/USD": "XBTUSD"}}}}
    rows = backtest.run(cfg, history, infos, B0, end, entry=backtest.random_entry({"break": 1.0}, 3), names=["break"])
    entered = [r for r in rows if r["kind"] == "entry"]
    assert entered and all(r["sleeve"] == "break" and r["target"] is not None for r in entered)
    again = backtest.run(cfg, history, infos, B0, end, entry=backtest.random_entry({"break": 1.0}, 3), names=["break"])
    assert [r.get("pnl") for r in rows] == [r.get("pnl") for r in again]         # seeded: the same run twice
    never = backtest.run(cfg, history, infos, B0, end, entry=backtest.random_entry({"break": 0.0}, 3), names=["break"])
    assert [r for r in never if r["kind"] == "entry"] == []


def _rows(rs: list[float], sleeve: str = "break") -> list[dict]:
    out = [{"kind": "sleeve", "sleeve": sleeve, "equity": "10000", "pairs": {"BTC/USD": {"fire": True}}}]
    for i, r in enumerate(rs):
        out.append({"kind": "exit", "sleeve": sleeve, "pair": "BTC/USD", "r": r, "pnl": str(r * 100), "reason": "stop",
                    "t": f"2026-0{1 + i % 9}-{1 + i % 27:02d}T00:00:00+00:00"})
    return out


def test_the_verdict_follows_the_registered_rules():
    rng = np.random.default_rng(5)
    good = _rows(list(rng.normal(0.6, 1.0, 200)))
    s = backtest.summary(good, "break", 0, 365 * DAY, 7, 10_000)
    assert s["trades"] == 200 and s["trades_per_month"] == pytest.approx(16.67, abs=0.05) and s["ci_low"] > 0
    assert backtest.verdict(s, s, 0.01) == {"passed": True, "failed_on": []}
    assert backtest.verdict(s, s, 0.20)["failed_on"] == ["no_better_than_random_entry"]
    few = backtest.summary(_rows([1.0] * 10), "break", 0, 365 * DAY, 7, 10_000)
    assert "too_few_trades" in backtest.verdict(few, few, 0.01)["failed_on"]
    bad = backtest.summary(_rows(list(rng.normal(-0.1, 1.0, 200))), "break", 0, 365 * DAY, 7, 10_000)
    failed = backtest.verdict(bad, bad, 0.9)["failed_on"]
    assert {"ci_not_above_zero_at_1.5x_slippage", "deflated_sharpe", "no_better_than_random_entry", "profit_factor"} <= set(failed)
    none = backtest.summary([], "break", 0, 365 * DAY, 7, 10_000)
    assert none["trades"] == 0 and backtest.verdict(none, none, None)["passed"] is False
