"""The market monitor (wt.crypto.monitor): descriptive readings from stored bars and from the books."""
from __future__ import annotations

import math
from decimal import Decimal

import pytest

from wt.crypto import indicators, monitor, signals
from wt.crypto.book import Book
from wt.crypto.data import Bar, PairInfo

STEP = 14_400
INFO = PairInfo(8, Decimal("0.001"), Decimal("0.0001"), Decimal("0.5"))


def bars(closes: list[float], t0: int = 0, step: int = STEP, v: float = 100.0) -> list[Bar]:
    return [Bar(t0 + i * step, c, c * 1.01, c * 0.99, c, c, v, 10) for i, c in enumerate(closes)]


def walk(n: int, start: float = 100.0, by: float = 1.0) -> list[float]:
    return [start + i * by for i in range(n)]


def test_a_coin_is_read_with_the_desks_own_definitions():
    b = bars(walk(80))
    now = b[-1].t + STEP + 60
    row = monitor.pair_row("BTC/USD", b, 240, now)
    closes = [x.c for x in b]
    assert row["close"] == 179.0 and row["bar"] == b[-1].t + STEP and row["bars"] == 80 and row["stale"] is False
    assert row["ret_30"] == signals.ret_30(b)
    assert row["rsi"] == round(indicators.rsi(closes, 14), 2)
    assert row["atr_pct"] == round(indicators.atr(b, 14) / 179.0 * 100, 4)
    assert row["above_ema20"] is True and row["above_ema50"] is True
    assert row["ret_1"] == round((179 / 178 - 1) * 100, 4) and row["ret_day"] == round((179 / 173 - 1) * 100, 4)
    assert row["volume_ratio"] == 1.0
    # the close against the highest HIGH of the 20 bars before it (each high is 1% over its close)
    assert row["to_high_20_pct"] == round((179 / (178 * 1.01) - 1) * 100, 4)


def test_too_few_bars_give_none_not_a_number():
    row = monitor.pair_row("ETH/USD", bars(walk(10)), 240, 10 * STEP)
    assert row["ret_30"] is None and row["to_high_20_pct"] is None and row["above_ema50"] is None
    assert row["rsi"] is None and row["atr_pct"] is None and row["volume_ratio"] is None
    assert monitor.pair_row("ETH/USD", [], 240, 0) is None


def test_only_the_bars_after_the_last_hole_are_used():
    old, new = bars(walk(60)), bars(walk(40, 500.0), t0=70 * STEP)          # ten bars are missing in between
    row = monitor.pair_row("SOL/USD", old + new, 240, new[-1].t + STEP)
    assert row["bars"] == 40 and row["above_ema50"] is None and row["ret_30"] == signals.ret_30(new)


def test_an_old_newest_bar_is_marked_stale():
    b = bars(walk(40))
    assert monitor.pair_row("BTC/USD", b, 240, b[-1].t + STEP + 2 * STEP + 1)["stale"] is True


def test_coins_are_placed_by_their_30_bar_return_and_one_without_it_comes_last():
    rows = [{"pair": "A", "ret_30": 1.0}, {"pair": "B", "ret_30": None}, {"pair": "C", "ret_30": 5.0},
            {"pair": "D", "ret_30": 1.0}]
    got = monitor.ranked(rows)
    assert [(r["pair"], r["rank"]) for r in got] == [("C", 1), ("A", 2), ("D", 3), ("B", None)]


def test_correlation_of_a_coin_with_its_copy_its_mirror_and_a_flat_one():
    up = [100.0]
    for i in range(60):
        up.append(up[-1] * (1.02 if i % 3 else 0.97))
    mirror = [100.0]
    for a, b in zip(up, up[1:], strict=False):
        mirror.append(mirror[-1] * (a / b))
    got = monitor.correlation({"A": bars(up), "B": bars(up), "C": bars(mirror), "F": bars([5.0] * 61)}, 240)
    by = {r["pair"]: dict(zip(got["pairs"], r["with"], strict=True)) for r in got["rows"]}
    assert by["A"]["A"] == 1.0 and by["A"]["B"] == 1.0 and by["A"]["C"] == -1.0
    assert by["A"]["F"] is None and by["F"]["A"] is None          # a coin that did not move is correlated with nothing
    assert by["C"]["A"] == by["A"]["C"]
    assert got["high"] == 1.0 and got["low"] == -1.0 and got["mean"] == pytest.approx(-1 / 3, abs=1e-3)


