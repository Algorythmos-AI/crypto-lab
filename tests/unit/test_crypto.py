"""The crypto desk (wt.crypto): recorded Kraken responses and synthetic bars only; nothing here touches a network."""
from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from wt.core import desk as desks
from wt.core import ledger
from wt.crypto import cycle, features, indicators, labels, risk
from wt.crypto.book import Book, Rejected
from wt.crypto.data import Bar, DataError, KrakenPublic, PairInfo, Quote
from wt.crypto.quality import assess
from wt.crypto.strategy import Params, Reading, entry, find_exit, read
from wt.ops import alerts as alerts_mod

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests/fixtures/kraken"
CFG = yaml.safe_load((ROOT / "config/crypto.yaml").read_text())
P = Params.of(CFG["strategy"])
INFO = PairInfo(8, Decimal("0.1"), Decimal("0.00005"), Decimal("1"))
DAY = 86_400
T0 = 1_791_072_000                                       # 2026-10-04 00:00:00 UTC, a 15-minute boundary
assert T0 % DAY == 0


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class Venue:
    """Answers the four public endpoints from dictionaries the test fills in."""

    def __init__(self):
        self.ohlc: dict[tuple[str, int], tuple[list, int]] = {}
        self.quote: dict[str, tuple[float, float]] = {}
        self.now = T0
        self.down = False
        self.pairs = fx("assetpairs.json")["result"]

    def get(self, url: str, params: dict):
        if self.down:
            raise OSError("down")
        name = url.rsplit("/", 1)[1]
        if name == "Time":
            return {"error": [], "result": {"unixtime": int(self.now)}}
        pair = params["pair"]
        if name == "OHLC":
            rows, last = self.ohlc[(pair, int(params["interval"]))]
            since = params.get("since")
            return {"error": [], "result": {pair: [r for r in rows if since is None or r[0] > since], "last": last}}
        if name == "Ticker":
            b, a = self.quote[pair]
            return {"error": [], "result": {pair: {"a": [str(a), "1", "1"], "b": [str(b), "1", "1"], "c": [str(a), "1"]}}}
        if name == "AssetPairs":
            return {"error": [], "result": {pair: self.pairs[pair]}}
        raise AssertionError(name)

    def api(self) -> KrakenPublic:
        return KrakenPublic(get=self.get, min_interval_s=0)


def row(t, o, h, l, c, v=1.0, n=3, vwap=None):  # noqa: E741
    return [t, str(o), str(h), str(l), str(c), str(c if vwap is None else vwap), str(v), n]


def bar(t, c, o=None, h=None, l=None, v=1.0, n=3, vwap=None) -> Bar:  # noqa: E741
    o = c if o is None else o
    return Bar(t, o, max(o, c) if h is None else h, min(o, c) if l is None else l, c, c if vwap is None else vwap, v, n)


# ---------------------------------------------------------------- data

def test_the_bar_still_forming_is_never_returned():
    raw = fx("ohlc15_XBTAUD.json")
    rows, last = raw["result"]["XBTAUD"], raw["result"]["last"]
    assert rows[-1][0] > last                                      # the recording ends with Kraken's open bar
    bars = KrakenPublic(get=lambda url, params: raw, min_interval_s=0).ohlc("XBTAUD", 15)
    assert len(bars) == len(rows) - 1 and bars[-1].t == last
    assert all(b.t % 900 == 0 for b in bars)


def test_recorded_aud_pairs_have_bars_nobody_traded_in():
    """Why gate C0 exists (DEC-0012): on the day of the recording a large share of SOL/AUD bars had no trade."""
    bars = KrakenPublic(get=lambda url, params: fx("ohlc15_SOLAUD.json"), min_interval_s=0).ohlc("SOLAUD", 15)
    quiet = [b for b in bars if not b.traded]
    assert len(quiet) > 10 and all(b.vwap == 0 and b.v == 0 and b.o == b.h == b.l == b.c for b in quiet)


