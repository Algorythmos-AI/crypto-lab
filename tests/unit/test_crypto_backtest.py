"""Gate C1 (DEC-0015): the backtest drives the desk's own code, so the same bars give the same trades."""
from __future__ import annotations

import dataclasses
import json
from decimal import Decimal

import numpy as np
import pytest

from wt.crypto import backtest, risk
from wt.crypto.data import Bar, CoinbasePublic, DataError, aggregate, fill_grid

from test_crypto_sleeves import B0, CFG, DAY, H4, INFO, Venue, crypto, row  # noqa: F401

HOUR = 3600
DESK_LIMITS = risk.load_desk_limits()                    # the real ones: the `crypto` fixture switches them off


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


def test_the_backtest_and_the_live_cycle_make_the_same_trades_from_the_same_bars(crypto, monkeypatch):  # noqa: F811
    d, _, _ = crypto
    monkeypatch.setattr(risk, "load_desk_limits", lambda key="CD": DESK_LIMITS)
    after = [(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 112.0, 105.0, 111.0, 3.0), (111.0, 111.5, 110.0, 110.5, 1.0)]
    history = {"BTC/USD": breakout_history(after)}
    end = B0 + H4 * (len(after) + 1) + 1                                 # `end` is exclusive: include the last close
    infos = {"XBTUSD": INFO["XBTUSD"]}
    cfg = {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": {"BTC/USD": "XBTUSD"}}}}
    # The desk runs under its desk-wide limits (DEC-0019), so the backtest is given the same ones. TREND and BREAK
    # both fire on the breakout bar; TREND is first and takes the coin.
    tested = backtest.run(cfg, history, infos, B0, end, desk_lim=DESK_LIMITS)
    kinds = [r["kind"] for r in tested if r.get("sleeve") == "trend" and r["kind"] in ("entry", "exit")]
    assert kinds[:1] == ["entry"]
    assert [r["why"] for r in tested if r.get("sleeve") == "break" and r["kind"] == "refused"][0] == ["desk_coin"]

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
    assert _economics(live, "trend") == _economics(tested, "trend") and len(_economics(live, "trend")) >= 1
    assert _economics(live, "break") == _economics(tested, "break") == []
    refusals = lambda rows: [(r["sleeve"], r["pair"], r["bar"], r["why"]) for r in rows if r["kind"] == "refused"]  # noqa: E731
    assert refusals(live) == refusals(tested)


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


# ---------------------------------------------------------------- following a signal to its outcome (DEC-0016, 2)

def _one_pair_cfg() -> dict:
    return {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": {"BTC/USD": "XBTUSD"}}}}


@pytest.mark.parametrize("after,reason", [
    ([(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 112.0, 105.0, 111.0, 3.0)], "target"),
    ([(104.0, 104.5, 96.0, 97.0, 2.0)], "stop"),
])
def test_a_signals_outcome_is_what_the_book_booked_for_the_trade_it_took(after, reason):
    from wt.crypto import rules, signals
    cfg = _one_pair_cfg()
    history = {"BTC/USD": breakout_history(after)}
    rows = backtest.run(cfg, history, {"XBTUSD": INFO["XBTUSD"]}, B0, B0 + H4 * (len(after) + 1) + 1)
    sig = next(r for r in rows if r["kind"] == "signal" and r["sleeve"] == "break")
    booked = next(r for r in rows if r["kind"] == "exit" and r["sleeve"] == "break")
    assert sig["taken"] is True and booked["reason"] == reason
    m = backtest.Market.of(history["BTC/USD"], H4, DAY)
    c = rules.Common.of(cfg["sleeves"]["common"])
    got = signals.outcome("break", sig["price"], sig["stop"], sig["target"], sig["atr"], sig["bar"], m.h4, m.hourly, c,
                          cfg["sleeves"]["break"], cfg["costs"], 3600)
    assert got["reason"] == reason and got["exit_price"] == pytest.approx(float(booked["exit_price"]), rel=1e-6)
    assert got["r"] == pytest.approx(booked["r"], abs=0.01)                       # the book rounds to ticks and cents


def test_a_refused_signal_gets_the_same_outcome_and_an_unfinished_one_gets_none():
    from wt.crypto import rules, signals
    cfg = _one_pair_cfg()
    after = [(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 112.0, 105.0, 111.0, 3.0)]
    history = {"BTC/USD": breakout_history(after)}
    m = backtest.Market.of(history["BTC/USD"], H4, DAY)
    c = rules.Common.of(cfg["sleeves"]["common"])
    price, atr = 104.052, 1.2357
    stop, target, _ = rules.levels(price, atr, c, cfg["sleeves"]["break"])
    full = signals.outcome("break", price, stop, target, atr, B0, m.h4, m.hourly, c, cfg["sleeves"]["break"], cfg["costs"], 3600)
    assert full["reason"] == "target" and full["r"] > 1 and full["held_bars"] >= 1
    early = [b for b in m.h4 if b.t <= B0 + H4]
    fine = [b for b in m.hourly if b.t < B0 + 2 * H4]
    assert signals.outcome("break", price, stop, target, atr, B0, early, fine, c, cfg["sleeves"]["break"], cfg["costs"], 3600) is None
    assert signals.outcome("break", price, price, target, atr, B0, m.h4, m.hourly, c, cfg["sleeves"]["break"], cfg["costs"], 3600) is None


def test_the_backtest_records_every_signal_so_history_can_be_labelled():
    cfg = _one_pair_cfg()
    after = [(104.0, 106.0, 103.5, 105.5, 2.0)] * 6
    rows = backtest.run(cfg, {"BTC/USD": breakout_history(after)}, {"XBTUSD": INFO["XBTUSD"]}, B0, B0 + 7 * H4 + 1)
    sigs = [r for r in rows if r["kind"] == "signal"]
    decided = [r for r in rows if r["kind"] in ("entry", "refused")]
    assert len(sigs) == len(decided) > 0 and {r["sid"] for r in sigs} == {f"{r['sleeve']}|{r['pair']}|{r['bar']}" for r in decided}
    assert all(r["inputs"]["btc_above_sma50"] in (0.0, 1.0) for r in sigs)       # the market inputs are there in history too


def test_a_signal_has_the_same_inputs_in_the_training_set_as_on_the_desk():
    """DEC-0018: a model is trained on `wt.ml.dataset.build` and scores what the desk records. From the same bars
    the two must give the same signals with the same inputs, or the model is scoring something it never saw."""
    from wt.crypto import signals
    from wt.ml import dataset
    after = [(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 112.0, 105.0, 111.0, 3.0), (111.0, 111.5, 110.0, 110.5, 1.0),
             (110.5, 118.0, 110.0, 117.0, 6.0), (117.0, 117.5, 108.0, 109.0, 2.0), (109.0, 110.0, 108.5, 109.5, 1.0)]
    pairs = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD"}
    cfg = {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": pairs}}}
    bars = breakout_history(after)
    history = {"BTC/USD": bars, "ETH/USD": [Bar(b.t, b.o / 10, b.h / 10, b.l / 10, b.c / 10, b.vwap / 10, b.v, b.n) for b in bars]}
    end = B0 + H4 * (len(after) + 1) + 1
    # A tick far below the price, as on the venue: the fixtures' 0.1 tick on a price of 100 would move a bought
    # signal's stop by a visible share of its distance, which no traded pair's real tick does.
    fine = {k: dataclasses.replace(INFO[k], tick=Decimal("0.000001")) for k in pairs.values()}
    desk = {r["sid"]: r for r in backtest.run(cfg, history, fine, B0, end) if r["kind"] == "signal"}
    assert len(desk) >= 4 and {r["inputs"]["breadth"] for r in desk.values()} == {2.0}
    # History is followed past the end of the desk's run so that every one of its signals has an outcome.
    flat = [(109.5, 109.6, 80.0, 81.0, 1.0)] + [(81.0, 81.5, 80.5, 81.0, 1.0)] * 200
    longer = breakout_history([*after, *flat])
    shift = bars[0].t - longer[0].t
    longer = [Bar(b.t + shift, b.o, b.h, b.l, b.c, b.vwap, b.v, b.n) for b in longer]
    assert [(b.t, b.c) for b in longer[:len(bars)]] == [(b.t, b.c) for b in bars]
    trained = {e.sid: e for e in dataset.build(
        cfg, {"BTC/USD": longer, "ETH/USD": [Bar(b.t, b.o / 10, b.h / 10, b.l / 10, b.c / 10, b.vwap / 10, b.v, b.n)
                                             for b in longer]}, B0, end)}
    assert set(desk) <= set(trained), sorted(set(desk) - set(trained))
    assert {s for s, e in trained.items() if e.t < end} == set(desk)
    unknown = {"spread_pct", "held_elsewhere"}                           # candle history cannot supply these
    for sid, row_ in desk.items():
        for name in signals.INPUTS:
            if name in unknown:
                continue
            a, b = row_["inputs"][name], trained[sid].inputs[name]
            assert (a is None and b is None) or a == pytest.approx(b, rel=1e-4, abs=1e-4), (sid, name, a, b)


def test_without_desk_limits_each_book_stands_alone_as_in_exp_0016():
    cfg = _one_pair_cfg()
    after = [(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 112.0, 105.0, 111.0, 3.0), (111.0, 111.5, 110.0, 110.5, 1.0)]
    rows = backtest.run(cfg, {"BTC/USD": breakout_history(after)}, {"XBTUSD": INFO["XBTUSD"]}, B0, B0 + 4 * H4 + 1)
    entered = {r["sleeve"] for r in rows if r["kind"] == "entry"}
    assert {"trend", "break"} <= entered                                 # one coin, bought twice on one bar


def test_under_desk_limits_no_coin_is_held_twice_and_a_refused_signal_is_still_followed():
    from wt.crypto import signals
    pairs = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}
    cfg = {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": pairs}}}
    after = [(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 112.0, 105.0, 111.0, 3.0), (111.0, 111.5, 110.0, 110.5, 1.0),
             (110.5, 118.0, 110.0, 117.0, 6.0), (117.0, 117.5, 108.0, 109.0, 2.0), (109.0, 110.0, 108.5, 109.5, 1.0)]
    bars = breakout_history(after)
    history = {p: [Bar(b.t, b.o / k, b.h / k, b.l / k, b.c / k, b.vwap / k, b.v, b.n) for b in bars]
               for p, k in zip(pairs, (1, 10, 4), strict=True)}
    infos = {k: dataclasses.replace(INFO.get(k, INFO["XBTUSD"]), tick=Decimal("0.000001")) for k in pairs.values()}
    end = B0 + H4 * (len(after) + 1) + 1
    lim = DESK_LIMITS
    assert lim is not None and lim.one_position_per_coin and lim.max_open_risk_pct == Decimal("3.0")
    alone = backtest.run(cfg, history, infos, B0, end)
    held = backtest.run(cfg, history, infos, B0, end, desk_lim=lim)
    # Replay the journal: who holds what after every row.
    open_: dict[str, set[str]] = {}
    for r in held:
        if r["kind"] == "entry":
            assert not any(r["pair"] in v for n, v in open_.items() if n != r["sleeve"]), r
            open_.setdefault(r["sleeve"], set()).add(r["pair"])
        elif r["kind"] == "exit":
            open_[r["sleeve"]].discard(r["pair"])
    why = [c for r in held if r["kind"] == "refused" for c in r["why"]]
    assert "desk_coin" in why                                            # (the risk rule has its own test below)
    assert len([r for r in held if r["kind"] == "entry"]) < len([r for r in alone if r["kind"] == "entry"])
    # The limits change who may enter, never how big: an entry made under them is sized as the sleeve sizes it.
    size = {(r["sleeve"], r["pair"], r["bar"]): (r["qty"], r["price"], r["stop"]) for r in alone if r["kind"] == "entry"}
    first = next(r for r in held if r["kind"] == "entry")
    assert size[(first["sleeve"], first["pair"], first["bar"])] == (first["qty"], first["price"], first["stop"])
    # A signal the desk refused is recorded like any other, with its inputs, so it can be followed to an outcome.
    sig = next(r for r in held if r["kind"] == "signal" and not r["taken"] and "desk_coin" in r["why"])
    assert sig["inputs"]["held_elsewhere"] == 1.0 and sig["stop"] < sig["price"]
    assert signals.INPUTS == tuple(sig["inputs"])


def test_open_risk_counts_every_book_and_a_stop_above_its_entry_risks_nothing():
    from wt.crypto.book import Book, Position
    def pos(pair, qty, entry, stop):                                     # noqa: E306
        p = Position.__new__(Position)
        p.pair, p.qty, p.entry_price, p.stop = pair, Decimal(qty), Decimal(entry), Decimal(stop)
        return p
    a, b = Book(Decimal(10000), Decimal(10000)), Book(Decimal(10000), Decimal(10000))
    a.positions["BTC/USD"] = pos("BTC/USD", "1", "100", "90")           # risks 10
    b.positions["ETH/USD"] = pos("ETH/USD", "2", "50", "60")            # stop trailed above the entry: risks nothing
    books = {"trend": a, "break": b}
    assert risk.open_risk(books) == Decimal(10)
    lim = risk.DeskLimits(True, Decimal("3.0"))
    eq = Decimal(20000)                                                  # 3% is 600
    assert risk.desk_blockers("SOL/USD", "dip", Decimal(590), books, eq, lim) == []
    assert risk.desk_blockers("SOL/USD", "dip", Decimal(591), books, eq, lim) == ["desk_risk"]
    assert risk.desk_blockers("BTC/USD", "break", Decimal(1), books, eq, lim) == ["desk_coin"]
    assert risk.desk_blockers("BTC/USD", "trend", Decimal(1), books, eq, lim) == []      # its own book's rule, not this one
    assert risk.desk_blockers("BTC/USD", "break", Decimal(10**6), books, eq, None) == []
    assert risk.desk_blockers("BTC/USD", "break", Decimal(1), books, eq, risk.DeskLimits(False, Decimal("3.0"))) == []
