"""The learning loop on the trading side (DEC-0016, 2 to 4): outcomes for every signal, the model scoring in
shadow, the promotion and demotion tests, what an acting model may do to an entry, and the daily job."""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from test_crypto_backtest import breakout_history
from test_crypto_challengers import AFTER
from test_crypto_sleeves import B0, CFG, DAY, H4, book_of, break_rows, crypto, journal, rows_of, run, venue  # noqa: F401

from wt.core import ledger
from wt.crypto import challengers, control, learn, outcomes, promotion, risk, rules, scorer
from wt.ops import units
from wt.ops.schedule import JOBS

NOW = float(B0 + H4 + 10)
LINEAGE = "m1:C=1"
EDGES = [20.0, 40.0, 60.0, 80.0]
SCORE_EDGES = [0.17, 0.29, 0.63, 0.79]          # the quintiles of the scores `signals_with_outcomes` gives


@pytest.fixture(autouse=True)
def own_locks(tmp_path, monkeypatch):
    from wt.ops import locks
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


def point(desk, version: str = "m1-a", lineage: str = LINEAGE, drift: dict | None = None) -> None:
    """A registered model as the ML side leaves it: the pointer and the card."""
    models = promotion.models_dir(desk)
    (models / version).mkdir(parents=True, exist_ok=True)
    (models / "current.json").write_text(json.dumps({"version": version, "kind": "logistic", "lineage": lineage,
                                                     "trained_at": iso(NOW - DAY), "inputs": ["rsi"], "sha256": "x",
                                                     "cutoff": 0.4, "half_below": 0.6}))
    (models / version / "card.json").write_text(json.dumps({"drift": drift or {"scores": SCORE_EDGES, "edges": {"rsi": EDGES}}}))


def signals_with_outcomes(n: int, good: bool, start: int = 0, lineage: str = LINEAGE, rsi: float | None = None) -> list[dict]:
    """`n` scored signals, one a day, with their outcomes. When `good`, the kept half wins and the skipped half
    loses; otherwise the result has nothing to do with the score."""
    rows: list[dict] = []
    for k in range(start, start + n):
        kept = k % 2 == 0
        score = (0.55 + 0.4 * ((k * 37) % 100) / 100) if kept else (0.05 + 0.3 * ((k * 53) % 100) / 100)
        if good:
            r = (1.5 if k % 3 else -1.0) if kept else (-1.0 if k % 5 else 1.5)
        else:
            r = 1.5 if (k * 7919) % 10 < 4 else -1.0
        sid = f"trend|BTC/USD|{k}"
        t = NOW + k * DAY
        rows.append({"kind": "signal", "sid": sid, "sleeve": "trend", "t": iso(t), "score": round(score, 4), "cutoff": 0.4,
                     "model": "m1-a", "lineage": lineage, "inputs": {"rsi": float((k * 17) % 100) if rsi is None else rsi}})
        rows.append({"kind": "outcome", "sid": sid, "sleeve": "trend", "exit_t": iso(t + 2 * DAY + k), "r": r})
    return rows


# ---------------------------------------------------------------- outcomes
def signal_row(price: float = 104.2, stop: float = 101.0, target: float | None = 112.0, sleeve: str = "break") -> dict:
    return {"kind": "signal", "sid": f"{sleeve}|BTC/USD|{B0}", "sleeve": sleeve, "pair": "BTC/USD", "bar": B0, "taken": False,
            "why": ["kill"], "price": price, "stop": stop, "target": target, "atr": 1.6, "t": iso(NOW)}


def test_a_signal_that_was_refused_is_still_followed_to_its_result_with_the_sleeves_own_exits(crypto):  # noqa: F811
    d, _, _ = crypto
    ledger.append(d.journal, signal_row())
    hourly = breakout_history(AFTER)                         # after the breakout bar the price runs to 118
    asked: list[tuple] = []

    def load(pair, start, end):
        asked.append((pair, start, end))
        return hourly
    later = float(B0 + H4 * (len(AFTER) + 1) + 60)
    found = outcomes.run(d, CFG, later, journal(d), load)
    assert len(found) == 1 and asked[0][0] == "BTC/USD" and asked[0][1] < B0 - 100 * H4
    o = found[0]
    assert (o["sid"], o["reason"], o["taken"], o["sleeve"]) == (f"break|BTC/USD|{B0}", "target", False, "break")
    # (112 - 104.2 - the fee on both sides) over the 3.2 the trade risked.
    assert o["r"] == pytest.approx((112 - 104.2 - 0.004 * (104.2 + 112)) / 3.2, abs=0.01) and o["fine"] == "hourly"
    assert [r["kind"] for r in journal(d)] == ["signal", "outcome"] and ledger.verify_chain(d.journal) == []
    assert outcomes.run(d, CFG, later, journal(d), load) == []                  # followed once
    assert outcomes.pending(journal(d)) == []