@pytest.mark.parametrize("body", [
    {"error": ["EQuery:Unknown asset pair"], "result": {}},
    {"error": []},
    "not json",
    {"error": [], "result": {"XBTAUD": [[1, "x"]], "last": 1}},
    {"error": [], "result": {"XBTAUD": [row(900, 1, 1, 1, 1), row(450, 1, 1, 1, 1)], "last": 900}},   # off the grid
])
def test_every_bad_answer_is_a_data_error(body):
    with pytest.raises(DataError):
        KrakenPublic(get=lambda url, params: body, min_interval_s=0).ohlc("XBTAUD", 15)


def test_a_dead_connection_and_a_crossed_book_are_data_errors():
    v = Venue()
    v.down = True
    with pytest.raises(DataError):
        v.api().time()
    v.down, v.quote["XBTAUD"] = False, (101.0, 100.0)
    with pytest.raises(DataError):
        v.api().ticker("XBTAUD")


def test_ticker_and_pair_info_from_the_recordings():
    q = KrakenPublic(get=lambda url, params: {"error": [], "result": {"XBTAUD": fx("ticker.json")["result"]["XBTAUD"]}},
                     min_interval_s=0).ticker("XBTAUD")
    assert 0 < q.bid < q.ask and 0 < q.spread_pct < 1
    info = Venue().api().pair_info("XBTAUD")
    assert info == PairInfo(8, Decimal("0.1"), Decimal("0.00005"), Decimal("1"))


def test_calls_are_spaced():
    waits = []
    api = KrakenPublic(get=lambda url, params: {"error": [], "result": {"unixtime": 1}}, min_interval_s=5,
                       sleep=waits.append)
    api.time()
    api.time()
    assert len(waits) == 1 and 4 < waits[0] <= 5


# ---------------------------------------------------------------- indicators (hand-computed)

def test_ema_rsi_and_atr_against_hand_computed_values():
    assert indicators.ema([1, 2, 3, 4, 5], 3) == pytest.approx(4.0)        # seed 2; then 3; then 4
    assert indicators.ema([1, 2], 3) is None
    # n=2: changes +1 +1 -1 -> seed gain 1, loss 0; then gain .5, loss .5 -> RS 1 -> 50
    assert indicators.rsi([1, 2, 3, 2], 2) == pytest.approx(50.0)
    assert indicators.rsi([1, 2, 3, 4], 2) == 100.0
    assert indicators.rsi([5, 5, 5, 5], 2) is None                         # no movement: not 50, not 0
    assert indicators.rsi([1, 2], 2) is None
    bars = [bar(0, 10, h=11, l=9), bar(900, 11, h=12, l=10), bar(1800, 10, h=13, l=10), bar(2700, 12, h=12, l=9)]
    # true ranges: 2, 3, 3 -> n=2: seed 2.5, then (2.5 + 3) / 2
    assert indicators.atr(bars, 2) == pytest.approx(2.75)


def test_session_vwap_uses_only_traded_bars_of_the_last_bars_utc_day():
    bars = [bar(T0 - 900, 50, v=100),                                      # yesterday: ignored
            bar(T0, 100, v=1, vwap=100), bar(T0 + 900, 100, v=0, n=0, vwap=0), bar(T0 + 1800, 110, v=3, vwap=110)]
    assert indicators.session_vwap(bars) == pytest.approx((100 * 1 + 110 * 3) / 4)
    assert indicators.session_vwap([bar(T0, 100, v=0, n=0, vwap=0)]) is None


def test_macd_needs_slow_plus_signal_bars():
    assert indicators.macd([float(i) for i in range(34)]) is None
    line, sig, hist = indicators.macd([float(i) for i in range(60)])
    assert line == pytest.approx(7.0) and hist == pytest.approx(line - sig)   # a straight line: EMA lag difference


# ---------------------------------------------------------------- quality

def test_a_pair_is_tradable_only_when_its_last_bar_traded_and_the_quote_is_tight_and_fresh():
    q = CFG["quality"]
    now = T0 + 900 + 10
    good = [bar(T0 - 900, 100), bar(T0, 100)]
    assert assess(good, Quote(99.95, 100.05, now), now, 15, q).tradable
    assert assess(good[:1] + [bar(T0, 100, v=0, n=0, vwap=0)], Quote(99.95, 100.05, now), now, 15, q).reasons == ("bar_untraded",)
    assert assess(good, Quote(99.0, 101.0, now), now, 15, q).reasons == ("spread_wide",)
    assert assess(good, Quote(99.95, 100.05, now - 60), now, 15, q).reasons == ("quote_stale",)
    assert assess(good, None, now, 15, q).reasons == ("no_quote",)
    assert assess(good, Quote(99.95, 100.05, now + 3600), now + 3600, 15, q).reasons == ("bar_stale",)
    assert assess([], None, now, 15, q).reasons == ("no_bars",)
    assert assess(good[:1] + [bar(T0, 100, v=0, n=0, vwap=0)], Quote(99.95, 100.05, now), now, 15, q).traded_share == 0.5