def test_correlation_needs_enough_shared_bars_and_two_coins():
    assert monitor.correlation({"A": bars(walk(50))}, 240) is None
    short = monitor.correlation({"A": bars(walk(20)), "B": bars(walk(20, 50.0, 2.0))}, 240)
    assert short["rows"][0]["with"] == [1.0, None] and short["mean"] is None
    # bars that do not line up in time share nothing
    apart = monitor.correlation({"A": bars(walk(50)), "B": bars(walk(50), t0=1_000 * STEP)}, 240)
    assert apart["rows"][0]["with"][1] is None


def test_correlation_is_measured_on_the_newest_bars_only():
    a = [100.0 * (1.01 if i % 2 else 0.99) ** 1 * (1 + 0.001 * i) for i in range(300)]
    got = monitor.correlation({"A": bars(a), "B": bars(a)}, 240, bars=40)
    assert got["bars"] == 40 and got["rows"][0]["with"] == [1.0, 1.0]


def daily(closes: list[float]) -> list[Bar]:
    return bars(closes, step=86_400)


def test_the_regime_code_follows_bitcoin_and_breadth():
    up, down = daily(walk(60, 100.0, 1.0)), daily(walk(60, 200.0, -1.0))
    now = up[-1].t + 86_400
    high = [{"above_ema50": True, "ret_30": 2.0, "stale": False}] * 3
    low = [{"above_ema50": False, "ret_30": -2.0, "stale": False}] * 3
    assert monitor.regime(up, high, now)["code"] == "up"
    assert monitor.regime(down, low, now)["code"] == "down"
    assert monitor.regime(up, low, now)["code"] == "mixed" and monitor.regime(down, high, now)["code"] == "mixed"
    got = monitor.regime(up, high + low[:1], now)
    assert got["pairs"] == 4 and got["above_ema50"] == 3 and got["breadth"] == 0.75 and got["rising_share"] == 0.75
    assert got["btc_close"] == 159.0 and got["btc_vs_sma50_pct"] > 0 and got["btc_ret_30d"] == round((159 / 129 - 1) * 100, 4)


def test_the_regime_says_nothing_when_a_reading_is_missing():
    assert monitor.regime(daily(walk(20)), [{"above_ema50": True, "ret_30": 1.0}], 0)["code"] is None
    got = monitor.regime([], [], 0)
    assert got["code"] is None and got["btc_close"] is None and got["vol_30d_pct"] is None and got["breadth"] is None
    # a stale coin is left out of breadth
    stale = monitor.regime(daily(walk(60)), [{"above_ema50": True, "ret_30": 1.0, "stale": True}], 0)
    assert stale["pairs"] == 0 and stale["breadth"] is None and stale["code"] is None


def test_volatility_is_the_yearly_figure_of_thirty_daily_moves():
    closes = [100.0]
    for i in range(59):
        closes.append(closes[-1] * (1.02 if i % 2 else 0.98))
    moves = [math.log(b / a) for a, b in zip(closes[-31:], closes[-30:], strict=False)]
    mean = sum(moves) / 30
    sd = math.sqrt(sum((m - mean) ** 2 for m in moves) / 29)
    assert monitor.regime(daily(closes), [], 0)["vol_30d_pct"] == round(sd * math.sqrt(365) * 100, 2)


def book_with(*held: tuple[str, str, str, str]) -> Book:
    book = Book(Decimal(10_000), Decimal(10_000))
    for pair, qty, price, stop in held:
        book.buy_qty(pair, Decimal(qty), Decimal(price), INFO, 0.40, 10, 0, Decimal(stop), Decimal("Infinity"), "trend",
                     Decimal("1"))
    return book