def test_a_signal_whose_trade_has_not_finished_has_no_outcome_yet(crypto):  # noqa: F811
    d, _, _ = crypto
    ledger.append(d.journal, signal_row(target=200.0, stop=50.0))               # neither level is reached
    assert outcomes.run(d, CFG, float(B0 + H4 * 4), journal(d), lambda *a: breakout_history(AFTER)) == []
    assert len(outcomes.pending(journal(d))) == 1
    ledger.append(d.journal, {**signal_row(), "sid": "x", "sleeve": "nobody"})  # a sleeve the desk does not know
    ledger.append(d.journal, {**signal_row(), "sid": "y", "bar": B0 + 77})      # a bar that is not in the history
    assert outcomes.run(d, CFG, float(B0 + H4 * 4), journal(d), lambda *a: breakout_history(AFTER)) == []
    assert outcomes.run(d, CFG, float(B0 + H4 * 4), journal(d), lambda *a: []) == []


def test_a_challengers_signal_is_followed_with_the_challengers_own_rules(crypto):  # noqa: F811
    d, _, _ = crypto
    dials = rules.canonical({"base": "break", "timeframe_min": 240, "high_bars": 30, "stop_atr": 3.0, "target_atr": 6.0,
                             "trail_atr": 2.0, "min_stop_pct": 1.0, "btc_filter": False, "volume_filter": False, "skip_held": False})
    cid = rules.challenger_id(dials)
    challengers.note(d, cid, "registered", NOW, dials=dials, rules="r", slot="random", of=None, week="w", n_trials=8)
    assert set(outcomes.specs_of(CFG, journal(d))) == {"trend", "break", "dip", cid}
    ledger.append(d.journal, signal_row(sleeve=cid, stop=99.4, target=113.8))
    found = outcomes.run(d, CFG, float(B0 + H4 * 5), journal(d), lambda *a: breakout_history(AFTER))
    assert [(o["sleeve"], o["reason"]) for o in found] == [(cid, "target")]


# ---------------------------------------------------------------- the tests a model must pass
def test_the_spread_its_lower_bound_and_the_brier_score():
    good = promotion.scored(signals_with_outcomes(60, True), LINEAGE)
    noise = promotion.scored(signals_with_outcomes(60, False), LINEAGE)
    assert len(good) == 60 and [o.exit_t for o in good] == sorted(o.exit_t for o in good)
    assert promotion.spread(good) > 1.0 and promotion.spread_lower(good, 0.05 / 6) > 0
    assert abs(promotion.spread(noise)) < 0.5 and promotion.spread_lower(noise, 0.05 / 6) < 0
    assert promotion.spread_lower(good, 0.05 / 6) == promotion.spread_lower(good, 0.05 / 6)     # seeded
    assert promotion.spread_lower(good, 0.05 / 6) < promotion.spread_lower(good, 0.25)          # a stricter level asks more
    model, base = promotion.brier(good)
    assert model < base
    assert promotion.brier(noise)[0] > promotion.brier(noise)[1]
    one_sided = [o for o in good if o.kept]
    assert promotion.spread(one_sided) is None and promotion.spread_lower(one_sided, 0.05) is None
    # Another lineage's signals, an unscored signal and one with no outcome are not this model's evidence.
    rows = signals_with_outcomes(4, True) + signals_with_outcomes(2, True, 10, "m2:x") + [
        {"kind": "signal", "sid": "a", "lineage": LINEAGE, "score": 0.9, "cutoff": 0.4},
        {"kind": "signal", "sid": "b", "lineage": LINEAGE}, {"kind": "outcome", "sid": "b", "r": 1.0, "exit_t": iso(NOW)}]
    assert len(promotion.scored(rows, LINEAGE)) == 4


def test_the_stability_index_is_near_zero_on_the_same_data_and_large_when_the_data_moved():
    same = [float((k * 17) % 100) for k in range(60)]
    assert promotion.psi(EDGES, same) < 0.1
    assert promotion.psi(EDGES, [95.0] * 60) > 1.0
    assert promotion.psi([1.0], same) is None and promotion.psi(EDGES, []) is None
    obs = promotion.scored(signals_with_outcomes(60, True), LINEAGE)
    calm = promotion.drift({"scores": SCORE_EDGES, "edges": {"rsi": EDGES}}, obs, 0.25, 3)
    assert calm["score_psi"] < 0.1
    assert calm["drifted"] is False and calm["inputs"] == {}
    moved = promotion.scored(signals_with_outcomes(60, True, rsi=99.0), LINEAGE)
    three = promotion.drift({"scores": [], "edges": {"rsi": EDGES, "a": EDGES, "b": EDGES}},
                            [promotion.Obs(o.sid, o.t, o.day, o.exit_t, o.score, o.cutoff, o.r, {"rsi": 99.0, "a": 99.0, "b": 99.0})
                             for o in moved], 0.25, 3)
    assert three["drifted"] is True and set(three["inputs"]) == {"rsi", "a", "b"}
    one = promotion.drift({"scores": [], "edges": {"rsi": EDGES}}, moved, 0.25, 3)
    assert one["drifted"] is False and "rsi" in one["inputs"]                    # one input alone is not drift
    assert promotion.drift({"scores": [0.9, 0.92, 0.94, 0.96], "edges": {}}, obs, 0.25, 3)["drifted"] is True   # the scores moved


def judged(d, rows, now=NOW) -> tuple[dict, list[str]]:
    state, events = promotion.evaluate(d, CFG, rows, now)
    promotion.save_state(d, state)
    return state, [e["event"] for e in events]