# ---------------------------------------------------------------- strategy

def test_entry_needs_all_three_conditions_and_a_missing_indicator_fails_its_condition():
    assert entry(Reading(100, 25, 99, 99), P) == (True, ())
    assert entry(Reading(100, 40, 99, 99), P) == (False, ("rsi_high",))
    assert entry(Reading(100, 25, 101, 99), P) == (False, ("below_ema",))
    assert entry(Reading(100, 25, 99, 101), P) == (False, ("below_vwap",))
    assert entry(Reading(100, None, None, None), P) == (False, ("no_vwap", "no_ema", "no_rsi"))
    assert entry(Reading(100, float(P.rsi_below), 99, 99), P)[0] is False          # below, not at


def test_exit_order_inside_and_across_bars():
    stop, target = 99.5, 101.0
    assert find_exit([bar(0, 100, h=100.9, l=99.6)], stop, target) is None
    assert find_exit([bar(0, 100, h=101.0, l=99.6)], stop, target) is None             # a touch is not a fill
    assert find_exit([bar(0, 100, h=101.2, l=99.6)], stop, target).reason == "target"
    both = find_exit([bar(0, 100, h=101.2, l=99.4)], stop, target)
    assert (both.reason, both.price) == ("stop", 99.5)                                  # unknown order: the stop
    gap = find_exit([bar(0, 98.0, o=98.5, h=98.6, l=97.9)], stop, target)
    assert (gap.reason, gap.price) == ("stop", 98.5)                                    # gapped through: the open
    first = find_exit([bar(0, 100, h=101.2, l=99.9), bar(60, 99, h=99.9, l=99.0)], stop, target)
    assert (first.reason, first.t) == ("target", 0)                                     # time order
    assert find_exit([bar(0, 90, v=0, n=0, vwap=0)], stop, target) is None              # nobody traded there


# ---------------------------------------------------------------- book

def test_a_buy_rounds_down_to_the_lot_pays_fee_and_slippage_and_sets_its_levels():
    b = Book(Decimal(10000), Decimal(10000))
    p = b.buy("BTC/AUD", Decimal(50), 122370.8, INFO, 0.40, 5, T0 + 10, T0 - 900, 0.5, 1.0)
    assert p.entry_price == Decimal("122432.0")                           # ask * 1.0005, on the 0.1 tick
    assert p.qty == Decimal("0.00040838") and p.qty * p.entry_price <= 50 < (p.qty + Decimal("1e-8")) * p.entry_price
    assert p.entry_fee == p.cost * Decimal("0.004")
    assert b.cash == Decimal(10000) - p.cost - p.entry_fee
    assert (p.stop, p.target) == (Decimal("121819.8"), Decimal("123656.3"))
    assert b.entries == {"2026-10-04": 1} and b.orders == {"2026-10-04": 1} and b.exposure() == p.cost
    with pytest.raises(Rejected, match="already_open"):
        b.buy("BTC/AUD", Decimal(50), 122370.8, INFO, 0.40, 5, T0, T0, 0.5, 1.0)


def test_orders_below_the_venue_minimum_or_beyond_cash_are_rejected():
    b = Book(Decimal(10), Decimal(10))
    with pytest.raises(Rejected, match="below_minimum"):
        b.buy("BTC/AUD", Decimal(5), 122370.8, INFO, 0.40, 5, T0, T0, 0.5, 1.0)       # 0.00004 BTC < 0.00005
    with pytest.raises(Rejected, match="insufficient_cash"):
        b.buy("BTC/AUD", Decimal(50), 122370.8, INFO, 0.40, 5, T0, T0, 0.5, 1.0)
    assert b.cash == 10 and not b.positions and not b.entries