def test_exposure_adds_up_by_coin_and_by_book():
    books = {"trend": book_with(("BTC/USD", "0.01", "100000", "96000"), ("ETH/USD", "0.5", "4000", "3800")),
             "break": book_with(("BTC/USD", "0.02", "100000", "95000")), "dip": book_with()}
    marks = {"BTC/USD": 110_000.0}                                  # ETH has no mark: valued at its entry
    got = monitor.exposure(books, marks)
    coins = {c["pair"]: c for c in got["coins"]}
    assert [c["pair"] for c in got["coins"]] == ["BTC/USD", "ETH/USD"]            # largest first
    assert coins["BTC/USD"]["books"] == ["break", "trend"] and coins["BTC/USD"]["notional"] == pytest.approx(3_300.0)
    assert coins["BTC/USD"]["risk"] == pytest.approx(0.01 * 14_000 + 0.02 * 15_000)
    assert coins["ETH/USD"]["notional"] == pytest.approx(2_000.0) and coins["ETH/USD"]["risk"] == pytest.approx(100.0)
    assert got["gross"] == pytest.approx(5_300.0) and got["gross"] == pytest.approx(sum(b["notional"] for b in got["books"]))
    assert got["risk"] == pytest.approx(sum(b["risk"] for b in got["books"]))
    assert sum(c["share_pct"] for c in got["coins"]) == pytest.approx(100.0)
    assert got["largest_share_pct"] == pytest.approx(3_300 / 5_300 * 100)
    assert got["equity"] == pytest.approx(sum(b["equity"] for b in got["books"]))
    assert {b["name"]: b["positions"] for b in got["books"]} == {"trend": 2, "break": 1, "dip": 0}
    # the gain on Bitcoin, less the entry fees
    assert coins["BTC/USD"]["unrealised"] == pytest.approx(0.03 * 10_000 - 0.03 * 100_000 * 0.004)


def test_a_stop_above_the_price_is_no_negative_risk_and_empty_books_are_all_none():
    got = monitor.exposure({"trend": book_with(("BTC/USD", "0.01", "100000", "99000"))}, {"BTC/USD": 98_000.0})
    assert got["coins"][0]["risk"] == 0.0
    empty = monitor.exposure({"trend": book_with()}, {})
    assert empty["coins"] == [] and empty["gross"] == 0 and empty["gross_pct"] == 0 and empty["largest_share_pct"] is None


def test_r_bands_hold_every_trade_once():
    rs = [-2.5, -2.0, -1.0, -0.99, -0.01, 0.0, 0.49, 0.5, 2.99, 3.0, 4.0, 9.0]
    bands = monitor.r_bands(rs)
    assert sum(b["n"] for b in bands) == len(rs) and len(bands) == len(monitor.R_EDGES) + 1
    by = {(b["lo"], b["hi"]): b["n"] for b in bands}
    assert by[None, -2.0] == 1 and by[-2.0, -1.5] == 1 and by[-1.0, -0.5] == 2 and by[-0.5, 0.0] == 1
    assert by[0.0, 0.5] == 2 and by[0.5, 1.0] == 1 and by[2.0, 3.0] == 1 and by[3.0, 4.0] == 1 and by[4.0, None] == 2
    assert monitor.r_bands([]) == []


def test_wins_against_losses_in_r():
    got = monitor.r_summary([2.0, 1.0, -1.0, -0.5, 0.0])
    assert got["mean_win_r"] == 1.5 and got["mean_loss_r"] == -0.5 and got["payoff"] == 3.0
    assert got["best_r"] == 2.0 and got["worst_r"] == -1.0 and got["median_r"] == 0.0
    none = monitor.r_summary([])
    assert none == {"mean_win_r": None, "mean_loss_r": None, "payoff": None, "best_r": None, "worst_r": None,
                    "median_r": None, "r_bands": []}
    assert monitor.r_summary([1.0])["payoff"] is None and monitor.r_summary([0.0])["payoff"] is None


def test_the_view_reads_every_pairs_stored_bars(tmp_path):
    import json

    from wt.crypto import sleeves
    pairs = {"BTC/USD": "XBTUSD", "ETH/USD": "ETHUSD", "SOL/USD": "SOLUSD"}

    def store(venue: str, tf: int, rows: list[Bar]) -> None:
        (tmp_path / f"{venue}-{tf}m.jsonl").write_text(
            "".join(json.dumps({"t": b.t, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "vwap": b.vwap, "v": b.v, "n": b.n}) + "\n"
                    for b in rows))
    assert monitor.view(tmp_path, pairs, 240, 1440, 0, sleeves._read_bars) is None
    store("XBTUSD", 240, bars(walk(80)))
    store("ETHUSD", 240, bars(walk(80, 50.0, 2.0)))
    store("XBTUSD", 1440, daily(walk(60)))
    got = monitor.view(tmp_path, pairs, 240, 1440, 80 * STEP, sleeves._read_bars)
    assert [r["pair"] for r in got["pairs"]] == ["ETH/USD", "BTC/USD"]          # SOL has no bars: left out
    assert got["bar"] == 80 * STEP and got["tf_min"] == 240
    assert got["correlation"]["pairs"] == ["BTC/USD", "ETH/USD"]
    assert got["regime"]["btc_close"] == 159.0 and got["regime"]["pairs"] == 2
