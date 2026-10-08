"""The challengers' daily run (DEC-0016, 5 and 6): the draw, registration before any result, gate C1, admission
onto a paper book, retirement, and the owner's switch."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from test_crypto_backtest import breakout_history
from test_crypto_challengers import AFTER, INFOS, ONE
from test_crypto_sleeves import B0, CFG, H4, book_of, break_rows, crypto, journal, row, rows_of, run, venue  # noqa: F401

from wt.core import ledger
from wt.crypto import backtest, challengers, control, risk, rules, snapshot
from wt.ops import deploy, units
from wt.ops.schedule import JOBS

NOW = float(B0 + H4 + 10)
WEEK = challengers.week_of(NOW)
WIDE = rules.canonical({"base": "break", "timeframe_min": 240, "high_bars": 30, "stop_atr": 3.0, "target_atr": 6.0,
                        "trail_atr": 2.0, "min_stop_pct": 1.0, "btc_filter": False, "volume_filter": False,
                        "skip_held": False})
WIDE_ID = rules.challenger_id(WIDE)
PASS = {"passed": True, "failed_on": [], "base": {"trades": 40, "mean_r": 0.3}, "stressed": {"ci_low": 0.05},
        "control_p": 0.01, "control_mean_r": -0.1, "span": [0, 1], "n_trials": 8, "controls": 3, "seed": 7}
FAIL = {**PASS, "passed": False, "failed_on": ["too_few_trades"], "base": {"trades": 2, "mean_r": -0.4}}
MARKET = ({"BTC/USD": breakout_history(AFTER)}, INFOS)


@pytest.fixture(autouse=True)
def own_locks(tmp_path, monkeypatch):
    """Every lock these tests take is in the test's own folder, never the checkout's."""
    from wt.ops import locks
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")


CONFIRMED = {"passed": True, "trades": 40, "mean_r": 0.2, "profit_factor": 1.4, "span": [0, 1], "pairs": ["BTC/USD"],
             "data_hash": "e"}


@pytest.fixture(autouse=True)
def earlier_history(monkeypatch):
    """DEC-0021: a challenger that passes gate C1 is confirmed on earlier history. These tests are about the rest
    of the run, so the confirmation is a stub that passes and counts its calls; the tests of the confirmation
    itself replace it. Nothing here may reach the exchange."""
    calls: list[str] = []

    def stub(cfg, dials, start, end, earlier=None):
        calls.append(rules.challenger_id(dials))
        return dict(CONFIRMED)
    monkeypatch.setattr(challengers, "confirmed", stub)
    monkeypatch.setattr(challengers, "load_history", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    return calls


def gate_returning(monkeypatch, result: dict, seen: list | None = None, desk=None) -> None:
    def fake(cfg, dials, n_trials, hourly, infos, start, end, controls=3, seed=7):
        if seen is not None:
            seen.append((rules.challenger_id(dials), n_trials, [r.get("event") for r in journal(desk) if r["kind"] == "challenger"]))
        return {**result, "n_trials": n_trials}
    monkeypatch.setattr(challengers, "gate", fake)


def admit(crypto, monkeypatch, dials: dict = WIDE, now: float = NOW) -> str:  # noqa: F811
    """Register one challenger by hand and let the run pass and admit it."""
    desk, alerts, _ = crypto
    cid = rules.challenger_id(dials)
    challengers.note(desk, cid, "registered", now, dials=dials, rules=challengers.describe(dials), slot="random",
                     of=None, week="2020-W01", n_trials=8)
    gate_returning(monkeypatch, PASS)
    monkeypatch.setattr(challengers, "draw", lambda *a, **k: [])
    assert challengers.run(now, desk, CFG, MARKET, alerts) == 0
    return cid


# ---------------------------------------------------------------- the space and the draw
def test_the_space_is_the_charters_dials_and_every_member_is_one_runnable_strategy():
    members = challengers.space(CFG)
    grid = CFG["learning"]["challengers"]["dials"]
    assert 500 < len(members) < 3 * 2 * 3 * 3 * 4 * 3 * 3 * 2 * 2 * 2          # the same strategy is counted once
    for cid, d in list(members.items())[::37]:
        spec = rules.challenger(CFG, d)
        assert spec.name == cid == rules.challenger_id(d) and d == rules.canonical(d)
        assert d["base"] in grid["base"] and d["timeframe_min"] in grid["timeframe_min"] and d["stop_atr"] in grid["stop_atr"]
        assert (d["target_atr"] is None) != (d["trail_atr"] is None)
    assert WIDE_ID in members


def test_a_dial_that_does_nothing_is_not_a_difference_and_no_target_is_a_setting():
    trail = {**WIDE, "target_atr": None, "trail_atr": 3.0}
    assert challengers.distance(WIDE, WIDE) == 0
    assert challengers.distance(WIDE, {**WIDE, "stop_atr": 4.0}) == 1
    assert challengers.distance(WIDE, trail) == 1                               # target against trailed: one dial
    assert challengers.distance(trail, {**trail, "trail_atr": 4.0}) == 1
    dip = rules.canonical({**WIDE, "base": "dip"})
    assert challengers.distance(WIDE, dip) == 1                                 # the base alone: break always has volume
    assert challengers.distance(WIDE, {**dip, "volume_filter": False}) == 2
    assert challengers.distance(dip, {**dip, "high_bars": None}) == 0           # the dip rule has no lookback


def test_the_best_current_sleeve_is_judged_on_backtest_and_live_trades_together():
    exp = challengers.experiment(CFG)
    assert (exp["start"], exp["end"]) == (1728273600, 1791345600)               # EXP-0016's span, not a moving window
    name, dials, mean = challengers.best(CFG, {}, [], exp)
    assert name == "break" and dials == challengers.sleeve_dials(CFG, "break") and mean == pytest.approx(-0.1474)
    # 400 live losers of -1R on the breakout sleeve make the trend sleeve the best.
    live = [{"kind": "exit", "sleeve": "break", "id": str(i), "r": -1.0, "risk0": 1, "pnl": "-1"} for i in range(400)]
    assert challengers.best(CFG, {}, live, exp)[0] == "trend"
    # A live challenger with a better record leads; a failed one is not a current sleeve.
    state = {WIDE_ID: {"id": WIDE_ID, "dials": WIDE, "status": "live", "c1": {"base": {"trades": 50, "mean_r": 0.2}}},
             "ch-dead": {"id": "ch-dead", "dials": WIDE, "status": "failed", "c1": {"base": {"trades": 50, "mean_r": 9.0}}}}
    assert challengers.best(CFG, state, [], exp)[:2] == (WIDE_ID, WIDE)


def test_the_weekly_draw_is_a_neighbour_of_the_best_and_one_at_random_and_is_the_same_every_time():
    best = challengers.sleeve_dials(CFG, "break")
    picks = challengers.draw(CFG, {}, "break", best, WEEK)
    assert [p["slot"] for p in picks] == ["neighbour", "random"] and picks[0]["of"] == "break"
    assert picks == challengers.draw(CFG, {}, "break", best, WEEK)              # seeded by the week alone
    assert picks != challengers.draw(CFG, {}, "break", best, "2031-W07")
    members = challengers.space(CFG)
    assert all(p["id"] in members and members[p["id"]] == p["dials"] for p in picks) and picks[0]["id"] != picks[1]["id"]
    near = {cid: challengers.distance(d, best) for cid, d in members.items()}
    assert near[picks[0]["id"]] == min(n for n in near.values() if n > 0)       # as near as the space allows
    # Beside a sleeve that is itself in the space, the neighbour differs in exactly one dial.
    one = challengers.draw(CFG, {}, WIDE_ID, WIDE, WEEK)[0]
    assert challengers.distance(one["dials"], WIDE) == 1
    # The registered sleeves are trials already and are never drawn again.
    assert not {p["id"] for p in picks} & {rules.challenger_id(challengers.sleeve_dials(CFG, n)) for n in rules.NAMES}


def test_the_caps_stop_the_generator():
    best = challengers.sleeve_dials(CFG, "break")
    members = list(challengers.space(CFG).items())

    def state(n: int, status: str, week: str = "2020-W01") -> dict:
        return {cid: {"id": cid, "dials": d, "status": status, "week": week, "slot": "random"} for cid, d in members[:n]}
    assert len(challengers.draw(CFG, state(1, "failed", WEEK), "break", best, WEEK)) == 1      # two a week
    assert challengers.draw(CFG, state(2, "failed", WEEK), "break", best, WEEK) == []
    assert len(challengers.draw(CFG, state(5, "live"), "break", best, WEEK)) == 1              # six live
    assert challengers.draw(CFG, state(6, "live"), "break", best, WEEK) == []
    assert len(challengers.draw(CFG, state(59, "failed"), "break", best, WEEK)) == 1           # sixty in all
    assert challengers.draw(CFG, state(60, "failed"), "break", best, WEEK) == []
    # This week's neighbour is registered already: only the random one is left, and it is not drawn twice.
    first = challengers.draw(CFG, {}, "break", best, WEEK)
    done = {first[0]["id"]: {"id": first[0]["id"], "dials": first[0]["dials"], "status": "failed", "week": WEEK, "slot": "neighbour"}}
    assert [p["slot"] for p in challengers.draw(CFG, done, "break", best, WEEK)] == ["random"]


# ---------------------------------------------------------------- the run
def test_a_challenger_is_in_the_journal_before_its_backtest_and_one_that_fails_never_trades(crypto, monkeypatch):  # noqa: F811
    d, a, _ = crypto
    seen: list = []
    gate_returning(monkeypatch, FAIL, seen, d)
    assert challengers.run(NOW, d, CFG, MARKET, a) == 0
    assert len(seen) == 2
    # When the first backtest starts, both of this week's challengers are registered and nothing is judged.
    assert seen[0][2] == ["registered", "registered"] and seen[1][2] == ["registered", "registered", "c1"]
    assert [n for _, n, _ in seen] == [13, 14]              # family C's twelve (DEC-0025), plus those registered so far
    rows = [r for r in journal(d) if r["kind"] == "challenger"]
    assert [r["event"] for r in rows] == ["registered", "registered", "c1", "c1"]
    assert all(r["sleeve"].startswith("ch-") and r["week"] == WEEK and r["rules"] for r in rows[:2])
    assert rules.challenger_id(rows[0]["dials"]) == rows[0]["sleeve"]           # its exact rules, in the record
    assert ledger.verify_chain(d.journal) == []
    state = challengers.load_state(d)
    assert [r["status"] for r in state.values()] == ["failed", "failed"]
    assert challengers.active(d, CFG) == ({}, {})
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)                                                              # a breakout: only the registered sleeve buys
    assert len(rows_of(d, "break", "entry")) == 1
    assert not [r for r in journal(d) if r.get("sleeve", "").startswith("ch-") and r["kind"] in ("entry", "refused", "sleeve")]
    # The same week again draws nothing; the record is unchanged.
    assert challengers.run(NOW + 3600, d, CFG, MARKET, a) == 0
    assert len([r for r in journal(d) if r["kind"] == "challenger"]) == 4


def test_a_run_that_died_before_its_backtest_is_finished_by_the_next(crypto, monkeypatch):  # noqa: F811
    d, a, _ = crypto
    challengers.note(d, WIDE_ID, "registered", NOW, dials=WIDE, rules="r", slot="random", of=None, week=WEEK, n_trials=8)
    seen: list = []
    gate_returning(monkeypatch, FAIL, seen, d)
    assert challengers.run(NOW + 3600, d, CFG, MARKET, a) == 0
    assert seen[0][:2] == (WIDE_ID, 8)                                          # with the count fixed when it was registered
    assert challengers.load_state(d)[WIDE_ID]["status"] == "failed"
    assert len(seen) == 2                                                       # and one more drawn: two a week


def test_the_real_gate_fails_a_challenger_on_a_thin_history_and_its_control_does_trade(crypto):  # noqa: F811
    hourly, infos = MARKET
    res = challengers.gate(ONE, WIDE, 9, hourly, infos, B0, B0 + H4 * 5, controls=6)
    assert res["passed"] is False and "too_few_trades" in res["failed_on"] and res["n_trials"] == 9
    spec = rules.challenger(ONE, {**WIDE, "btc_filter": True})
    plain = rules.challenger(ONE, WIDE)
    base = backtest.run(ONE, hourly, infos, B0, B0 + H4 * 5, specs={plain.name: plain})
    assert backtest.fire_rate(base, plain.name) > 0
    # The control's random entries are asked of the base rule and are not filtered: with the filter kept, or the
    # rate filed under the challenger's id, it would never trade and every challenger would fail on it.
    rows = [{**r, "sleeve": spec.name} if r.get("sleeve") == plain.name else r for r in base]
    _, means = backtest.control(ONE, hourly, infos, B0, B0 + H4 * 5, rows, spec.name, 12, 7, specs={spec.name: spec})
    assert means


def test_one_that_passes_trades_its_own_book_beside_the_registered_sleeves(crypto, monkeypatch):  # noqa: F811
    d, _, box = crypto
    cid = admit(crypto, monkeypatch)
    assert [r["event"] for r in journal(d) if r["kind"] == "challenger"] == ["registered", "c1", "admitted"]
    assert any("joins the tournament" in m["title"] for m in box)
    specs, off = challengers.active(d, CFG)
    assert list(specs) == [cid] and off == {} and specs[cid].c.stop_atr == 3.0
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    mine, reg = rows_of(d, cid, "entry"), rows_of(d, "break", "entry")
    assert len(mine) == len(reg) == 1 and mine[0]["pair"] == "BTC/USD" and mine[0]["config"] == cid
    assert Decimal(mine[0]["stop"]) < Decimal(reg[0]["stop"]) and Decimal(mine[0]["target"]) > Decimal(reg[0]["target"])
    assert list(book_of(d, cid).positions) == ["BTC/USD"] and book_of(d, cid).start_equity == Decimal(10_000)
    assert book_of(d, "break").cash != book_of(d, cid).cash                     # its own book
    assert ledger.verify_chain(d.journal) == []
    # The snapshot shows it beside the three, as a strategy that passed its backtest.
    import datetime as dt
    view = snapshot.sleeves_view(d, CFG, journal(d), dt.datetime.fromtimestamp(v.now, dt.UTC))
    assert [s["name"] for s in view] == ["trend", "break", "dip", cid]
    assert view[3]["stage"] == "passed" and view[3]["strategy"].startswith("Challenger: break rule on 4-hour bars")
    assert len(view[3]["positions"]) == 1 and view[1]["stage"] == "failed"         # shown with its own verdict (DEC-0016, 1)
    # The baseline's rows are the ones with no sleeve: none of the challenger's is among them.
    assert not [r for r in journal(d) if not r.get("sleeve") and r["kind"] in ("challenger", "entry")]


def test_a_lost_cache_is_rebuilt_from_the_journal_and_a_broken_record_never_stops_the_sleeves(crypto, monkeypatch):  # noqa: F811
    d, _, box = crypto
    cid = admit(crypto, monkeypatch)
    challengers.state_path(d).unlink()
    assert list(challengers.active(d, CFG)[0]) == [cid] and challengers.state_path(d).exists()
    challengers.state_path(d).write_text("{not json")
    assert list(challengers.active(d, CFG)[0]) == [cid]
    # A record whose rules do not give its id is refused whole; the registered sleeves still trade.
    bad = json.loads(json.dumps({"challengers": {cid: {"id": cid, "status": "live", "dials": {**WIDE, "stop_atr": 4.0}}}}))
    challengers.state_path(d).write_text(json.dumps(bad))
    v = venue({"XBTUSD": break_rows()})
    assert run(v, crypto) == 0
    assert len(rows_of(d, "break", "entry")) == 1 and rows_of(d, cid, "entry") == []
    assert any("challengers did not run" in m["title"] for m in box)


# ---------------------------------------------------------------- the owner's switch
def test_the_owners_switch_stops_challengers_opening_trades_and_stops_no_exit(crypto, monkeypatch):  # noqa: F811
    d, a, _ = crypto
    cid = admit(crypto, monkeypatch)
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    target = Decimal(rows_of(d, cid, "entry")[0]["target"])
    assert control.main(["learning-off"], d) == 0 and risk.learning_file(d).exists()
    assert control.main(["learning-off"], d) == 0                               # already off: nothing more is written
    assert challengers.active(d, CFG)[1] == {cid: "learning_off"}
    # A second breakout, on another pair: the registered sleeve buys it, the challenger is refused by name.
    v.now += H4
    eth = break_rows(B0 + H4)
    v.fill("ETHUSD", eth, float(eth[-1][4]))
    minute = B0 + H4 + 60
    v.ohlc[("XBTUSD", 1)] = ([row(minute, 104.2, float(target) + 1, 104.1, float(target) + 0.5)], minute)
    v.quote["XBTUSD"] = (float(target) + 0.4, float(target) + 0.5)
    run(v, crypto)
    assert [r["pair"] for r in rows_of(d, "break", "entry")] == ["BTC/USD", "ETH/USD"]
    assert [r["pair"] for r in rows_of(d, cid, "entry")] == ["BTC/USD"]
    refused = rows_of(d, cid, "refused")
    assert len(refused) == 1 and refused[0]["pair"] == "ETH/USD" and refused[0]["why"] == ["learning_off"]
    assert [r["reason"] for r in rows_of(d, cid, "exit")] == ["target"]         # its open trade was still managed
    # Nothing is drawn, tested or admitted while it is off.
    monkeypatch.undo()
    before = len(journal(d))
    assert challengers.run(v.now, d, CFG, MARKET, a) == 0 and len(journal(d)) == before
    assert control.main(["learning-on"], d) == 0 and not risk.learning_file(d).exists()
    assert challengers.active(d, CFG)[1] == {}
    assert [r["action"] for r in journal(d) if r["kind"] == "control"] == ["learning-off", "learning-on"]
    assert ledger.verify_chain(d.journal) == []


# ---------------------------------------------------------------- retirement
def exits(cid: str, rs: list[float], t0: float = NOW, step: float = 86_400.0) -> list[dict]:
    return [{"kind": "exit", "sleeve": cid, "id": str(i), "r": r, "t": challengers._iso(t0 + i * step)} for i, r in enumerate(rs)]


def test_the_three_retirement_rules():
    rule = CFG["learning"]["challengers"]["retire"]
    rec = {"id": "ch-x", "admitted": challengers._iso(NOW)}
    later = NOW + 40 * 86_400
    assert challengers.retire_reason(rec, exits("ch-x", [-1.0, -0.8] * 14 + [-1.0]), 10_000, later, rule) is None   # 29 trades
    why = challengers.retire_reason(rec, exits("ch-x", [-1.0, -0.8] * 15), 10_000, later, rule)
    assert why is not None and why[0] == "mean_r_below_zero" and why[1]["trades"] == 30 and why[1]["ci_high"] < 0
    assert challengers.retire_reason(rec, exits("ch-x", [-1.0, 1.2] * 15), 10_000, later, rule) is None            # not clearly bad
    curve = [{"kind": "sleeve", "sleeve": "ch-x", "equity": e} for e in ("10000.00", "10400.00", "9500.00")]
    assert challengers.retire_reason(rec, curve, 9_500, later, rule) is None                                       # 8.7% off the peak
    why = challengers.retire_reason(rec, curve, 9_300, later, rule)
    assert why is not None and why[0] == "drawdown" and why[1]["max_drawdown_pct"] < -10
    assert challengers.retire_reason(rec, [{"kind": "sleeve", "sleeve": "other", "equity": "1.00"}], 10_000, later, rule) is None
    assert challengers.retire_reason(rec, exits("ch-x", [0.5] * 4), 10_000, NOW + 59 * 86_400, rule) is None       # not 60 days yet
    why = challengers.retire_reason(rec, exits("ch-x", [0.5] * 4), 10_000, NOW + 60 * 86_400, rule)
    assert why == ("idle", {"trades_in_window": 4, "days": 60})
    assert challengers.retire_reason(rec, exits("ch-x", [0.5] * 5), 10_000, NOW + 60 * 86_400, rule) is None


def test_a_retired_challenger_opens_nothing_more_and_its_open_trade_is_managed_to_the_end(crypto, monkeypatch):  # noqa: F811
    d, a, box = crypto
    cid = admit(crypto, monkeypatch)
    v = venue({"XBTUSD": break_rows()})
    run(v, crypto)
    target = Decimal(rows_of(d, cid, "entry")[0]["target"])
    for r in exits(cid, [-1.0, -0.9] * 15, t0=NOW + 900):                       # thirty losers in its record
        ledger.append(d.journal, r)
    assert challengers.run(NOW + 40 * 86_400, d, CFG, MARKET, a) == 0
    state = challengers.load_state(d)
    assert state[cid]["status"] == "retired" and state[cid]["retired"]["why"] == "mean_r_below_zero"
    assert any("retired" in m["title"] for m in box)
    assert challengers.active(d, CFG)[1] == {cid: "retired"}                    # still run: it holds a position
    v.now += H4
    eth = break_rows(B0 + H4)
    v.fill("ETHUSD", eth, float(eth[-1][4]))
    minute = B0 + H4 + 60
    v.ohlc[("XBTUSD", 1)] = ([row(minute, 104.2, float(target) + 1, 104.1, float(target) + 0.5)], minute)
    v.quote["XBTUSD"] = (float(target) + 0.4, float(target) + 0.5)
    run(v, crypto)
    assert rows_of(d, cid, "refused")[-1]["why"] == ["retired"] and len(rows_of(d, cid, "entry")) == 1
    assert rows_of(d, cid, "exit")[-1]["reason"] == "target" and book_of(d, cid).positions == {}
    assert challengers.active(d, CFG) == ({}, {})                               # flat and retired: no longer run
    assert (risk.sleeve_dir(d, cid) / "book.json").exists()                     # its book and record stay
    # It is still a trial, and it is never admitted again.
    assert challengers.run(NOW + 41 * 86_400, d, CFG, MARKET, a) == 0
    assert challengers.load_state(d)[cid]["status"] == "retired"


def test_the_run_refuses_on_a_broken_chain_or_a_book_that_is_not_the_tournaments(crypto):  # noqa: F811
    d, a, _ = crypto
    d.chain_flag.write_text("x")
    assert challengers.run(NOW, d, CFG, MARKET, a) == 2
    d.chain_flag.unlink()
    odd = {**CFG, "learning": {**CFG["learning"], "challengers": {**CFG["learning"]["challengers"], "start_equity": 5000}}}
    assert challengers.run(NOW, d, odd, MARKET, a) == 2 and journal(d) == []


# ---------------------------------------------------------------- the job
def test_the_daily_job_is_not_a_trading_job_and_a_deploy_waits_for_it(tmp_path):
    job = JOBS["crypto-challengers"]
    assert not job.trading and job.desk == "crypto" and job.interval_s is None and job.deadline_min == 60
    assert job.runtime_max_h * 60 > job.deadline_min + 1
    assert "OnCalendar=Mon..Sun 03:30 America/New_York" in units.timer(job)     # an hour that exists every night
    service = units.service(job, Path("/r"))
    assert "MemoryMax=1536M" in service and "-m wt.ops.jobs run crypto-challengers" in service
    from wt.ops.locks import job_lock
    with job_lock("crypto-challengers"), deploy.quiesced(wait_s=0) as busy:
        assert busy == ["crypto-challengers"]
    with deploy.quiesced(wait_s=0) as busy:
        assert busy == []


# ---------------------------------------------------------------- the dashboard's list
def test_the_snapshot_lists_every_challenger_with_its_verdict_and_writes_nothing(crypto, monkeypatch):  # noqa: F811
    import datetime as dt
    d, a, _ = crypto
    gate_returning(monkeypatch, FAIL)
    assert challengers.run(NOW, d, CFG, MARKET, a) == 0                         # two drawn, both fail
    cid = rules.challenger_id(WIDE)
    challengers.note(d, cid, "registered", NOW + 60, dials=WIDE, rules=challengers.describe(WIDE), slot="random",
                     of=None, week=WEEK, n_trials=10)
    challengers.state_path(d).unlink()
    now = dt.datetime.fromtimestamp(NOW + 120, dt.UTC)
    view = snapshot.challengers_view(d, CFG, journal(d), now)
    assert view is not None and not challengers.state_path(d).exists()          # read from the journal's rows alone
    assert (view["registered"], view["failed"], view["live"], view["drawn_this_week"]) == (3, 2, 0, 3)
    assert (view["learning"], view["per_week"], view["max_live"], view["max_registered"]) == ("on", 2, 6, 60)
    newest, older = view["list"][0], view["list"][-1]
    assert (newest["id"], newest["status"], newest["trades"], newest["failed_on"]) == (cid, "registered", None, [])
    assert older["status"] == "failed" and older["failed_on"] == ["too_few_trades"] and older["mean_r"] == -0.4
    assert older["ci_low"] == 0.05 and older["control_p"] == 0.01 and older["rules"]
    assert (older["confirm_passed"], older["confirm_trades"]) == (None, None)   # it failed gate C1: never run on earlier history
    challengers.note(d, "ch-conf", "registered", NOW, dials=WIDE, rules="r", slot="random", of=None, week=WEEK, n_trials=11)
    challengers.note(d, "ch-conf", "c1", NOW, passed=False, failed_on=[challengers.UNCONFIRMED], base={"trades": 40, "mean_r": 0.3},
                     confirm={"passed": False, "trades": 22, "mean_r": -0.12, "profit_factor": 0.8})
    conf = snapshot.challengers_view(d, CFG, journal(d), now)["list"][0]
    assert (conf["id"], conf["status"], conf["confirm_passed"], conf["confirm_trades"], conf["confirm_mean_r"],
            conf["confirm_profit_factor"]) == ("ch-conf", "failed", False, 22, -0.12, 0.8)
    risk.learning_file(d).write_text("off\n")
    assert snapshot.challengers_view(d, CFG, journal(d), now)["learning"] == "off"
    assert snapshot.challengers_view(d, {**CFG, "learning": {}}, journal(d), now) is None
    # The whole section passes the published contract.
    clean = snapshot.publish.Sanitizer(lambda x: x, None, strict=False).apply(
        {"challengers": snapshot.ALLOW["challengers"]}, {"challengers": view})
    assert clean["challengers"]["list"][0]["id"] == cid and len(clean["challengers"]["list"]) == 3


# ---------------------------------------------------------------- confirmation on earlier history (DEC-0021)
def _registered(desk, dials=WIDE, now=NOW) -> str:
    cid = rules.challenger_id(dials)
    challengers.note(desk, cid, "registered", now, dials=dials, rules=challengers.describe(dials), slot="random",
                     of=None, week="2020-W01", n_trials=8)
    return cid


def test_a_challenger_that_passes_c1_and_is_not_confirmed_never_trades(crypto, monkeypatch, earlier_history):  # noqa: F811
    desk, alerts, _ = crypto
    cid = _registered(desk)
    gate_returning(monkeypatch, PASS)
    monkeypatch.setattr(challengers, "draw", lambda *a, **k: [])
    lost = {**CONFIRMED, "passed": False, "mean_r": -0.1, "profit_factor": 0.8}
    monkeypatch.setattr(challengers, "confirmed", lambda *a, **k: dict(lost))
    assert challengers.run(NOW, desk, CFG, MARKET, alerts) == 0
    rec = challengers.load_state(desk)[cid]
    assert rec["status"] == "failed" and challengers.UNCONFIRMED in rec["c1"]["failed_on"]
    assert rec["c1"]["passed"] is False and rec["c1"]["confirm"]["mean_r"] == -0.1
    events = [r["event"] for r in journal(desk) if r["kind"] == "challenger"]
    assert events == ["registered", "c1"]                                # judged once, and not admitted
    # The journal alone gives the same record: a lost cache does not forget why it failed.
    again = challengers.state_of(journal(desk))[cid]
    assert again["status"] == "failed" and again["c1"]["confirm"] == rec["c1"]["confirm"]
    # A later run does not judge it again, and it is still not admitted.
    monkeypatch.setattr(challengers, "confirmed", lambda *a, **k: dict(CONFIRMED))
    assert challengers.run(NOW + 86_400, desk, CFG, MARKET, alerts) == 0
    assert challengers.load_state(desk)[cid]["status"] == "failed"


def test_the_earlier_history_is_only_touched_by_a_challenger_that_passed_c1(crypto, monkeypatch, earlier_history):  # noqa: F811
    desk, alerts, _ = crypto
    cid = _registered(desk)
    gate_returning(monkeypatch, FAIL)
    monkeypatch.setattr(challengers, "draw", lambda *a, **k: [])
    assert challengers.run(NOW, desk, CFG, MARKET, alerts) == 0
    assert earlier_history == [] and "confirm" not in [k for k, v in challengers.load_state(desk)[cid]["c1"].items() if v]
    other = _registered(desk, {**WIDE, "btc_filter": not WIDE.get("btc_filter", False)})
    gate_returning(monkeypatch, PASS)
    assert challengers.run(NOW + 86_400, desk, CFG, MARKET, alerts) == 0
    assert earlier_history == [other]                                    # once, for the one that passed
    assert challengers.load_state(desk)[other]["status"] == challengers.LIVE


def test_without_the_earlier_history_a_challenger_waits_and_is_never_admitted_unconfirmed(crypto, monkeypatch):  # noqa: F811
    from wt.crypto.data import DataError
    desk, alerts, box = crypto
    cid = _registered(desk)
    gate_returning(monkeypatch, PASS)
    monkeypatch.setattr(challengers, "draw", lambda *a, **k: [])
    monkeypatch.setattr(challengers, "confirmed", lambda *a, **k: (_ for _ in ()).throw(DataError("candles: HTTPError")))
    assert challengers.run(NOW, desk, CFG, MARKET, alerts) == 0
    assert challengers.load_state(desk)[cid]["status"] == "registered"
    assert [r["event"] for r in journal(desk) if r["kind"] == "challenger"] == ["registered"]     # no verdict was written
    assert any("waiting for earlier history" in str(m) for m in box)
    monkeypatch.setattr(challengers, "confirmed", lambda *a, **k: dict(CONFIRMED))
    assert challengers.run(NOW + 86_400, desk, CFG, MARKET, alerts) == 0                           # the next run judges it
    assert challengers.load_state(desk)[cid]["status"] == challengers.LIVE


def test_the_confirmation_runs_on_the_span_before_c1s_and_needs_trades_and_a_profit(monkeypatch):
    seen = {}

    def fake_confirm(cfg, dials, hourly, infos, start, end):
        seen.update(span=(start, end), pairs=sorted(hourly))
        return dict(CONFIRMED)
    monkeypatch.undo()                                                   # the real `confirmed`, with its own stubs
    monkeypatch.setattr(challengers, "confirm", fake_confirm)
    hourly, infos = MARKET
    challengers.confirmed(ONE, WIDE, 1000, 3000, ({**hourly, "ETH/USD": []}, infos))
    assert seen == {"span": (-1000, 1000), "pairs": ["BTC/USD"]}         # the two "years" before, never C1's own
    from wt.crypto.data import DataError
    with pytest.raises(DataError):
        challengers.confirmed(ONE, WIDE, 1000, 3000, ({"BTC/USD": []}, infos))
    monkeypatch.undo()
    # The real run on a thin history: one or two trades are not a confirmation, whatever they made.
    got = challengers.confirm(ONE, WIDE, hourly, infos, B0, B0 + H4 * 5)
    assert got["passed"] is False and got["trades"] < challengers.MIN_CONFIRM_TRADES and got["span"] == [B0, B0 + H4 * 5]
    from wt.crypto import backtest as bt
    for trades, mean_r, pf, ok in ((15, 0.1, 1.2, True), (14, 0.5, 2.0, False), (40, 0.0, 1.0, False),
                                   (40, -0.1, 0.9, False), (40, 0.1, None, True), (40, 0.1, 1.0, False)):
        monkeypatch.setattr(bt, "run", lambda *a, **k: [])
        monkeypatch.setattr(bt, "summary", lambda *a, t=trades, m=mean_r, p=pf, **k: {"trades": t, "mean_r": m, "profit_factor": p})
        assert challengers.confirm(ONE, WIDE, hourly, infos, 0, 1)["passed"] is ok, (trades, mean_r, pf)


def test_a_failed_fetch_is_tried_again_before_it_costs_a_challenger_a_day():
    from wt.crypto.data import DataError
    calls, waits = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise DataError("candles: ConnectionError")
        return "bars"
    assert challengers._retried(flaky, waits.append) == "bars" and len(calls) == 3 and waits == [5.0, 10.0]
    calls.clear()
    with pytest.raises(DataError):
        challengers._retried(lambda: (_ for _ in ()).throw(DataError("down")), waits.append)