def test_a_win_at_the_target_nets_far_less_than_the_target_after_two_fees():
    """The arithmetic DEC-0012 states: a 1.0% target with 0.40% a side leaves about 0.2%."""
    b = Book(Decimal(10000), Decimal(10000))
    p = b.buy("BTC/AUD", Decimal(50), 100000.0, INFO, 0.40, 0, T0, T0, 0.5, 1.0)
    cost = p.cost
    win = b.sell("BTC/AUD", float(p.target), 0.40, 0, T0 + 600)
    assert Decimal(win["pnl"]) / cost * 100 == pytest.approx(Decimal("0.196"), abs=Decimal("0.01"))
    assert win["r"] == pytest.approx(0.39, abs=0.02) and win["held_s"] == 600
    assert b.cash == Decimal(10000) + Decimal(win["pnl"]).quantize(Decimal("0.01")) or abs(b.cash - 10000 - Decimal(win["pnl"])) < Decimal("0.01")
    p = b.buy("BTC/AUD", Decimal(50), 100000.0, INFO, 0.40, 0, T0, T0, 0.5, 1.0)
    loss = b.sell("BTC/AUD", float(p.stop), 0.40, 0, T0 + 900)
    assert Decimal(loss["pnl"]) / cost * 100 == pytest.approx(Decimal("-1.298"), abs=Decimal("0.01"))
    assert loss["r"] == pytest.approx(-2.6, abs=0.05)
    assert b.realised["2026-10-04"] == pytest.approx(Decimal(win["pnl"]) + Decimal(loss["pnl"]), abs=Decimal("0.01"))


def test_the_book_survives_a_save_and_load_exactly(tmp_path):
    b = Book(Decimal(10000), Decimal(10000))
    b.buy("BTC/AUD", Decimal(50), 122370.8, INFO, 0.40, 5, T0 + 10, T0 - 900, 0.5, 1.0)
    b.note({"kind": "entry"})
    b.meta["last_bar"]["BTC/AUD"] = T0 - 900
    b.save(tmp_path / "book.json")
    c = Book.load(tmp_path / "book.json", Decimal(1))
    assert (c.cash, c.start_equity, c.positions, c.entries, c.orders, c.outbox, c.meta) == \
           (b.cash, b.start_equity, b.positions, b.entries, b.orders, b.outbox, b.meta)
    assert not list(tmp_path.glob(".*.tmp"))
    assert Book.load(tmp_path / "missing.json", Decimal(600)).cash == 600


# ---------------------------------------------------------------- risk

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


LIMITS = risk.Limits(("BTC/AUD", "ETH/AUD", "SOL/AUD"), Decimal(50), Decimal(120), 2, 20, Decimal(20))


def test_limits_load_from_the_risk_file():
    lim = risk.load_limits()
    assert lim.allowlist == tuple(CFG["pairs"]) and lim.max_notional == 50 and lim.daily_loss_latch == 20


def test_each_limit_blocks_entries_with_its_own_code(crypto, monkeypatch):
    desk, _, _ = crypto
    day, b = "2026-10-04", Book(Decimal(10000), Decimal(10000))

    def why(pair="BTC/AUD", notional=Decimal(50)):
        return risk.entry_blockers(pair, notional, b, day, desk, LIMITS)
    assert why() == []
    assert why("DOGE/AUD") == ["not_allowlisted"] and why(notional=Decimal(51)) == ["notional"]
    desk.kill_file.write_text("x")
    assert why() == ["kill"]
    desk.kill_file.unlink()
    desk.chain_flag.write_text("x")
    assert why() == ["chain_broken"]
    desk.chain_flag.unlink()
    monkeypatch.setenv("WT_ROLE", "shadow")
    assert why() == ["shadow_role"]
    monkeypatch.delenv("WT_ROLE")
    b.buy("BTC/AUD", Decimal(50), 100000.0, INFO, 0.40, 0, T0, T0, 0.5, 1.0)
    assert why() == ["already_open"] and why("ETH/AUD") == []
    b.buy("ETH/AUD", Decimal(50), 4000.0, PairInfo(8, Decimal("0.01"), Decimal("0.001"), Decimal(1)), 0.40, 0, T0, T0, 0.5, 1.0)
    assert why("SOL/AUD") == ["exposure", "entries_today"]                    # 100 open + 50 > 120; 2 entries today
    assert risk.entry_blockers("SOL/AUD", Decimal(10), b, "2026-10-05", desk, LIMITS) == []      # a new UTC day


