"""The tournament sleeves (DEC-0014, DEC-0015): three strategies on 4-hour bars, each on its own paper book, run
after the baseline cycle and unable to disturb it. The real cycle on a scripted venue, as in test_crypto.py."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random
import time
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from wt.core import desk as desks
from wt.core import ledger
from wt.crypto import cycle, risk, rules, sleeves, snapshot
from wt.crypto.book import Book, to_lot, to_tick
from wt.crypto.data import Bar, DataError, KrakenPublic, PairInfo
from wt.ops import alerts as alerts_mod
from wt.ops import publish

ROOT = Path(__file__).resolve().parents[2]
CFG = yaml.safe_load((ROOT / "config/crypto.yaml").read_text())
SC = CFG["sleeves"]
C = rules.Common.of(SC["common"])
PAIRS: dict[str, str] = SC["common"]["pairs"]
H4, DAY = 14_400, 86_400
B0 = 1_791_072_000                                       # 2026-10-04 00:00 UTC: a 4-hour and a daily boundary
assert B0 % DAY == 0
NOW = B0 + H4 + 10                                       # ten seconds after the B0 4-hour bar closed
# What Kraken's AssetPairs answered for the eight pairs on 2026-10-06: (its key, lot decimals, tick, minimum).
VENUE_PAIRS = {"XBTUSD": ("XXBTZUSD", 8, "0.1", "0.00005"), "ETHUSD": ("XETHZUSD", 8, "0.01", "0.001"),
               "SOLUSD": ("SOLUSD", 8, "0.01", "0.06"), "XRPUSD": ("XXRPZUSD", 8, "0.00001", "1.65"),
               "ADAUSD": ("ADAUSD", 8, "0.000001", "20"), "XDGUSD": ("XDGUSD", 8, "0.0000001", "50"),
               "LINKUSD": ("LINKUSD", 8, "0.00001", "0.55"), "AVAXUSD": ("AVAXUSD", 8, "0.001", "0.5")}
INFO = {k: PairInfo(v[1], Decimal(v[2]), Decimal(v[3]), Decimal("0.5")) for k, v in VENUE_PAIRS.items()}


def row(t, o, h, l, c, v=1.0, n=3):  # noqa: E741
    return [t, str(o), str(h), str(l), str(c), str(c), str(v), n]


def to_bars(rows: list) -> list[Bar]:
    return [Bar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6]), int(r[7]))
            for r in rows]


def flat(end: int, step: int, n: int, c: float = 100.0) -> list:
    return [row(end - step * (n - 1 - i), c, c + 0.5, c - 0.5, c) for i in range(n)]


def break_rows(end: int = B0) -> list:
    """119 quiet bars at 100, then a bar that closes at 104 on five times the volume: a new 30-bar high."""
    rows = flat(end, H4, 120)
    rows[-1] = row(end, 100.0, 104.2, 99.9, 104.0, v=5.0)
    return rows


def trend_rows(end: int = B0) -> list:
    """A steady climb of 0.3 a bar, then a bar that jumps 1.5: EMA-20 over EMA-50 and a new 20-bar high."""
    closes = [100 + 0.3 * i for i in range(119)]
    rows = [row(end - H4 * (119 - i), c, c + 0.5, c - 0.5, c) for i, c in enumerate(closes)]
    last = closes[-1] + 1.5
    return rows + [row(end, closes[-1], last + 0.2, closes[-1] - 0.1, last)]


def dip_rows(end: int = B0) -> list:
    """A slow climb, five hard down bars (RSI under 35), then the first close back above EMA-8."""
    closes = [100 + 0.1 * i for i in range(114)] + [111.3 - 1.4 * k for k in range(1, 6)] + [109.5]
    return [row(end - H4 * (119 - i), c, c + 0.6, c - 0.6, c) for i, c in enumerate(closes)]


def daily_rows(end: int = B0 - DAY, rising: bool = True) -> list:
    closes = [100 + (0.5 * i if rising else -0.5 * i) for i in range(60)]
    return [row(end - DAY * (59 - i), c, c + 1, c - 1, c) for i, c in enumerate(closes)]


class Venue:
    """Answers the public endpoints for any pair and interval the test filled in. Unfilled data is a KeyError,
    which the client turns into "no data", as a dead endpoint would be."""

    def __init__(self):
        self.ohlc: dict[tuple[str, int], tuple[list, int]] = {}
        self.quote: dict[str, tuple[float, float]] = {}
        self.now = NOW
        self.asked: list[tuple[str, str]] = []

    def get(self, url: str, params: dict):
        name = url.rsplit("/", 1)[1]
        self.asked.append((name, str(params.get("pair", ""))))
        if name == "Time":
            return {"error": [], "result": {"unixtime": int(self.now)}}
        pair = params["pair"]
        if name == "OHLC":
            rows, last = self.ohlc[(pair, int(params["interval"]))]
            since = params.get("since")
            return {"error": [], "result": {pair: [r for r in rows if since is None or r[0] > since], "last": last}}
        if name == "Ticker":
            b, a = self.quote[pair]
            return {"error": [], "result": {pair: {"a": [str(a), "1", "1"], "b": [str(b), "1", "1"]}}}
        if name == "AssetPairs":
            return {"error": [], "result": {VENUE_PAIRS[p][0]: {
                "altname": p, "status": "online", "lot_decimals": VENUE_PAIRS[p][1], "tick_size": VENUE_PAIRS[p][2],
                "ordermin": VENUE_PAIRS[p][3], "costmin": "0.5"} for p in pair.split(",")}}
        raise AssertionError(name)

    def api(self) -> KrakenPublic:
        return KrakenPublic(get=self.get, min_interval_s=0)

    def fill(self, pair: str, rows4h: list, price: float, daily: list | None = None) -> None:
        self.ohlc[(pair, 240)] = (rows4h + [row(rows4h[-1][0] + H4, price, price, price, price)], rows4h[-1][0])
        d = daily_rows() if daily is None else daily
        self.ohlc[(pair, 1440)] = (d + [row(d[-1][0] + DAY, price, price, price, price)], d[-1][0])
        self.ohlc[(pair, 1)] = ([], rows4h[-1][0])
        self.quote[pair] = (price - 0.05, price + 0.05)


def venue(signal: dict[str, list] | None = None) -> Venue:
    """Every pair quiet on every timeframe, except the 4-hour series given in `signal` (by Kraken pair)."""
    v = Venue()
    for k in PAIRS.values():
        rows = (signal or {}).get(k) or flat(B0, H4, 120)
        v.fill(k, rows, float(rows[-1][4]))
        quiet15 = flat(B0 + H4 - 900, 900, 60)
        v.ohlc[(k, 15)] = (quiet15 + [row(B0 + H4, 100, 100, 100, 100)], quiet15[-1][0])
    return v


@pytest.fixture
def crypto(tmp_path, monkeypatch):
    d = dataclasses.replace(desks.DESKS["crypto"], state_dir=tmp_path / "crypto", kill_file=tmp_path / "crypto/KILL",
                            ledgers=(("crypto", tmp_path / "crypto/crypto_journal.jsonl"),),
                            chain_flag=tmp_path / "crypto/chain-broken")
    d.state_dir.mkdir()
    monkeypatch.delenv("WT_ROLE", raising=False)
    box: list[dict] = []
    monkeypatch.setattr(alerts_mod, "_send", lambda msg, topic, server, timeout=5.0: box.append(msg) or True)
    return d, alerts_mod.Alerts(root=tmp_path / "alerts", topic="t"), box


def run(v: Venue, crypto) -> int:
    d, a, _ = crypto
    return cycle.run(now=v.now, api=v.api(), desk=d, cfg=CFG, limits=risk.load_limits("C"), alerts=a)


def journal(d) -> list[dict]:
    return [json.loads(x) for x in d.journal.read_text().splitlines()] if d.journal.exists() else []


def rows_of(d, sleeve: str, kind: str) -> list[dict]:
    return [r for r in journal(d) if r.get("sleeve") == sleeve and r["kind"] == kind]


def book_of(d, sleeve: str) -> Book:
    return Book.load(risk.sleeve_dir(d, sleeve) / "book.json", Decimal(10_000))


# ---------------------------------------------------------------- the rules

@pytest.mark.parametrize("name,rows", [("trend", trend_rows()), ("break", break_rows()), ("dip", dip_rows())])
def test_each_synthetic_series_is_a_signal_for_its_own_rule(name, rows):
    fire, why, atr = rules.entry(name, to_bars(rows), to_bars(daily_rows()), C, SC[name])
    assert (fire, why) == (True, ()) and atr is not None and atr > 0


def test_a_quiet_market_names_the_conditions_that_failed():
    quiet, daily = to_bars(flat(B0, H4, 120)), to_bars(daily_rows(rising=False))
    assert rules.entry("trend", quiet, daily, C, SC["trend"])[1] == ("no_uptrend", "below_ema", "no_new_high")
    assert rules.entry("break", quiet, daily, C, SC["break"])[1] == ("no_new_high", "low_volume")
    assert rules.entry("dip", quiet, daily, C, SC["dip"])[1] == ("below_daily_average", "no_dip", "not_reclaimed")
    assert rules.entry("break", quiet[:10], daily, C, SC["break"])[1][0] == "no_history"
    untraded = [*quiet[:-1], dataclasses.replace(quiet[-1], n=0, v=0.0)]
    assert "bar_untraded" in rules.entry("break", untraded, daily, C, SC["break"])[1]


def test_the_stop_is_two_atr_and_a_stop_that_is_too_close_or_too_far_skips_the_signal():
    stop, target, skip = rules.levels(100.0, 1.5, C, SC["break"])
    assert (stop, target, skip) == (97.0, 106.0, None)
    assert rules.levels(100.0, 1.5, C, SC["trend"])[1] is None                    # the trend rule has no target
    assert rules.levels(100.0, 0.4, C, SC["break"])[2] == "stop_too_tight"       # 0.8% away
    assert rules.levels(100.0, 7.0, C, SC["break"])[2] == "stop_too_wide"        # 14% away


def test_a_closed_bar_raises_the_trend_stop_and_never_lowers_it():
    bars = to_bars(trend_rows())
    entry_bar = bars[-4].t
    high = max(b.h for b in bars[-3:])
    reason, stop, seen_high = rules.bar_exit("trend", bars, entry_bar, 100.0, 1.0, 0.0, C, SC["trend"])
    assert (reason, seen_high) == (None, high) and stop == pytest.approx(high - 2.0)
    assert rules.bar_exit("trend", bars, entry_bar, 999.0, 1.0, 0.0, C, SC["trend"])[1] == 999.0
    assert rules.bar_exit("trend", bars, bars[-1].t, 100.0, 1.0, 0.0, C, SC["trend"]) == (None, 100.0, 0.0)


def test_a_close_below_the_ema_ends_the_trend_and_time_ends_the_others():
    falling = to_bars(trend_rows()[:-1] + [row(B0, 130, 130, 120, 121)])
    assert rules.bar_exit("trend", falling, falling[-3].t, 100.0, 1.0, 0.0, C, SC["trend"])[0] == "trend_exit"
    bars = to_bars(flat(B0, H4, 120))
    held = int(SC["break"]["time_stop_bars"])
    assert rules.bar_exit("break", bars, bars[-1].t - H4 * held, 90.0, 1.0, 0.0, C, SC["break"])[0] == "time"
    assert rules.bar_exit("break", bars, bars[-1].t - H4 * (held - 1), 90.0, 1.0, 0.0, C, SC["break"])[0] is None


# ---------------------------------------------------------------- sizing and the book

def test_a_price_is_a_whole_number_of_ticks_whatever_the_tick():
    assert to_tick(Decimal("100.07"), Decimal("0.05")) == Decimal("100.05")
    assert to_tick(Decimal("100.07"), Decimal("0.05"), "ROUND_UP") == Decimal("100.10")
    assert to_tick(Decimal("0.1234567"), Decimal("0.0000001")) == Decimal("0.1234567")
    assert to_lot(Decimal("1.123456789"), 8) == Decimal("1.12345678")


def test_sizing_never_passes_a_limit_for_any_of_the_eight_pairs():
    lim, rng = risk.load_sleeve_limits("CT"), random.Random(11)
    for kraken_pair, info in INFO.items():
        for _ in range(300):
            price = to_tick(Decimal(str(rng.choice([0.08, 0.5, 2.4, 18, 150, 2700, 85000]) * rng.uniform(0.7, 1.3))),
                            info.tick, "ROUND_UP")
            stop = to_tick(price * Decimal(str(1 - rng.uniform(0.01, 0.12))), info.tick)
            equity = Decimal(str(round(rng.uniform(2_000, 30_000), 2)))
            cash = equity * Decimal(str(rng.uniform(0.0, 1.0)))
            qty = to_lot(risk.size(equity, cash, price, stop, 0.40, lim), info.lot_decimals)
            cost = qty * price
            assert qty >= 0 and cost * Decimal("1.004") <= cash, kraken_pair
            assert cost <= equity * Decimal("0.30") and qty * (price - stop) <= equity * Decimal("0.01"), kraken_pair
            assert (price / info.tick) % 1 == 0 and (stop / info.tick) % 1 == 0, kraken_pair
    assert risk.size(Decimal(10_000), Decimal(10_000), Decimal(100), Decimal(100), 0.4, lim) == 0


def test_a_book_written_before_the_sleeves_existed_loads_as_it_was(tmp_path):
    old = {"cash": "9950", "start_equity": "10000", "realised": {}, "entries": {}, "orders": {}, "outbox": [],
           "positions": {"BTC/USD": {"pair": "BTC/USD", "qty": "0.0005", "entry_price": "85000", "entry_fee": "0.17",
                                     "entry_t": 1, "entry_bar": 0, "stop": "84575", "target": "85850", "checked_to": 0}}}
    (tmp_path / "book.json").write_text(json.dumps(old))
    p = Book.load(tmp_path / "book.json", Decimal(10_000)).positions["BTC/USD"]
    assert (p.sleeve, p.atr, p.high, p.bar_checked) == ("", 0, 0, 0) and p.stop == Decimal("84575")


def test_pair_metadata_is_matched_on_the_short_name_kraken_was_asked_for():
    got = venue().api().pair_infos(list(PAIRS.values()))
    assert set(got) == set(PAIRS.values()) and got["XBTUSD"] == INFO["XBTUSD"] and got["XDGUSD"].order_min == 50


# ---------------------------------------------------------------- the cycle

def test_with_the_kill_switch_on_every_sleeve_records_what_it_would_do_and_opens_nothing(crypto):
    d, _, _ = crypto
    d.kill_file.write_text("on")
    v = venue({"XBTUSD": break_rows(), "ETHUSD": trend_rows(), "SOLUSD": dip_rows()})
    assert run(v, crypto) == 0
    for sleeve, pair in (("break", "BTC/USD"), ("trend", "ETH/USD"), ("dip", "SOL/USD")):
        refused = rows_of(d, sleeve, "refused")
        assert (pair, ["kill"]) in [(r["pair"], r["why"]) for r in refused], sleeve
        assert all(r["why"] == ["kill"] for r in refused)                            # a jump can be two rules' signal
        assert refused[0]["stage"] == "incubation" and refused[0]["strategy"].startswith("HYP-002")
        assert book_of(d, sleeve).positions == {} and book_of(d, sleeve).cash == Decimal(10_000)
        assert len(rows_of(d, sleeve, "sleeve")) == 1 and len(rows_of(d, sleeve, "sleeve")[0]["pairs"]) == 8
    assert ledger.verify_chain(d.journal) == []
    assert [r["kind"] for r in journal(d) if not r.get("sleeve")] == ["cycle"]       # the baseline's own row, as before


def test_bars_of_every_timeframe_are_stored_although_they_share_timestamps(crypto):
    d, _, _ = crypto
    d.kill_file.write_text("on")
    run(venue(), crypto)
    for tf, n in ((15, 60), (240, 120), (1440, 60)):
        lines = (d.state_dir / "bars" / f"XBTUSD-{tf}m.jsonl").read_text().splitlines()
        assert len(lines) == n, tf
    state = json.loads((d.state_dir / "sleeves" / "data.json").read_text())
    assert state["stored_to"]["XBTUSD|240"] == B0 and state["stored_to"]["XBTUSD|1440"] == B0 - DAY
    obs = [json.loads(x) for x in (d.state_dir / "observations" / "sleeve-2026-10-04.jsonl").read_text().splitlines()]
    assert len(obs) == 24 and {(o["sleeve"], o["pair"]) for o in obs} == {(s, p) for s in rules.NAMES for p in PAIRS}
    assert all(o["tf"] == 240 for o in obs)


def test_a_breakout_is_bought_sized_from_its_stop_and_sold_at_its_target(crypto):
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    e = rows_of(d, "break", "entry")
    assert len(e) == 1 and e[0]["pair"] == "BTC/USD" and e[0]["bar"] == B0 and e[0]["stage"] == "incubation"
    price, stop, target, qty = (Decimal(e[0][k]) for k in ("price", "stop", "target", "qty"))
    atr = Decimal(str(e[0]["atr"]))
    assert price == Decimal("104.2") and (price / Decimal("0.1")) % 1 == 0          # ask 104.05 plus 5 bps, up to the tick
    assert stop == to_tick(price - 2 * atr, Decimal("0.1")) and target == to_tick(price + 4 * atr, Decimal("0.1"))
    assert qty * price <= Decimal(3_000) and qty * price > Decimal(2_990)            # capped at 30% of the book
    assert qty * (price - stop) <= Decimal(100)                                      # and so risking under 1%
    book = book_of(d, "break")
    assert book.cash == Decimal(10_000) - qty * price - Decimal(e[0]["fee"]) and list(book.positions) == ["BTC/USD"]
    assert rows_of(d, "dip", "entry") == [] and book_of(d, "dip").cash == Decimal(10_000)   # each book is its own

    v.now += 900                                                                     # the next cycle: price runs
    minute = B0 + H4 + 60
    v.ohlc[("XBTUSD", 1)] = ([row(minute, 104.2, float(target) + 1, 104.1, float(target) + 0.5)], minute)
    v.quote["XBTUSD"] = (float(target) + 0.4, float(target) + 0.5)
    run(v, crypto)
    x = rows_of(d, "break", "exit")
    assert len(x) == 1 and x[0]["reason"] == "target" and Decimal(x[0]["exit_price"]) == target
    assert Decimal(x[0]["pnl"]) > 0 and x[0]["r"] > 1 and book_of(d, "break").positions == {}
    assert book_of(d, "break").cash > Decimal(10_000)
    assert ledger.verify_chain(d.journal) == []
    run(v, crypto)                                                                   # the same bar again: nothing new
    assert len(rows_of(d, "break", "entry")) == 1 and len(rows_of(d, "break", "exit")) == 1


def test_a_stop_is_taken_with_slippage_and_a_gap_fills_at_the_open(crypto):
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    stop = Decimal(rows_of(d, "break", "entry")[0]["stop"])
    v.now += 900
    minute = B0 + H4 + 60
    v.ohlc[("XBTUSD", 1)] = ([row(minute, float(stop) - 1, float(stop) - 0.5, float(stop) - 2, float(stop) - 1.5)], minute)
    run(v, crypto)
    x = rows_of(d, "break", "exit")[0]
    assert x["reason"] == "stop" and Decimal(x["exit_price"]) < stop - 1 and Decimal(x["pnl"]) < 0 and x["r"] < -1


def test_a_trend_position_has_its_stop_raised_on_a_closed_bar_then_leaves_when_the_trend_ends(crypto):
    d, _, _ = crypto
    v = venue({"ETHUSD": trend_rows()})
    run(v, crypto)
    e = rows_of(d, "trend", "entry")[0]
    assert e["target"] is None and book_of(d, "trend").positions["ETH/USD"].target == Decimal("Infinity")
    first_stop = Decimal(e["stop"])
    up = trend_rows() + [row(B0 + H4, 137.2, 143.0, 137.0, 142.5)]                   # a strong next bar closes
    v.now = B0 + 2 * H4 + 10
    v.fill("ETHUSD", up[1:], 142.5)
    v.ohlc[("ETHUSD", 1)] = ([row(B0 + H4 + 60 * i, 140, 140.5, 139.5, 140) for i in range(1, 240)], B0 + 2 * H4 - 60)
    run(v, crypto)
    pos = book_of(d, "trend").positions["ETH/USD"]
    assert pos.stop > first_stop and pos.high == Decimal("143.0") and pos.bar_checked == B0 + H4
    assert pos.stop == to_tick(Decimal("143.0") - Decimal(str(SC["trend"]["trail_atr"])) * pos.atr, Decimal("0.01"))
    down = up + [row(B0 + 2 * H4, 142.5, 142.6, 131.0, 131.5)]                       # then a bar closes under EMA-20
    v.now = B0 + 3 * H4 + 10
    v.fill("ETHUSD", down[2:], 131.5)
    v.ohlc[("ETHUSD", 1)] = ([row(B0 + 2 * H4 + 60 * i, 142, 142.5, float(pos.stop) + 1, 142) for i in range(1, 240)],
                             B0 + 3 * H4 - 60)
    run(v, crypto)
    x = rows_of(d, "trend", "exit")
    assert len(x) == 1 and x[0]["reason"] == "trend_exit" and book_of(d, "trend").positions == {}


def test_a_signal_found_late_is_recorded_and_not_traded(crypto):
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    v.now = B0 + H4 + sleeves.FRESH_S + 60
    run(v, crypto)
    assert [r["why"] for r in rows_of(d, "break", "refused")] == [["late_bar"]] and rows_of(d, "break", "entry") == []


def test_every_signal_ends_as_an_entry_or_a_named_refusal(crypto):
    d, _, _ = crypto
    v = venue({k: break_rows() for k in PAIRS.values()})                            # eight breakouts at once
    run(v, crypto)
    entered, refused = rows_of(d, "break", "entry"), rows_of(d, "break", "refused")
    assert len(entered) == 3 and len(refused) == 5                                   # three positions is the limit
    assert all(set(r["why"]) & {"positions", "exposure", "insufficient_cash", "below_minimum"} for r in refused)
    book = book_of(d, "break")
    assert book.exposure() <= Decimal(10_000) and book.cash >= 0 and book.entries["2026-10-04"] == 3
    fired = [p for p, r in rows_of(d, "break", "sleeve")[0]["pairs"].items() if r["fire"]]
    assert sorted(fired) == sorted([r["pair"] for r in entered + refused])


# ---------------------------------------------------------------- isolation and budget

def test_one_pair_without_data_and_one_failing_rule_leave_the_rest_running(crypto, monkeypatch):
    d, _, _ = crypto
    v = venue({"ETHUSD": break_rows()})
    del v.ohlc[("XBTUSD", 240)]
    real = rules.entry

    def faulty(name, *a, **k):
        if name == "dip":
            raise RuntimeError("bug")
        return real(name, *a, **k)
    monkeypatch.setattr(rules, "entry", faulty)
    assert run(v, crypto) == 0
    assert [r["pair"] for r in rows_of(d, "break", "entry")] == ["ETH/USD"]
    assert "BTC/USD" not in rows_of(d, "trend", "sleeve")[0]["pairs"] and len(rows_of(d, "trend", "sleeve")[0]["pairs"]) == 7
    assert rows_of(d, "dip", "sleeve") == [] and (risk.sleeve_dir(d, "dip") / "book.json").exists()
    assert [r["kind"] for r in journal(d) if not r.get("sleeve")] == ["cycle"]


def test_a_fault_in_the_sleeves_never_fails_the_baseline_cycle(crypto, monkeypatch):
    d, a, box = crypto
    monkeypatch.setattr(sleeves, "run_all", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    assert run(venue(), crypto) == 0
    assert [r["kind"] for r in journal(d)] == ["cycle"] and "crypto:sleeves-failed" in a.firing()


def test_the_cycle_stops_asking_at_its_budget_saves_and_says_so(crypto, monkeypatch):
    d, _, _ = crypto
    d.kill_file.write_text("on")
    monkeypatch.setattr(sleeves, "MAX_CALLS", 14)
    v = venue({"XBTUSD": break_rows()})
    api = v.api()
    api.calls = 13                                                                   # what the baseline already used
    out = sleeves.run_all(v.now, api, d, CFG, crypto[1], time.monotonic(), cycle.flush)
    assert out["failed"] == {"BTC/USD": "budget"} and out["evaluated"] == {}      # one call left: the pair metadata
    assert api.calls == 14 and all((risk.sleeve_dir(d, n) / "book.json").exists() for n in rules.NAMES)
    assert [r for r in journal(d) if r.get("sleeve")] == []                          # nothing evaluated, nothing claimed
    with pytest.raises(DataError, match="budget"):
        sleeves.Budget(v.api(), time.monotonic() - 500).spend()                      # out of time, not of calls


def test_higher_timeframe_bars_are_fetched_once_per_closed_bar_not_every_cycle(crypto):
    d, _, _ = crypto
    d.kill_file.write_text("on")
    v = venue()
    run(v, crypto)
    first = [x for x in v.asked if x[0] == "OHLC"]
    v.asked.clear()
    v.now += 900
    run(v, crypto)
    again = [x for x in v.asked if x[0] == "OHLC"]
    assert len(first) == 3 + 16 and len(again) == 3                                  # only the baseline's 15-minute bars
    assert ("AssetPairs", ",".join(PAIRS.values())) not in v.asked                   # once a day


def test_bars_with_a_hole_are_not_an_indicator(crypto):
    d, _, _ = crypto
    d.kill_file.write_text("on")
    v = venue()
    holed = flat(B0, H4, 121)
    del holed[60]
    v.fill("XBTUSD", holed, 100.0)
    run(v, crypto)
    assert "BTC/USD" not in rows_of(d, "break", "sleeve")[0]["pairs"]


# ---------------------------------------------------------------- latch, controls, snapshot

def test_each_sleeve_has_its_own_latch_and_one_reset_clears_them_all(crypto, monkeypatch, tmp_path):
    from wt.crypto import control
    from wt.ops import locks
    d, _, _ = crypto
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    lim = risk.load_sleeve_limits("CT")
    book = Book(Decimal(9_600), Decimal(10_000))
    book.realised["2026-10-04"] = Decimal("-299")
    assert not risk.update_sleeve_latch(book, "2026-10-04", Decimal(10_000), risk.sleeve_dir(d, "break"), lim)
    book.realised["2026-10-04"] = Decimal("-300")
    assert risk.update_sleeve_latch(book, "2026-10-04", Decimal(10_000), risk.sleeve_dir(d, "break"), lim)
    args = ("BTC/USD", Decimal(100), Decimal(10_000), Book(Decimal(10_000), Decimal(10_000)), "2026-10-05", d)
    assert risk.sleeve_blockers(*args, risk.sleeve_dir(d, "break"), tuple(PAIRS), lim) == ["latch"]
    assert risk.sleeve_blockers(*args, risk.sleeve_dir(d, "trend"), tuple(PAIRS), lim) == []      # the others trade on
    assert risk.sleeve_blockers("PEPE/USD", *args[1:], risk.sleeve_dir(d, "trend"), tuple(PAIRS), lim) == ["not_allowlisted"]
    assert control.main(["reset-latch"], d) == 0 and not (risk.sleeve_dir(d, "break") / "latch").exists()
    assert journal(d)[-1]["action"] == "reset-latch" and "daily loss limit" in journal(d)[-1]["was"]


def test_the_baselines_snapshot_does_not_count_the_sleeves_trades(crypto):
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    v.now += 900
    minute, target = B0 + H4 + 60, float(rows_of(d, "break", "entry")[0]["target"])
    v.ohlc[("XBTUSD", 1)] = ([row(minute, 104.2, target + 1, 104.1, target + 0.5)], minute)
    run(v, crypto)
    assert len(rows_of(d, "break", "exit")) == 1
    snap = snapshot.collect(dt.datetime.fromtimestamp(v.now, dt.UTC), desk=d, cfg=CFG)
    assert snap["perf"]["trades"] == 0 and snap["book"]["positions"] == [] and snap["book"]["equity"] == 10_000.0
    assert snap["activity"]["entries_7d"] == 0


def test_the_snapshot_reports_each_sleeve_from_its_own_book_and_values_positions_at_the_last_bid(crypto):
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    when = dt.datetime.fromtimestamp(v.now, dt.UTC)
    snap = snapshot.collect(when, desk=d, cfg=CFG)
    by = {s["name"]: s for s in snap["sleeves"]}
    assert list(by) == ["trend", "break", "dip"] and all(s["stage"] == "failed" for s in by.values())   # each shown with its own gate C1 verdict (DEC-0016, 1)
    pos = by["break"]["positions"][0]
    entry = rows_of(d, "break", "entry")[0]
    assert pos["pair"] == "BTC/USD" and pos["entry_price"] == float(entry["price"]) and pos["mark"] == 103.95
    assert pos["unrealised"] == pytest.approx(pos["qty"] * (103.95 - pos["entry_price"]) - float(entry["fee"]))
    assert by["break"]["equity"] == pytest.approx(10_000 + pos["unrealised"]) and by["break"]["open_pnl"] == pos["unrealised"]
    assert pos["unrealised_r"] == pytest.approx(pos["unrealised"] / pos["risk"]) and by["break"]["trades"] == 0
    assert by["dip"]["equity"] == 10_000.0 and by["dip"]["positions"] == [] and by["dip"]["signals"] == []
    assert by["break"]["signals"][0]["outcome"] == "entered" and len(by["break"]["why_not"]) == 8
    assert by["trend"]["positions"][0]["target"] is None                              # no target: published as none

    v.now += 900
    minute, target = B0 + H4 + 60, float(entry["target"])
    v.ohlc[("XBTUSD", 1)] = ([row(minute, 104.2, target + 1, 104.1, target + 0.5)], minute)
    v.quote["XBTUSD"] = (target + 0.4, target + 0.5)
    run(v, crypto)
    snap = snapshot.collect(dt.datetime.fromtimestamp(v.now, dt.UTC), desk=d, cfg=CFG)
    b = next(s for s in snap["sleeves"] if s["name"] == "break")
    won = float(rows_of(d, "break", "exit")[0]["pnl"])
    assert (b["trades"], b["wins"], b["win_rate"], b["positions"]) == (1, 1, 1.0, [])
    assert b["pnl"]["total"] == won and b["pnl"]["today"] == won and b["equity"] == pytest.approx(10_000 + won)
    assert b["recent"][0]["reason"] == "target" and b["recent"][0]["entry_price"] == float(entry["price"])
    assert b["recent"][0]["entry_time"] == entry["t"] and b["recent"][0]["exit_price"] == target
    clean = snapshot.build(snap, publish.Sanitizer(lambda x: x, None, strict=False), "r", when)
    assert snapshot.validate(clean) == [] and len(clean["sleeves"]) == 3
    assert snap["perf"]["trades"] == 0 and snap["book"]["equity"] == 10_000.0        # the baseline's own figures


def test_a_fault_in_the_sleeves_view_leaves_the_baselines_snapshot_whole(crypto, monkeypatch):
    d, _, _ = crypto
    run(venue(), crypto)
    monkeypatch.setattr(snapshot, "sleeves_view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    snap = snapshot.collect(dt.datetime.fromtimestamp(NOW, dt.UTC), desk=d, cfg=CFG)
    assert snap["sleeves"] == [] and snap["book"]["equity"] == 10_000.0


def test_r_is_measured_against_the_risk_taken_at_entry_whatever_a_trailed_stop_does(crypto):
    d, _, _ = crypto
    v = venue({"ETHUSD": trend_rows()})
    run(v, crypto)
    entry = rows_of(d, "trend", "entry")[0]
    risk0 = Decimal(entry["risk"])
    up = trend_rows() + [row(B0 + H4, 137.2, 143.0, 137.0, 142.5)]                   # the stop is raised on this bar
    v.now = B0 + 2 * H4 + 10
    v.fill("ETHUSD", up[1:], 142.5)
    v.ohlc[("ETHUSD", 1)] = ([row(B0 + H4 + 60 * i, 140, 140.5, 139.5, 140) for i in range(1, 240)], B0 + 2 * H4 - 60)
    run(v, crypto)
    pos = book_of(d, "trend").positions["ETH/USD"]
    assert pos.risk0 == risk0.quantize(pos.risk0) or abs(pos.risk0 - risk0) < Decimal("0.01")
    assert pos.risk < pos.risk0 and pos.unit == pos.risk0                            # the stop moved; the unit did not
    v.now += 900
    minute = B0 + 2 * H4 + 60
    v.ohlc[("ETHUSD", 1)] = ([row(minute, float(pos.stop) - 0.5, float(pos.stop), float(pos.stop) - 1, float(pos.stop) - 0.8)], minute)
    run(v, crypto)
    x = rows_of(d, "trend", "exit")[0]
    assert x["reason"] == "stop" and x["r"] == pytest.approx(float(Decimal(x["pnl"]) / pos.risk0), abs=0.002)
    snap = snapshot.collect(dt.datetime.fromtimestamp(v.now, dt.UTC), desk=d, cfg=CFG)
    t = next(s for s in snap["sleeves"] if s["name"] == "trend")
    assert t["recent"][0]["r"] == x["r"] and t["mean_r"] == x["r"]


def test_an_exit_booked_against_a_moved_stop_is_shown_against_its_entry_risk():
    """The desk's first TREND trade, as journalled on 2026-10-07: booked at -1.371R against the raised stop."""
    rows = [{"id": "e", "kind": "entry", "sleeve": "trend", "pair": "AVAX/USD", "t": "2026-10-06T16:00:12+00:00",
             "risk": "100.00"},
            {"id": "x", "kind": "exit", "sleeve": "trend", "pair": "AVAX/USD", "entry_t": "2026-10-06T16:00:12+00:00",
             "t": "2026-10-07T02:01:00+00:00", "pnl": "-79.88", "r": -1.371},
            {"id": "old", "kind": "exit", "sleeve": "break", "pair": "SOL/USD", "entry_t": "nowhere", "pnl": "5", "r": 0.4},
            {"id": "base", "kind": "exit", "pair": "BTC/USD", "pnl": "1", "r": 2.0}]
    assert snapshot.exit_r(rows) == {"x": -0.799, "old": 0.4}                        # the baseline's rows are not touched