def test_a_model_is_promoted_only_at_a_checkpoint_and_only_on_signals_it_scored_first(crypto):  # noqa: F811
    d, _, _ = crypto
    assert judged(d, []) == ({}, [])                                            # no model: nothing to judge
    point(d)
    state, events = judged(d, signals_with_outcomes(59, True))
    assert events == ["lineage"] and state["state"] == "shadow" and state["checkpoints"] == 0
    assert (state["finished"], state["next_checkpoint"]) == (59, 60)
    assert promotion.in_force(d) == promotion.InForce("m1-a", LINEAGE, False)   # scores, acts on nothing
    state, events = judged(d, signals_with_outcomes(60, True))
    assert events == ["promoted"] and state["state"] == "acting" and state["checkpoints"] == 1
    look = state["looks"][0]
    assert look["passed"] and look["signals"] == 60 and look["alpha"] == pytest.approx(0.05 / 6) and look["lower"] > 0
    assert promotion.in_force(d).acting is True
    assert judged(d, signals_with_outcomes(61, True))[1] == []                  # no look between checkpoints


def test_six_looks_and_no_more_and_noise_is_never_promoted(crypto):  # noqa: F811
    d, _, _ = crypto
    point(d)
    seen: list[str] = []
    for n in (60, 120, 180, 240, 300, 360, 420, 480):
        state, events = judged(d, signals_with_outcomes(n, False))
        seen += events
    assert seen == ["lineage"] + ["checkpoint"] * 6 and state["state"] == "shadow" and len(state["looks"]) == 6
    assert state["checkpoints"] == 8                                            # counted, not looked at
    # Even signals that would pass are not looked at after the sixth checkpoint.
    assert judged(d, signals_with_outcomes(540, True))[1] == [] and promotion.in_force(d).acting is False
    # All due checkpoints are taken in order when the job missed days.
    d2 = d.state_dir / "other"
    import dataclasses
    other = dataclasses.replace(d, state_dir=d2)
    point(other)
    state, events = judged(other, signals_with_outcomes(130, False))
    assert events == ["lineage", "checkpoint", "checkpoint"] and [k["signals"] for k in state["looks"]] == [60, 120]


def test_an_acting_model_is_demoted_when_its_latest_signals_no_longer_separate(crypto):  # noqa: F811
    d, _, _ = crypto
    point(d)
    rows = signals_with_outcomes(60, True)
    assert judged(d, rows)[1] == ["lineage", "promoted"]
    rows += signals_with_outcomes(60, True, 60)
    assert judged(d, rows)[1] == ["held"]
    # The next 120 are worse for the kept half than for the skipped half.
    bad = signals_with_outcomes(120, True, 120)
    for r in bad:
        if r["kind"] == "outcome":
            r["r"] = -r["r"]
    state, events = judged(d, rows + bad)
    assert events == ["demoted"] and state["state"] == "demoted" and promotion.in_force(d).acting is False
    assert state["checkpoints"] == 4                        # demoted at the third; the fourth is counted, not looked at
    assert judged(d, rows + bad + signals_with_outcomes(120, True, 240))[1] == []      # not promoted again


def test_drift_suspends_an_acting_model_until_it_is_retrained_and_a_new_lineage_starts_over(crypto):  # noqa: F811
    d, _, _ = crypto
    point(d)
    assert judged(d, signals_with_outcomes(60, True, rsi=99.0))[1] == ["lineage", "promoted"]   # one input moved: not drift
    point(d, drift={"scores": [0.9, 0.92, 0.94, 0.96], "edges": {}})
    state, events = judged(d, signals_with_outcomes(60, True))
    assert events == ["suspended"] and state["state"] == "suspended" and state["drift"]["drifted"]
    assert promotion.in_force(d).acting is False and judged(d, signals_with_outcomes(60, True))[1] == []
    point(d, version="m1-b")                                                    # the weekly retraining, same lineage
    state, events = judged(d, signals_with_outcomes(60, True))
    assert events == ["resumed", "retrained"] and state["state"] == "acting" and state["checkpoints"] == 1
    assert "drift" not in state                             # the new version has scored nothing yet
    point(d, version="m2-a", lineage="m2:n_estimators=100,num_leaves=4")
    state, events = judged(d, signals_with_outcomes(60, True))
    assert events == ["lineage"] and state["state"] == "shadow" and state["checkpoints"] == 0 and state["finished"] == 0
    (promotion.models_dir(d) / "current.json").unlink()
    state, events = judged(d, [])
    assert events == ["none"] and state["state"] is None and promotion.in_force(d).scoring is False


def test_the_owners_switch_and_a_damaged_pointer_both_mean_no_model(crypto):  # noqa: F811
    d, _, _ = crypto
    point(d)
    judged(d, signals_with_outcomes(60, True))
    assert promotion.in_force(d).acting is True
    risk.learning_file(d).write_text("off\n")
    assert promotion.in_force(d) == promotion.InForce(None, None, False)
    risk.learning_file(d).unlink()
    (promotion.models_dir(d) / "current.json").write_text("{not json")
    assert promotion.in_force(d).scoring is False
    (promotion.models_dir(d) / "current.json").write_text(json.dumps({"version": "../x", "lineage": LINEAGE}))
    assert promotion.pointer(d) is None
    assert promotion.applies_to("trend") and not promotion.applies_to("ch-12345678") and not promotion.applies_to("pull")
    row = promotion.model_row({"sleeve": "dip", "inputs": {"rsi": 31.0, "spread_pct": None, "breadth": 2.0}})
    assert row == {"rsi": 31.0, "breadth": 2.0, "is_trend": 0.0, "is_break": 0.0, "is_dip": 1.0}