def test_the_loss_latch_is_set_by_realised_loss_and_stays_until_removed(crypto):
    desk, _, _ = crypto
    b = Book(Decimal(10000), Decimal(10000))
    b.realised["2026-10-04"] = Decimal("-19.99")
    assert not risk.update_latch(b, "2026-10-04", desk, LIMITS)
    b.realised["2026-10-04"] = Decimal("-20")
    assert risk.update_latch(b, "2026-10-04", desk, LIMITS) and not risk.update_latch(b, "2026-10-04", desk, LIMITS)
    assert risk.entry_blockers("BTC/AUD", Decimal(50), b, "2026-10-09", desk, LIMITS) == ["latch"]   # days later too


# ---------------------------------------------------------------- features and labels

def test_features_are_the_fourteen_columns_and_none_where_the_input_cannot_support_them():
    flat = [bar(T0 + i * 900, 100, v=0, n=0, vwap=0) for i in range(40)]
    f = features.extract(flat)
    assert tuple(f) == features.COLUMNS and len(f) == 14
    assert f["rsi"] is None and f["volume_ratio"] is None and f["vwap_distance_pct"] is None
    assert f["hour_utc"] == 9.0 and f["day_of_week"] == 6.0                 # 09:45 UTC on a Sunday
    bars = [bar(T0 + i * 900, 100 + i, v=2.0) for i in range(40)]
    g = features.extract(bars)
    assert g["rsi"] == 100.0 and g["volume_ratio"] == 1.0 and g["momentum_5"] == pytest.approx(5 / 134 * 100, abs=1e-5)


def test_labels_take_costs_and_wait_for_the_time_stop():
    p = dataclasses.replace(P, time_stop_bars=3)
    later = [bar(900, 100.2, h=100.4, l=99.9), bar(1800, 101.5, h=101.6, l=100.1)]
    win = labels.label(100.0, later, p, 0.40, 5)
    assert win == {"label": "win", "exit_bars": 2, "gross_pct": 1.0, "net_pct": pytest.approx(0.1)}
    assert labels.label(100.0, [bar(900, 99, o=100, h=100, l=98.9)], p, 0.40, 5)["net_pct"] == pytest.approx(-1.4)
    # a bar that opens below the stop fills at its open, not at the stop
    assert labels.label(100.0, [bar(900, 99, h=99.2, l=98.9)], p, 0.40, 5)["net_pct"] == pytest.approx(-1.9)
    assert labels.label(100.0, later[:1], p, 0.40, 5)["label"] == "pending"
    drift = [bar(900 * i, 100.3, h=100.4, l=99.9) for i in range(1, 4)]
    assert labels.label(100.0, drift, p, 0.40, 5) == {"label": "neither", "exit_bars": 3, "gross_pct": 0.3,
                                                       "net_pct": pytest.approx(-0.6)}


# ---------------------------------------------------------------- the cycle

def firing_rows(end: int) -> list:
    """31 bars sliding half a point each, then an up bar that opens a new UTC day: RSI 27.8, above EMA-8 and above
    the session VWAP (the only bar of its day). `end` is the open time of that last bar."""
    closes = [115 - 0.5 * i for i in range(31)] + [102.5]
    rows = [row(end - 900 * (len(closes) - 1 - i), c, c + 0.1, c - 0.1, c) for i, c in enumerate(closes)]
    rows[-1] = row(end, 100.0, 102.6, 99.9, 102.5, vwap=102.0)
    return rows


def venue_with_a_signal(pairs=("XBTAUD",)) -> Venue:
    v = Venue()
    v.now = T0 + 900 + 10                                           # ten seconds after the T0 bar closed
    for k in ("XBTAUD", "ETHAUD", "SOLAUD"):
        rows = firing_rows(T0) if k in pairs else [row(T0 - 900 * (40 - i), 100, 100.1, 99.9, 100) for i in range(41)]
        v.ohlc[(k, 15)] = (rows + [row(T0 + 900, 102.5, 102.5, 102.5, 102.5)], T0)        # plus the bar still forming
        v.ohlc[(k, 1)] = ([], T0)
        v.quote[k] = (102.45, 102.55)
    return v


