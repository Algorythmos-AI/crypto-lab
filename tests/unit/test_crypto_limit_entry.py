"""DEC-0020: a signal entered by a resting buy limit, followed to its outcome. The fills must not be flattered."""
from __future__ import annotations

import pytest

from wt.crypto import rules, signals
from wt.crypto.data import Bar, aggregate

from test_crypto_sleeves import CFG

H, H4 = 3600, 14_400
T = 1_791_072_000                                        # a 4-hour boundary: the signal bar opens here
C = rules.Common.of(CFG["sleeves"]["common"])
P = CFG["sleeves"]["break"]                              # stop 2 ATR below, target 4 ATR above, time stop
COSTS = {"taker_fee_pct": 0.40, "slippage_bps": 5}
MAKER = 0.22


def hb(k: int, o: float, h: float, l: float, c: float) -> Bar:                 # noqa: E741
    """The k-th hourly bar after the signal bar's close."""
    return Bar(T + H4 + k * H, o, h, l, c, (h + l + c) / 3, 10.0, 5)


def follow(fine: list[Bar], limit: float = 100.0, atr: float = 1.0, **kw):
    quiet = [Bar(T - 3 * H4 + i * H, 100, 100.2, 99.8, 100, 100, 10.0, 5) for i in range(16)]   # up to the signal's close
    bars = aggregate([*quiet, *fine], H, H4)
    return signals.limit_outcome("break", limit, atr, T, bars, fine, C, P, COSTS, H, MAKER, **kw)


def flat(k0: int, n: int, px: float = 100.5) -> list[Bar]:
    return [hb(k, px, px + 0.1, px - 0.1, px) for k in range(k0, k0 + n)]


def test_a_touch_does_not_fill_and_a_signal_nobody_sold_into_is_missed():
    touch = [hb(0, 100.5, 100.6, 100.0, 100.4), *flat(1, 3), *flat(4, 4)]
    assert follow(touch) == {"filled": False}
    assert follow(touch[:3]) is None                     # the four hours are not all there yet: no verdict
    late = [*flat(0, 4), hb(4, 100.5, 100.6, 99.0, 99.5), *flat(5, 3)]         # it came back after the order expired
    assert follow(late) == {"filled": False}


def test_a_fill_pays_the_maker_fee_and_a_target_is_a_resting_order_too():
    fine = [hb(0, 100.5, 100.6, 100.0, 100.4), hb(1, 100.4, 100.4, 99.9, 100.2), *flat(2, 2),
            hb(4, 100.5, 103.0, 100.4, 102.8), hb(5, 102.8, 104.5, 102.7, 104.2), *flat(6, 2, 104.2)]
    got = follow(fine)
    assert got["filled"] and got["price"] == 100.0 and got["fill_t"] == T + H4 + 2 * H
    assert got["stop"] == 98.0 and got["reason"] == "target" and got["exit_price"] == 104.0
    net = 104.0 - 100.0 - 0.0022 * 100.0 - 0.0022 * 104.0                       # maker in, maker out, no slippage
    assert got["r"] == pytest.approx(net / 2.0, abs=1e-4)
    # The same trade bought at the market pays the taker fee twice (and, on the desk, slippage on the way in).
    bars = aggregate([Bar(T - 3 * H4 + i * H, 100, 100.2, 99.8, 100, 100, 10.0, 5) for i in range(16)] + fine, H, H4)
    market = signals.outcome("break", 100.0, 98.0, 104.0, 1.0, T, bars, fine, C, P, COSTS, H)
    assert market["reason"] == "target" and market["r"] < got["r"]


def test_a_fill_bar_that_reaches_the_stop_is_a_loss_and_its_high_is_never_a_target():
    crash = [hb(0, 100.5, 100.6, 97.9, 98.2), *flat(1, 7, 98.2)]
    got = follow(crash)
    out = 98.0 * (1 - 0.0005)
    assert got["filled"] and got["reason"] == "stop" and got["exit_t"] == T + H4 + H and got["held_bars"] == 0
    assert got["r"] == pytest.approx((out - 100.0 - 0.0022 * 100.0 - 0.004 * out) / 2.0, abs=1e-4)
    # A bar that trades below the limit and above the target: the order of the two inside the bar is unknown, so
    # the target is not taken there. Here the price then falls to the stop.
    spike = [hb(0, 100.5, 105.0, 99.9, 100.1), *flat(1, 3, 100.1), hb(4, 100.1, 100.2, 97.5, 97.8), *flat(5, 3, 97.8)]
    got = follow(spike)
    assert got["filled"] and got["reason"] == "stop" and got["exit_t"] > got["fill_t"]


def test_a_stop_distance_the_sleeve_would_skip_is_skipped_and_an_unfinished_trade_has_no_result():
    fine = [hb(0, 100.4, 100.5, 99.9, 100.2), *flat(1, 7)]
    assert follow(fine, atr=0.3) == {"filled": False, "skipped": "stop_too_tight"}    # 0.6% away; the floor is 1%
    assert follow(fine) is None                          # filled, and neither level nor the time stop reached yet


def test_the_followers_default_arithmetic_is_untouched():
    fine = [hb(0, 100.4, 100.5, 99.9, 100.2), *flat(1, 3), hb(4, 100.5, 104.5, 100.4, 104.2), *flat(5, 3, 104.2)]
    bars = aggregate([Bar(T - 3 * H4 + i * H, 100, 100.2, 99.8, 100, 100, 10.0, 5) for i in range(16)] + fine, H, H4)
    a = signals.outcome("break", 100.05, 98.05, 104.05, 1.0, T, bars, fine, C, P, COSTS, H)
    net = 104.05 - 100.05 - 0.004 * (100.05 + 104.05)
    assert a["reason"] == "target" and a["r"] == round(net / 2.0, 4)