# ---------------------------------------------------------------- the model in the bar cycle
def fake_scorer(monkeypatch, score: float | None, calls: list | None = None, why: str = "timeout") -> None:
    def fake(rows, desk, **k):
        if calls is not None:
            calls.append(rows)
        if score is None:
            return None, why
        return scorer.Scored("m1-a", tuple(score for _ in rows), 0.4, 0.6), None
    monkeypatch.setattr(scorer, "score", fake)


def act(d) -> None:
    point(d)
    promotion.save_state(d, {"lineage": LINEAGE, "version": "m1-a", "state": "acting", "checkpoints": 1})


def test_in_shadow_every_registered_signal_is_scored_and_nothing_about_the_trade_changes(crypto, monkeypatch):  # noqa: F811
    d, _, _ = crypto
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)                                                              # no model at all
    plain = rows_of(d, "break", "entry")[0]
    assert "score" not in rows_of(d, "break", "signal")[0]

    d2, a2, _ = crypto
    import dataclasses
    other = dataclasses.replace(d, state_dir=d.state_dir / "b", kill_file=d.state_dir / "b/KILL",
                                ledgers=(("crypto", d.state_dir / "b/crypto_journal.jsonl"),), chain_flag=d.state_dir / "b/chain")
    other.state_dir.mkdir()
    point(other)
    calls: list = []
    fake_scorer(monkeypatch, 0.1, calls)                                        # a score that would skip it, if it acted
    run(venue({"XBTUSD": break_rows()}), (other, a2, []))
    entry, sig = rows_of(other, "break", "entry")[0], rows_of(other, "break", "signal")[0]
    assert (entry["qty"], entry["price"], entry["stop"]) == (plain["qty"], plain["price"], plain["stop"])
    assert (sig["score"], sig["model"], sig["lineage"], sig["cutoff"], sig["taken"]) == (0.1, "m1-a", LINEAGE, 0.4, True)
    assert "acted" not in sig
    # One call for the cycle, with the inputs as recorded and the sleeve as three 0/1 columns.
    assert len(calls) == 1 and all(r["is_break"] + r["is_trend"] + r["is_dip"] == 1.0 for r in calls[0])
    assert calls[0][0]["breadth"] == sig["inputs"]["breadth"] >= 1.0


@pytest.mark.parametrize(("score", "expect"), [(0.1, "skip"), (0.5, "half"), (0.9, "full")])
def test_an_acting_model_skips_or_halves_a_trade_and_never_enlarges_one(crypto, monkeypatch, score, expect):  # noqa: F811
    d, _, _ = crypto
    plain_dir = d.state_dir / "plain"
    import dataclasses
    plain = dataclasses.replace(d, state_dir=plain_dir, kill_file=plain_dir / "KILL",
                                ledgers=(("crypto", plain_dir / "crypto_journal.jsonl"),), chain_flag=plain_dir / "chain")
    plain_dir.mkdir()
    run(venue({"XBTUSD": break_rows()}), (plain, crypto[1], []))
    full = Decimal(rows_of(plain, "break", "entry")[0]["qty"])

    act(d)
    fake_scorer(monkeypatch, score)
    run(venue({"XBTUSD": break_rows()}), crypto)
    entries, sig = rows_of(d, "break", "entry"), rows_of(d, "break", "signal")[0]
    assert sig["score"] == score and sig["lineage"] == LINEAGE
    if expect == "skip":
        assert entries == [] and rows_of(d, "break", "refused")[0]["why"] == ["model_skip"]
        assert (sig["taken"], sig["why"], sig["acted"]) == (False, ["model_skip"], 0.0) and book_of(d, "break").positions == {}
    else:
        qty = Decimal(entries[0]["qty"])
        assert qty <= full and sig["taken"] is True                             # never more than the rule's own size
        assert (qty == full) if expect == "full" else (abs(qty * 2 - full) <= Decimal("0.0001"))
        assert sig["acted"] == (1.0 if expect == "full" else 0.5)
        assert entries[0]["stop"] == rows_of(plain, "break", "entry")[0]["stop"]   # the levels are the rule's own
    assert ledger.verify_chain(d.journal) == []


def test_a_scorer_that_does_not_answer_never_stops_a_trade_and_the_switch_takes_the_model_out(crypto, monkeypatch):  # noqa: F811
    d, _, box = crypto
    act(d)
    fake_scorer(monkeypatch, None)
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    assert len(rows_of(d, "break", "entry")) == 1 and "score" not in rows_of(d, "break", "signal")[0]
    assert any("did not score" in m["title"] for m in box)
    # With learning switched off the scorer is not even asked.
    assert control.main(["learning-off"], d) == 0
    calls: list = []
    fake_scorer(monkeypatch, 0.1, calls)
    v.now += H4
    eth = break_rows(B0 + H4)
    v.fill("ETHUSD", eth, float(eth[-1][4]))
    run(v, crypto)
    assert calls == [] and [r["pair"] for r in rows_of(d, "break", "entry")] == ["BTC/USD", "ETH/USD"]