def journal(desk) -> list[dict]:
    return [json.loads(x) for x in desk.journal.read_text().splitlines()] if desk.journal.exists() else []


def run(v: Venue, crypto, **kw) -> int:
    desk, a, _ = crypto
    return cycle.run(now=v.now, api=v.api(), desk=desk, cfg=CFG, limits=LIMITS, alerts=a, **kw)


def test_the_synthetic_signal_is_what_the_test_says_it_is():
    bars = venue_with_a_signal().api().ohlc("XBTAUD", 15)
    r = read(bars, P)
    assert r.rsi == pytest.approx(27.78, abs=0.01) and r.close > r.ema and r.vwap == 102.0
    assert entry(r, P) == (True, ())


def test_with_the_kill_switch_on_a_signal_is_recorded_and_refused(crypto):
    desk, _, _ = crypto
    desk.kill_file.write_text("on")
    v = venue_with_a_signal()
    assert run(v, crypto) == 0
    kinds = [(r["kind"], r.get("why")) for r in journal(desk)]
    assert kinds == [("refused", ["kill"]), ("cycle", None)]
    assert ledger.verify_chain(desk.journal) == []
    book = Book.load(desk.state_dir / "book.json", Decimal(0))
    assert not book.positions and book.cash == CFG["account"]["start_equity"] and not book.outbox
    obs = [json.loads(x) for x in (desk.state_dir / "observations/obs-2026-10-04.jsonl").read_text().splitlines()]
    assert {o["pair"]: o["would_fire"] for o in obs} == {"BTC/AUD": True, "ETH/AUD": False, "SOL/AUD": False}
    assert set(obs[0]["features"]) == set(features.COLUMNS) and obs[0]["config"] == cycle.config_hash(CFG)
    stored = (desk.state_dir / "bars/XBTAUD-15m.jsonl").read_text().splitlines()
    assert len(stored) == 32 and json.loads(stored[-1])["t"] == T0          # closed bars only


def test_a_second_run_for_the_same_bar_does_nothing(crypto):
    desk, _, _ = crypto
    v = venue_with_a_signal()
    run(v, crypto)
    before = (desk.journal.read_bytes(), sorted(p.read_bytes() for p in desk.state_dir.rglob("*.jsonl")))
    v.now += 30
    run(v, crypto)
    assert (desk.journal.read_bytes(), sorted(p.read_bytes() for p in desk.state_dir.rglob("*.jsonl"))) == before


def test_an_entry_then_a_target_exit_then_the_same_journal_on_a_replay(crypto, tmp_path):
    desk, _, _ = crypto
    v = venue_with_a_signal()
    run(v, crypto)
    e = next(r for r in journal(desk) if r["kind"] == "entry")
    assert e["pair"] == "BTC/AUD" and e["bar"] == T0 and Decimal(e["qty"]) * Decimal(e["price"]) <= 50
    book = Book.load(desk.state_dir / "book.json", Decimal(0))
    pos = book.positions["BTC/AUD"]
    assert pos.checked_to == T0 + 900 and book.entries == {"2026-10-04": 1}

    # the next cycle: minute bars drift, then one trades through the target
    v.now = T0 + 1800 + 10
    v.ohlc[("XBTAUD", 15)] = (v.ohlc[("XBTAUD", 15)][0] + [row(T0 + 1800, 104, 104, 104, 104)], T0 + 900)
    tgt = float(pos.target)
    v.ohlc[("XBTAUD", 1)] = ([row(T0 + 900, 90, 90, 90, 90),                               # the entry minute: skipped
                              row(T0 + 960, 102.6, 102.9, 102.5, 102.8),
                              row(T0 + 1020, 102.8, tgt + 0.3, 102.7, tgt + 0.2),
                              row(T0 + 1080, 90, 90, 90, 90)], T0 + 1080)
    run(v, crypto)
    x = next(r for r in journal(desk) if r["kind"] == "exit")
    assert x["reason"] == "target" and Decimal(x["exit_price"]) == pos.target and x["held_s"] == 170      # filled 00:15:10, out at the close of the 00:17 minute
    assert 0 < x["r"] < 0.5                                                 # a full target is under half an R after fees
    # the bar that closed meanwhile fires too: exits come first, so the same cycle may open a new position
    after = Book.load(desk.state_dir / "book.json", Decimal(0))
    assert [r["kind"] for r in journal(desk)][-3:] == ["exit", "entry", "cycle"]
    assert after.positions["BTC/AUD"].entry_bar == T0 + 900 and after.entries == {"2026-10-04": 2}
    assert ledger.verify_chain(desk.journal) == []

    # replay both cycles into a fresh desk: the same records, apart from their ids and chain hashes
    other = dataclasses.replace(desk, state_dir=tmp_path / "again", kill_file=tmp_path / "again/KILL",
                                ledgers=(("crypto", tmp_path / "again/crypto_journal.jsonl"),),
                                chain_flag=tmp_path / "again/chain-broken")
    w = venue_with_a_signal()
    cycle.run(now=w.now, api=w.api(), desk=other, cfg=CFG, limits=LIMITS, alerts=crypto[1])
    w.now, w.ohlc = v.now, v.ohlc
    cycle.run(now=w.now, api=w.api(), desk=other, cfg=CFG, limits=LIMITS, alerts=crypto[1])

    def bare(rows):
        return [{k: v for k, v in r.items() if k not in ("id", "prev_sha256")} for r in rows]
    assert bare(journal(other)) == bare(journal(desk))