# ---------------------------------------------------------------- the signal record (DEC-0016, 2)

def test_every_signal_is_recorded_with_its_inputs_whether_or_not_it_was_bought(crypto):
    from wt.crypto import signals
    d, _, _ = crypto
    d.kill_file.write_text("on")
    v = venue({"XBTUSD": break_rows(), "ETHUSD": break_rows()})
    run(v, crypto)
    recs = rows_of(d, "break", "signal")
    assert [(r["pair"], r["taken"], r["why"]) for r in recs] == [("BTC/USD", False, ["kill"]), ("ETH/USD", False, ["kill"])]
    r = recs[0]
    assert r["sid"] == f"break|BTC/USD|{B0}" and r["bar"] == B0 and r["stage"] == "incubation" and r["strategy"] == "HYP-0022"
    assert tuple(r["inputs"]) == signals.INPUTS and len(CFG["learning"]["inputs"]) == len(signals.INPUTS)
    assert list(r["inputs"]) == CFG["learning"]["inputs"]                        # the charter's list, in its order
    i = r["inputs"]
    assert i["breadth"] == 2.0 and i["held_elsewhere"] == 0.0 and i["hour_utc"] == 4.0 and i["day_of_week"] == 6.0
    assert i["volume_ratio"] == 5.0 and i["btc_above_sma50"] in (0.0, 1.0) and i["spread_pct"] is not None
    assert i["stop_pct"] == pytest.approx((r["price"] - r["stop"]) / r["price"] * 100, abs=1e-3) and r["target"] > r["price"]
    assert ledger.verify_chain(d.journal) == []
    snap = snapshot.collect(dt.datetime.fromtimestamp(v.now, dt.UTC), desk=d, cfg=CFG)
    assert snap["perf"]["trades"] == 0 and snap["activity"]["entries_7d"] == 0   # the baseline's figures do not move


def test_a_bought_signal_carries_the_positions_own_levels_and_a_late_one_is_still_recorded(crypto):
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    sig, entry = rows_of(d, "break", "signal")[0], rows_of(d, "break", "entry")[0]
    assert sig["taken"] is True and sig["why"] == []
    assert (sig["price"], sig["stop"], sig["target"]) == (float(entry["price"]), float(entry["stop"]), float(entry["target"]))
    assert rows_of(d, "trend", "signal")[0]["inputs"]["held_elsewhere"] in (0.0, 1.0)
    # one row per signal bar, however many times the cycle runs
    run(v, crypto)
    assert len(rows_of(d, "break", "signal")) == 1


def test_a_signal_found_late_is_recorded_as_not_taken(crypto):
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    v.now = B0 + H4 + sleeves.FRESH_S + 60
    run(v, crypto)
    sig = rows_of(d, "break", "signal")[0]
    assert sig["taken"] is False and sig["why"] == ["late_bar"] and sig["inputs"]["spread_pct"] is None