def test_the_model_does_not_touch_a_challenger_or_the_baseline(crypto, monkeypatch):  # noqa: F811
    d, a, _ = crypto
    dials = rules.canonical({"base": "break", "timeframe_min": 240, "high_bars": 30, "stop_atr": 3.0, "target_atr": 6.0,
                             "trail_atr": 2.0, "min_stop_pct": 1.0, "btc_filter": False, "volume_filter": False, "skip_held": False})
    cid = rules.challenger_id(dials)
    challengers.note(d, cid, "registered", NOW, dials=dials, rules="r", slot="random", of=None, week="w", n_trials=8)
    challengers.note(d, cid, "c1", NOW, passed=True, failed_on=[], base={"trades": 40, "mean_r": 0.3})
    challengers.note(d, cid, "admitted", NOW)
    act(d)
    calls: list = []
    fake_scorer(monkeypatch, 0.1, calls)                                        # skip everything it is asked about
    run(venue({"XBTUSD": break_rows()}), crypto)
    assert rows_of(d, "break", "entry") == [] and len(rows_of(d, cid, "entry")) == 1
    assert "score" not in rows_of(d, cid, "signal")[0]
    assert sum(len(c) for c in calls) == len(rows_of(d, "break", "signal")) + len(rows_of(d, "trend", "signal"))


def skipper(d) -> str:
    """An admitted challenger of the breakout rule that skips a pair another sleeve holds."""
    dials = rules.canonical({"base": "break", "timeframe_min": 240, "high_bars": 30, "stop_atr": 3.0, "target_atr": 6.0,
                             "trail_atr": 2.0, "min_stop_pct": 1.0, "btc_filter": False, "volume_filter": False, "skip_held": True})
    cid = rules.challenger_id(dials)
    challengers.note(d, cid, "registered", NOW, dials=dials, rules="r", slot="random", of=None, week="w", n_trials=8)
    challengers.note(d, cid, "c1", NOW, passed=True, failed_on=[], base={"trades": 40, "mean_r": 0.3})
    challengers.note(d, cid, "admitted", NOW)
    return cid


@pytest.mark.parametrize("score", [0.9, None])
def test_an_entry_that_waits_for_the_model_is_held_to_every_other_sleeve_as_it_would_be_with_no_model(crypto, monkeypatch, score):  # noqa: F811
    d, _, _ = crypto
    cid = skipper(d)
    act(d)
    calls: list = []
    fake_scorer(monkeypatch, score, calls)
    run(venue({"XBTUSD": break_rows()}), crypto)
    # With no model the breakout sleeve buys first and the challenger finds the pair held. A waiting entry must
    # look the same to it: an acting model, or one that gives no answer, never gives a challenger a trade.
    assert len(rows_of(d, "break", "entry")) == 1 and rows_of(d, cid, "entry") == []
    seen = [v for r in rows_of(d, cid, "sleeve") for k, v in r["pairs"].items() if k == "BTC/USD"]
    assert seen and seen[0]["why"] == ["held_elsewhere"]
    assert len(calls) == 1                                  # asked once a cycle, also when it does not answer
    if score is not None:
        sig = rows_of(d, "break", "signal")[0]
        assert promotion.model_row(sig) == calls[0][[r["is_break"] for r in calls[0]].index(1.0)]   # scored on what is journalled


def test_a_fault_in_one_waiting_entry_does_not_lose_the_rest_of_the_cycle(crypto, monkeypatch):  # noqa: F811
    from wt.crypto import sleeves
    d, _, _ = crypto
    act(d)
    fake_scorer(monkeypatch, 0.9)
    real = sleeves.enter

    def flaky(book, sleeve, *a, **k):
        if sleeve == "break":
            raise KeyError("boom")
        return real(book, sleeve, *a, **k)
    monkeypatch.setattr(sleeves, "enter", flaky)
    v = venue({"XBTUSD": break_rows()})
    assert run(v, crypto) == 0
    assert rows_of(d, "break", "entry") == [] and len(rows_of(d, "trend", "entry")) == 1    # the other sleeve's entry stands
    assert rows_of(d, "trend", "signal") and book_of(d, "trend").positions                  # journalled and saved
    assert "BTC/USD" not in book_of(d, "break").meta["last_bar"]                            # looked at again next cycle
    monkeypatch.setattr(sleeves, "enter", real)
    v.now += 60
    run(v, crypto)
    assert len(rows_of(d, "break", "entry")) == 1 and ledger.verify_chain(d.journal) == []