def test_a_stop_is_taken_before_a_later_target_and_a_loss_sets_the_latch(crypto):
    desk, a, box = crypto
    v = venue_with_a_signal()
    tight = dataclasses.replace(LIMITS, daily_loss_latch=Decimal("0.5"))
    cycle.run(now=v.now, api=v.api(), desk=desk, cfg=CFG, limits=tight, alerts=a)
    pos = Book.load(desk.state_dir / "book.json", Decimal(0)).positions["BTC/AUD"]
    v.now = T0 + 1800 + 10
    v.ohlc[("XBTAUD", 15)] = (v.ohlc[("XBTAUD", 15)][0], T0 + 900)
    v.ohlc[("XBTAUD", 1)] = ([row(T0 + 960, 102, 102.1, float(pos.stop) - 0.2, 102),
                              row(T0 + 1020, 104, 110, 104, 110)], T0 + 1020)
    cycle.run(now=v.now, api=v.api(), desk=desk, cfg=CFG, limits=tight, alerts=a)
    rows = journal(desk)
    # the bar that closed meanwhile fires again, and the latch that the loss just set refuses it
    assert [r["kind"] for r in rows][-4:] == ["exit", "latch", "refused", "cycle"] and rows[-2]["why"] == ["latch"]
    x = rows[-4]
    assert x["reason"] == "stop" and x["r"] < -1 and risk.latch_file(desk).exists()
    assert "crypto:latch" in a.firing() and any("loss limit" in m["title"] for m in box)


def test_the_time_stop_closes_a_position_that_reached_neither_level(crypto):
    desk, a, _ = crypto
    v = venue_with_a_signal()
    run(v, crypto)
    bars_later = T0 + 900 * P.time_stop_bars
    v.now = bars_later + 900 + 10
    v.ohlc[("XBTAUD", 15)] = ([row(bars_later - 900, 102.5, 102.6, 102.4, 102.5), row(bars_later, 102.5, 102.6, 102.4, 102.5)],
                              bars_later)
    v.ohlc[("XBTAUD", 1)] = ([row(T0 + 960 + 60 * i, 102.5, 102.6, 102.4, 102.5) for i in range(5)], T0 + 1200)
    run(v, crypto)
    x = next(r for r in journal(desk) if r["kind"] == "exit")
    assert x["reason"] == "time" and x["r"] < 0                              # flat price, two fees


