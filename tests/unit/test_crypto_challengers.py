"""Challengers on the engine (DEC-0016, 5): a base rule with some dials turned runs through the same step as the
registered sleeves, on its own bar length, with its own stop distance and filters."""
from __future__ import annotations

import dataclasses

import pytest

from test_crypto_backtest import breakout_history
from test_crypto_sleeves import B0, CFG, DAY, H4, INFO

from wt.crypto import backtest, rules
from wt.crypto.data import Bar

HOUR = 3600
DIALS = {"base": "break", "timeframe_min": 240, "high_bars": 30, "stop_atr": 2.0, "target_atr": 4.0, "trail_atr": 2.0,
         "min_stop_pct": 1.0, "btc_filter": False, "volume_filter": False, "skip_held": False}
ONE = {**CFG, "sleeves": {**CFG["sleeves"], "common": {**CFG["sleeves"]["common"], "pairs": {"BTC/USD": "XBTUSD"}}}}
INFOS = {"XBTUSD": INFO["XBTUSD"]}
AFTER = [(104.0, 106.0, 103.5, 105.5, 2.0), (105.5, 118.0, 105.0, 117.0, 3.0), (117.0, 117.5, 116.0, 116.5, 1.0)]
END = B0 + H4 * (len(AFTER) + 1) + 1


def kinds(rows: list[dict], sleeve: str) -> list[str]:
    return [r["kind"] for r in rows if r.get("sleeve") == sleeve and r["kind"] in ("entry", "exit", "refused")]


def test_the_registered_sleeves_are_specs_of_themselves():
    specs = rules.registered(CFG)
    assert list(specs) == ["trend", "break", "dip"]
    assert all(s.base == n and s.c == rules.Common.of(CFG["sleeves"]["common"]) and not (s.btc_filter or s.skip_held)
               for n, s in specs.items())
    assert specs["break"].hypothesis == "HYP-0022" and specs["break"].p["target_atr"] == 4.0


def test_two_settings_that_are_the_same_strategy_are_one_challenger():
    a = rules.challenger_id(DIALS)
    assert a.startswith("ch-") and len(a) == 11
    assert rules.challenger_id({**DIALS, "trail_atr": 4.0}) == a                # it has a target: the trail is idle
    assert rules.challenger_id({**DIALS, "volume_filter": True}) == a           # the breakout rule already has it
    assert rules.challenger_id({**DIALS, "stop_atr": 3.0}) != a
    dip = {**DIALS, "base": "dip"}
    assert rules.challenger_id({**dip, "high_bars": 55}) == rules.challenger_id(dip)     # the dip rule has no lookback
    trail = {**DIALS, "target_atr": "none"}
    assert rules.challenger_id({**trail, "trail_atr": 3.0}) != rules.challenger_id(trail)
    assert set(CFG["learning"]["challengers"]["dials"]) == set(rules.DIALS)     # the charter's dials, all of them


def test_a_challenger_is_its_base_rule_with_the_dials_in_place():
    s = rules.challenger(CFG, {**DIALS, "stop_atr": 4.0, "min_stop_pct": 3.0, "high_bars": 55, "target_atr": 8.0})
    assert (s.base, s.c.stop_atr, s.c.min_stop_pct, s.p["high_bars"], s.p["target_atr"]) == ("break", 4.0, 3.0, 55, 8.0)
    assert s.p["volume_bars"] == CFG["sleeves"]["break"]["volume_bars"] and "trail_atr" not in s.p
    assert s.hypothesis == "challenger of HYP-0022"
    assert s.name == rules.challenger_id({**DIALS, "stop_atr": 4.0, "min_stop_pct": 3.0, "high_bars": 55, "target_atr": 8.0})
    t = rules.challenger(CFG, {**DIALS, "base": "trend", "target_atr": "none", "trail_atr": 4.0})
    assert t.p["trail_atr"] == 4.0 and "target_atr" not in t.p and t.p["exit_below_ema"] == 20
    fixed = rules.challenger(CFG, {**DIALS, "base": "trend", "target_atr": 6.0})
    assert fixed.p["target_atr"] == 6.0 and "exit_below_ema" not in fixed.p and "trail_atr" not in fixed.p
    assert rules.registered(CFG)["break"].c.stop_atr == 2.0                     # the registered sleeve is untouched