def test_a_lineage_that_returns_carries_on_where_it_stopped_and_no_test_is_taken_twice(crypto):  # noqa: F811
    d, _, _ = crypto
    point(d)
    rows = signals_with_outcomes(60, True)
    assert judged(d, rows)[1] == ["lineage", "promoted"]
    point(d, version="m2-a", lineage="m2:x")
    state, events = judged(d, rows)
    assert events == ["lineage"] and state["state"] == "shadow" and state["lineages_started"] == 2
    point(d, version="m1-c")                                # a later retraining chooses the first lineage again
    state, events = judged(d, rows)
    assert events == ["returned"] and state["state"] == "acting" and state["checkpoints"] == 1 and len(state["looks"]) == 1
    assert state["lineages_started"] == 2 and "m2:x" in state["past"] and promotion.in_force(d).acting is True
    (promotion.models_dir(d) / "current.json").unlink()
    state, events = judged(d, rows)
    assert events == ["none"] and LINEAGE in state["past"]
    point(d, version="m1-d")
    state, events = judged(d, rows)
    assert events == ["returned"] and state["checkpoints"] == 1


def test_drift_is_measured_only_on_signals_the_version_in_force_scored(crypto):  # noqa: F811
    d, _, _ = crypto
    point(d, drift={"scores": [0.9, 0.92, 0.94, 0.96], "edges": {}})            # this card would call anything drift
    old = signals_with_outcomes(60, True)
    for r in old:
        if r["kind"] == "signal":
            r["model"] = "m1-earlier"
    state, events = judged(d, old)
    assert events == ["lineage", "promoted"] and "drift" not in state           # another version's scores: not measured
    state, events = judged(d, old + signals_with_outcomes(60, True, 60))        # sixty of its own
    assert "suspended" in events and state["drift"]["drifted"]


def test_one_pairs_unreadable_history_does_not_stop_the_other_pairs(crypto):  # noqa: F811
    d, _, _ = crypto
    ledger.append(d.journal, {**signal_row(), "sid": f"break|ETH/USD|{B0}", "pair": "ETH/USD"})
    ledger.append(d.journal, signal_row())
    ledger.append(d.journal, {**signal_row(), "sid": "ancient", "bar": B0 - 500 * DAY})     # too old ever to finish

    def load(pair, start, end):
        assert start > B0 - 60 * DAY                        # the ancient signal does not stretch the request
        if pair == "ETH/USD":
            raise OSError("bad cache")
        return breakout_history(AFTER)
    found = outcomes.run(d, CFG, float(B0 + H4 * 5), journal(d), load)
    assert [o["pair"] for o in found] == ["BTC/USD"]


# ---------------------------------------------------------------- the daily job
def test_the_daily_job_follows_signals_judges_the_model_and_retrains_once_a_week(crypto):  # noqa: F811
    d, a, box = crypto
    ledger.append(d.journal, signal_row())
    trained: list[float] = []

    def trainer(desk):
        trained.append(1)
        point(desk, version=f"m1-{len(trained)}")
        return {"t": iso(later), "chosen": "m1", "examples": 9000}, ""
    later = float(B0 + H4 * 5)
    assert learn.run(later, d, CFG, a, lambda *x: breakout_history(AFTER), trainer) == 0
    assert [r["kind"] for r in journal(d)] == ["signal", "outcome", "model"] and journal(d)[2]["event"] == "lineage"
    assert learn.last_train(d)["chosen"] == "m1" and promotion.load_state(d)["state"] == "shadow"
    assert learn.run(later + DAY, d, CFG, a, lambda *x: [], trainer) == 0 and len(trained) == 1     # not due yet
    assert learn.retrain_due(d, later + 6 * DAY) is False and learn.retrain_due(d, later + 7 * DAY) is True
    assert ledger.verify_chain(d.journal) == []


def test_a_retraining_with_no_candidate_leaves_no_model_in_force(crypto):  # noqa: F811
    d, a, _ = crypto
    point(d)
    promotion.save_state(d, {"lineage": LINEAGE, "version": "m1-a", "state": "acting", "checkpoints": 1})
    assert learn.run(NOW, d, CFG, a, lambda *x: [], lambda desk: ({"t": iso(NOW), "chosen": None, "examples": 9000}, "")) == 0
    assert promotion.pointer(d) is None and promotion.in_force(d).scoring is False
    assert list(promotion.models_dir(d).glob("retired-*.json"))                 # kept, not deleted
    assert promotion.load_state(d)["state"] is None and journal(d)[-1]["event"] == "none"


def test_a_failed_training_changes_nothing_and_the_switch_stops_tests_and_training(crypto):  # noqa: F811
    d, a, box = crypto
    point(d)
    assert learn.run(NOW, d, CFG, a, lambda *x: [], lambda desk: (None, "timeout")) == 1
    assert promotion.pointer(d) is not None and any("training failed" in m["title"] for m in box)
    assert learn.last_train(d) == {}
    assert learn.run(NOW, d, CFG, a, lambda *x: [], lambda desk: (None, "no_environment")) == 0   # not built yet: quiet
    assert learn.train(d, root=Path("/nonexistent"))[1] == "no_environment"
    risk.learning_file(d).write_text("off\n")
    ledger.append(d.journal, signal_row())
    called: list = []
    assert learn.run(float(B0 + H4 * 5), d, CFG, a, lambda *x: breakout_history(AFTER), lambda desk: called.append(1)) == 0
    assert called == [] and [r["kind"] for r in journal(d)][-2:] == ["signal", "outcome"]   # outcomes are still recorded
    d.chain_flag.write_text("x")
    assert learn.run(NOW, d, CFG, a, lambda *x: [], lambda desk: called.append(1)) == 2