def test_a_crash_between_the_book_and_the_journal_is_repaired_once(crypto, monkeypatch):
    desk, a, _ = crypto
    v = venue_with_a_signal()
    real = ledger.append
    monkeypatch.setattr(ledger, "append", lambda *x, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        run(v, crypto)
    assert not desk.journal.exists()
    saved = Book.load(desk.state_dir / "book.json", Decimal(0))
    assert "BTC/AUD" in saved.positions and [r["kind"] for r in saved.outbox] == ["entry", "cycle"]
    monkeypatch.setattr(ledger, "append", real)
    v.now += 20
    run(v, crypto)                                                          # same bar: only the repair happens
    assert [r["kind"] for r in journal(desk)] == ["entry", "cycle"]
    run(v, crypto)
    assert [r["kind"] for r in journal(desk)] == ["entry", "cycle"] and ledger.verify_chain(desk.journal) == []

    # a crash after the journal line but before the outbox was cleared must not write the line twice
    book = Book.load(desk.state_dir / "book.json", Decimal(0))
    book.outbox = [journal(desk)[0]]
    book.save(desk.state_dir / "book.json")
    assert cycle.flush(book, desk.state_dir / "book.json", desk.journal) == 0 and len(journal(desk)) == 2


def test_no_data_means_no_action_and_three_silent_cycles_page(crypto):
    desk, a, box = crypto
    v = venue_with_a_signal()
    v.down = True
    for i in range(3):
        v.now += 900
        assert run(v, crypto) == 0
        assert ("crypto:data-stale" in a.firing()) == (i == 2)
    assert not (desk.state_dir / "observations").exists()
    assert [r["kind"] for r in journal(desk)] == ["cycle"] * 3 and all(len(r["failed"]) == 3 for r in journal(desk))
    v.down, v.now = False, T0 + 900 + 10
    run(v, crypto)
    assert "crypto:data-stale" not in a.firing()


def test_a_wrong_clock_stops_the_cycle(crypto):
    desk, a, _ = crypto
    v = venue_with_a_signal()
    desk_now = v.now + 60                                                   # this host is a minute ahead of the venue
    cycle.run(now=desk_now, api=v.api(), desk=desk, cfg=CFG, limits=LIMITS, alerts=a)
    rec = journal(desk)[-1]
    assert rec["kind"] == "cycle" and not rec["pairs"] and all(x.startswith("clock:") for x in rec["failed"].values())


def test_a_wide_spread_or_an_untraded_bar_refuses_the_entry(crypto):
    desk, _, _ = crypto
    v = venue_with_a_signal()
    v.quote["XBTAUD"] = (102.0, 103.0)
    run(v, crypto)
    assert next(r for r in journal(desk) if r["kind"] == "refused")["why"] == ["spread_wide"]


def test_the_config_hash_follows_the_frozen_sections_only():
    h = cycle.config_hash(CFG)
    assert cycle.config_hash({**CFG, "account": {"start_equity": 1}}) == h
    assert cycle.config_hash({**CFG, "strategy": {**CFG["strategy"], "rsi_below": 30}}) != h
    assert cycle.config_hash({**CFG, "costs": {**CFG["costs"], "taker_fee_pct": 0.26}}) != h


# ---------------------------------------------------------------- paper by construction (ADR 0005)

PRIVATE_MARKERS = ("/0/private", "API-Sign", "API-Key", "KRAKEN_API", "krakenex", "ccxt", "AddOrder", "CancelOrder")


def test_nothing_in_the_code_can_reach_a_private_kraken_endpoint():
    """The crypto desk has no key, no signing and no order route. A marker of any of them fails here."""
    hits = [f"{p.relative_to(ROOT)}: {m}" for base in ("src", "scripts") for p in sorted((ROOT / base).rglob("*.py"))
            for m in PRIVATE_MARKERS if m in p.read_text()]
    assert hits == []
    assert not [k for k in ("requirements.lock.txt", "requirements-dev.lock.txt")
                if any(x in (ROOT / k).read_text().lower() for x in ("krakenex", "ccxt"))]


def test_the_crypto_package_imports_no_broker_and_only_the_public_base_url():
    import ast
    for p in sorted((ROOT / "src/wt/crypto").glob("*.py")):
        mods = {n.module or "" for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, ast.ImportFrom)} | \
               {a.name for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, ast.Import) for a in n.names}
        assert not [m for m in mods if m.startswith(("wt.brokers", "wt.oms", "wt.live", "alpaca"))], p.name
    from wt.crypto import data
    assert data.BASE == "https://api.kraken.com/0/public"
    assert [p.name for p in (ROOT / "src/wt/crypto").glob("*.py") if "requests" in p.read_text()] == ["data.py"]