def test_a_challenger_with_a_wider_stop_trades_the_same_breakout_with_wider_levels():
    history = {"BTC/USD": breakout_history(AFTER)}
    wide = rules.challenger(CFG, {**DIALS, "stop_atr": 4.0, "target_atr": 8.0})
    rows = backtest.run(ONE, history, INFOS, B0, END, specs={"break": rules.registered(CFG)["break"], wide.name: wide})
    reg = next(r for r in rows if r["kind"] == "entry" and r["sleeve"] == "break")
    ch = next(r for r in rows if r["kind"] == "entry" and r["sleeve"] == wide.name)
    assert reg["price"] == ch["price"] and float(ch["stop"]) < float(reg["stop"]) and float(ch["target"]) > float(reg["target"])
    assert float(reg["price"]) - float(ch["stop"]) == pytest.approx(2 * (float(reg["price"]) - float(reg["stop"])), abs=0.2)
    assert float(ch["qty"]) < float(reg["qty"]) or float(ch["qty"]) * float(ch["price"]) <= 3000.5     # sized from its own stop
    assert ch["strategy"] == "challenger of HYP-0022" and ch["config"] == wide.name and ch["tf"] == 240


def test_the_filters_refuse_with_their_own_reason():
    history = {"BTC/USD": breakout_history(AFTER)}
    base = rules.registered(CFG)["break"]
    picky = rules.challenger(CFG, {**DIALS, "skip_held": True})
    rows = backtest.run(ONE, history, INFOS, B0, END, specs={"break": base, picky.name: picky})
    seen = [r for r in rows if r["kind"] == "sleeve" and r["sleeve"] == picky.name]
    # The registered sleeve is looked at first and buys; the challenger, seconds later, finds the pair held.
    assert kinds(rows, "break")[0] == "entry" and "entry" not in kinds(rows, picky.name)
    assert any(v["why"] == ["held_elsewhere"] for r in seen for v in r["pairs"].values())
    bars = backtest.Market.of(history["BTC/USD"], H4, DAY).closed(240, B0 + H4, 120)
    daily = backtest.Market.of(history["BTC/USD"], H4, DAY).closed(1440, B0 + H4, 60)
    assert rules.evaluate(base, bars, daily, {}, False)[0] is True
    assert rules.evaluate(picky, bars, daily, {}, True)[:2] == (False, ("held_elsewhere",))
    btc = dataclasses.replace(base, btc_filter=True)
    assert rules.evaluate(btc, bars, daily, {"btc_above_sma50": 0.0}, False)[:2] == (False, ("btc_not_in_uptrend",))
    assert rules.evaluate(btc, bars, daily, {}, False)[:2] == (False, ("btc_not_in_uptrend",))          # unknown is not a pass
    assert rules.evaluate(btc, bars, daily, {"btc_above_sma50": 1.0}, False)[0] is True
    vol = dataclasses.replace(rules.registered(CFG)["trend"], volume_filter=True)
    quiet = [*bars[:-1], dataclasses.replace(bars[-1], v=0.1)]
    assert "low_volume" in rules.evaluate(vol, quiet, daily, {}, False)[1]


def daily_breakout() -> list[Bar]:
    """Flat at 100 for 140 days, one day that closes at 112 on volume (it opens at B0), then a run to 140."""
    out: list[Bar] = []
    start = B0 - 140 * DAY
    for d in range(140):
        for h in range(24):
            out.append(Bar(start + d * DAY + h * HOUR, 100.0, 100.5, 99.5, 100.0, 100.0, 1.0, 3))
    for h in range(24):
        px = 100.0 + 12.0 * (h + 1) / 24
        out.append(Bar(B0 + h * HOUR, px - 0.5, px + 0.1, px - 0.6, px, px, 6.0, 3))
    t = B0 + DAY
    for d in range(4):
        for h in range(24):
            px = 112.0 + (d * 24 + h + 1) * 0.35
            out.append(Bar(t + (d * 24 + h) * HOUR, px - 0.3, px + 0.2, px - 0.4, px, px, 2.0, 3))
    return out


def test_a_daily_challenger_is_evaluated_once_a_day_on_daily_bars_and_managed_every_cycle():
    daily = rules.challenger(CFG, {**DIALS, "timeframe_min": 1440, "high_bars": 20, "stop_atr": 3.0, "target_atr": 6.0})
    rows = backtest.run(ONE, {"BTC/USD": daily_breakout()}, INFOS, B0, B0 + 5 * DAY + 1, specs={daily.name: daily})
    entries = [r for r in rows if r["kind"] == "entry"]
    assert len(entries) >= 1 and entries[0]["tf"] == 1440 and entries[0]["bar"] == B0
    assert entries[0]["t"].startswith("2026-10-05T00:00:10")                    # ten seconds after the daily close
    evals = [r for r in rows if r["kind"] == "sleeve" and r["pairs"]]
    assert all(r["pairs"]["BTC/USD"]["bar"] % DAY == 0 for r in evals) and len(evals) <= 6    # one look per daily bar
    exits = [r for r in rows if r["kind"] == "exit"]
    assert exits and exits[0]["reason"] == "target" and not exits[0]["t"].endswith("00:00:10+00:00")   # hit between daily closes