def test_the_learning_job_runs_daily_after_the_challengers_and_a_deploy_waits_for_it():
    job = JOBS["crypto-learn"]
    assert not job.trading and job.desk == "crypto" and job.interval_s is None
    assert job.runtime_max_h * 60 > job.deadline_min > learn.TRAIN_TIMEOUT_S / 60
    assert "OnCalendar=Mon..Sun 04:30 America/New_York" in units.timer(job)
    assert "MemoryMax=2G" in units.service(job, Path("/r"))


# ---------------------------------------------------------------- the dashboard's section
def test_the_snapshot_says_what_was_trained_what_is_in_force_and_how_its_picks_did(crypto):  # noqa: F811
    from wt.crypto import snapshot
    d, a, _ = crypto
    empty = snapshot.learning_view(d, CFG, [])
    assert empty == {"switch": "on", "lineages_started": 0, "model": None, "training": None,
                     "signals": {"recorded": 0, "finished": 0, "open": 0, "win_rate": None, "mean_r": None, "scored": 0,
                                 "kept": 0, "kept_mean_r": None, "skipped": 0, "skipped_mean_r": None},
                     "limits": {"drift_psi": 0.25, "drift_inputs": 3, "max_model_age_days": 14, "demotion_window_signals": 120,
                                "alpha": 0.05, "cutoff_percentile": 40, "half_size_below_percentile": 60},
                     "scores": None, "series": [], "events": [], "lineages": [], "registry": [],
                     "planned": ["forecaster", "m3", "regime"]}
    assert snapshot.learning_view(d, {**CFG, "learning": {}}, []) is None
    point(d)
    rows = signals_with_outcomes(60, False) + [{"kind": "signal", "sid": "open", "sleeve": "trend", "lineage": LINEAGE, "score": 0.9}]
    judged(d, rows)
    (promotion.models_dir(d) / "last_train.json").write_text(json.dumps({
        "t": iso(NOW), "chosen": "m1", "decision": "DEC-0018", "examples": 9000, "pairs": 30, "effective_n": 2000.5,
        "win_rate": 0.34, "mean_r": -0.09, "attempt": 3, "m0": {"log_loss": 0.59, "kept": 7000, "kept_mean_r": -0.14},
        "best": {"m1": {"settings": {"C": 1}, "log_loss": 0.58, "kept": 4000, "kept_mean_r": -0.1, "dropped": 3000,
                        "dropped_mean_r": -0.2, "spread": 0.1, "spread_ci": [-0.1, 0.3]}},
        "importance": [{"input": "rsi", "weight": 0.4}, {"input": "volume_ratio", "gain_share": 0.2}]}))
    view = snapshot.learning_view(d, CFG, rows)
    m = view["model"]
    assert (m["version"], m["lineage"], m["state"], m["checkpoints"], m["finished"], m["next_checkpoint"]) == ("m1-a", LINEAGE, "shadow", 1, 60, 120)
    assert (m["max_checkpoints"], m["checkpoint_signals"]) == (6, 60) and len(m["looks"]) == 1 and m["looks"][0]["passed"] is False
    assert m["drifted"] is False and m["score_psi"] is not None and view["lineages_started"] == 1
    t = view["training"]
    assert [x["name"] for x in t["models"]] == ["m0", "m1"] and t["models"][1]["settings"] == "C=1"
    assert (t["models"][1]["ci_low"], t["models"][1]["ci_high"], t["models"][0]["settings"]) == (-0.1, 0.3, None)
    assert t["leans_on"] == [{"input": "rsi", "weight": 0.4}, {"input": "volume_ratio", "weight": 0.2}]
    g = view["signals"]
    assert (g["recorded"], g["finished"], g["open"], g["scored"]) == (61, 60, 1, 60) and g["kept"] + g["skipped"] == 60
    # What the host already keeps and the page now draws: the model's card, its scores, and its history.
    assert m["since"] and m["drift_psi"] == {} and m["leans_on"] == [] and m["by_sleeve"] == {}
    assert snapshot.learning_view(d, CFG, rows, dt.datetime.fromisoformat(m["trained_at"]) + dt.timedelta(days=3))["model"]["age_days"] == 3.0
    bins = view["scores"]["bins"]
    assert sum(b["kept"] + b["halved"] + b["skipped"] for b in bins) == 61          # the open signal is scored too
    assert all(0 <= b["lo"] < b["hi"] <= 1 for b in bins) and [b["lo"] for b in bins] == sorted(b["lo"] for b in bins)
    series = view["series"]
    assert len(series) == 60 and series[-1]["n"] == 60 and [x["t"] for x in series] == sorted(x["t"] for x in series)
    assert round(series[-1]["kept"] + series[-1]["skipped"], 3) == round(sum(r["r"] for r in rows if r.get("kind") == "outcome"), 3)
    assert view["lineages"] == [{"lineage": LINEAGE, "state": "shadow", "checkpoints": 1, "finished": 60,
                                 "since": m["since"], "in_force": True}]
    assert [(x["version"], x["in_force"]) for x in view["registry"]] == [("m1-a", True)]
    assert view["planned"] == ["forecaster", "m3", "regime"]
    assert (t["start"], t["end"], t["data_hash"], t["models"][1]["log_loss_se"]) == (None, None, None, None)
    journalled = rows + [{"kind": "model", "event": "checkpoint", "t": iso(NOW), "model": "m1-a", "lineage": LINEAGE}]
    assert snapshot.learning_view(d, CFG, journalled)["events"] == [{"t": iso(NOW), "event": "checkpoint", "lineage": LINEAGE, "version": "m1-a"}]
    card_file = promotion.models_dir(d) / "m1-a" / "card.json"
    card_file.write_text(json.dumps({**json.loads(card_file.read_text()), "kind": "logistic", "lineage": LINEAGE,
                                     "trained_at": iso(NOW), "examples": 9000, "by_sleeve": {"trend": {"n": 5, "mean_r": 0.1}},
                                     "importance": [{"input": f"x{k}", "weight": 0.1} for k in range(12)]}))
    carded = snapshot.learning_view(d, CFG, rows)
    assert len(carded["model"]["leans_on"]) == 12 and carded["model"]["by_sleeve"] == {"trend": {"n": 5, "mean_r": 0.1}}
    assert carded["registry"] == [{"version": "m1-a", "kind": "logistic", "lineage": LINEAGE, "trained_at": iso(NOW),
                                   "examples": 9000, "in_force": True}]
    view = carded
    risk.learning_file(d).write_text("off\n")
    assert snapshot.learning_view(d, CFG, rows)["switch"] == "off"
    # The whole section passes the published contract, and nothing was written by reading it.
    before = sorted(p.name for p in promotion.models_dir(d).iterdir())
    clean = snapshot.publish.Sanitizer(lambda x: x, None, strict=False).apply({"learning": snapshot.ALLOW["learning"]}, {"learning": view})
    assert clean["learning"]["model"]["lineage"] == LINEAGE and clean["learning"]["training"]["models"][1]["name"] == "m1"
    assert sorted(p.name for p in promotion.models_dir(d).iterdir()) == before


