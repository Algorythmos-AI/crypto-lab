"""DEC-0011 M-STAT: day-block bootstrap (synthetic trades only)."""
import numpy as np
import pytest

from wt.backtest.stats import bootstrap_ci, summarize


def day_correlated(n_days=80, per_day=5, seed=0):
    """Trades on one day share that day's move almost entirely (sd 1.0 across days, 0.1 within)."""
    rng = np.random.default_rng(seed)
    day_move = rng.normal(0.1, 1.0, n_days)
    r = np.repeat(day_move, per_day) + rng.normal(0, 0.1, n_days * per_day)
    days = np.repeat([f"2024-{1 + k // 28:02d}-{1 + k % 28:02d}" for k in range(n_days)], per_day)
    return r, days


def width(ci):
    return ci[1] - ci[0]


def test_default_is_the_recorded_trade_bootstrap_bit_for_bit():
    r = np.random.default_rng(4).normal(0.2, 1.0, 150)
    rng = np.random.default_rng(7)                                    # the pre-DEC-0011 code, verbatim
    boots = rng.choice(r, size=(5000, len(r)), replace=True).mean(axis=1)
    old = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]
    assert bootstrap_ci(r) == old
    s = summarize(r)
    assert s["ci95_expectancy"] == old and "ci95_block" not in s


def test_day_blocks_widen_the_ci_for_day_correlated_trades():
    r, days = day_correlated()
    trade, day = bootstrap_ci(r), bootstrap_ci(r, block="day", days=days)
    assert width(day) > 1.8 * width(trade)                            # ~sqrt(5) for 5 near-identical trades a day
    assert day[0] < float(r.mean()) < day[1]
    s = summarize(r, block="day", days=days)
    assert s["ci95_expectancy"] == day and s["ci95_block"] == "day"


def test_day_blocks_match_trade_bootstrap_when_trades_are_independent():
    r = np.random.default_rng(2).normal(0.0, 1.0, 400)
    days = [f"d{k}" for k in range(400)]                              # one trade per day: same estimator
    assert width(bootstrap_ci(r, block="day", days=days)) == pytest.approx(width(bootstrap_ci(r)), rel=0.15)


def test_day_bootstrap_reproduces_with_a_fixed_seed():
    r, days = day_correlated(seed=3)
    a = bootstrap_ci(r, seed=11, block="day", days=days)
    assert a == bootstrap_ci(r, seed=11, block="day", days=list(days))
    assert a != bootstrap_ci(r, seed=12, block="day", days=days)


def test_day_block_needs_labels_and_known_block():
    r, days = day_correlated()
    with pytest.raises(ValueError, match="day label"):
        bootstrap_ci(r, block="day")
    with pytest.raises(ValueError, match="day label"):
        bootstrap_ci(r, block="day", days=days[:-1])
    with pytest.raises(ValueError, match="block"):
        bootstrap_ci(r, block="week", days=days)