def test_the_snapshot_shows_the_desk_wide_limits_and_how_much_of_them_is_in_use(crypto, monkeypatch):  # noqa: F811
    from wt.crypto import snapshot
    d, _, _ = crypto
    now = dt.datetime.fromtimestamp(NOW + 900, dt.UTC)
    monkeypatch.setattr(risk, "load_desk_limits", lambda key="CD": None)
    assert snapshot.desk_view(d, CFG, [], now) is None                           # a checkout from before the limits
    monkeypatch.setattr(risk, "load_desk_limits", lambda key="CD": risk.DeskLimits(True, Decimal("3.0")))
    empty = snapshot.desk_view(d, CFG, [], now)
    assert (empty["books"], empty["positions"], empty["coins"], empty["open_risk"], empty["equity"]) == (3, 0, [], 0.0, 30_000.0)
    monkeypatch.setattr(risk, "load_desk_limits", lambda key="CD": None)         # as the fixture trades: both sleeves buy
    run(venue({"XBTUSD": break_rows()}), crypto)
    monkeypatch.setattr(risk, "load_desk_limits", lambda key="CD": risk.DeskLimits(True, Decimal("3.0")))
    rows = journal(d) + [{"kind": "refused", "sleeve": "dip", "t": iso(NOW), "why": ["desk_coin"]},
                         {"kind": "refused", "sleeve": "dip", "t": iso(NOW - 30 * DAY), "why": ["desk_risk", "exposure"]},
                         {"kind": "refused", "t": iso(NOW), "why": ["desk_coin"]}]      # the baseline is outside them
    view = snapshot.desk_view(d, CFG, rows, now)
    held = sum(len(book_of(d, n).positions) for n in rules.NAMES)
    assert view["positions"] == held >= 1 and view["coins"] == ["BTC/USD"]
    at_risk = sum(float(p.risk) for n in rules.NAMES for p in book_of(d, n).positions.values())
    assert view["open_risk"] == pytest.approx(at_risk) and 0 < view["open_risk_pct"] < 3.0
    assert view["open_risk_pct"] == pytest.approx(at_risk / view["equity"] * 100)
    assert (view["refused_coin"], view["refused_coin_7d"], view["refused_risk"], view["refused_risk_7d"]) == (1, 1, 1, 0)
    assert (view["one_position_per_coin"], view["max_open_risk_pct"]) == (True, 3.0)
    clean = snapshot.publish.Sanitizer(lambda x: x, None, strict=False).apply({"desk": snapshot.ALLOW["desk"]}, {"desk": view})
    assert clean["desk"]["coins"] == ["BTC/USD"]


def test_the_models_the_page_calls_planned_are_the_ones_the_trainer_does_not_fit():
    """The trading side may not import the trainer, so the list is kept twice and compared here as text."""
    from wt.crypto import snapshot
    source = (Path(snapshot.__file__).parents[1] / "ml" / "train.py").read_text()
    assert f"ORDER = {json.dumps(list(snapshot.BUILT_MODELS)).replace('[', '(').replace(']', ')')}" in source
