"""The crypto desk's snapshot contract (wt.crypto.snapshot): schema, fixture, and what it is built from."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest
from test_crypto import CFG, LIMITS, T0, crypto, journal, run, venue_with_a_signal  # noqa: F401 — fixtures

from wt.crypto import snapshot
from wt.ops import publish

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gen_crypto_fixture", ROOT / "scripts" / "gen_crypto_fixture.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def leaves(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from leaves(v)
    elif isinstance(node, list):
        yield from leaves(node[0])
    elif isinstance(node, publish.Map):
        yield from leaves(node.value)
    else:
        yield node


def test_the_committed_schema_is_what_the_allowlist_generates():
    assert json.loads(snapshot.SCHEMA_PATH.read_text()) == snapshot.to_schema()
    s = snapshot.to_schema()
    assert s["$id"] == "trading-lab/crypto-snapshot" != publish.SCHEMA_ID
    assert s["additionalProperties"] is False and s["required"] == ["schema", "schema_version", "run_id", "as_of"]


def test_the_crypto_snapshot_has_no_free_text_field():
    """Numbers, codes, dates and pair names only: nothing here needs the leak check the stocks snapshot runs."""
    assert publish.T not in list(leaves(snapshot.ALLOW))


def test_the_committed_fixture_is_current_and_valid():
    assert gen.main(["--check"]) == 0
    snap = json.loads(gen.FIXTURE.read_text())
    assert snapshot.validate(snap) == []
    assert snap["perf"]["trades"] > 0 and snap["book"]["positions"] and len(snap["market"]) == 3
    assert len(json.dumps(snap)) < publish.BUDGET


def test_a_snapshot_from_real_cycles_validates_and_says_what_happened(crypto, monkeypatch):  # noqa: F811
    desk, a, _ = crypto
    from wt.ops import alerts, heartbeat
    monkeypatch.setattr(alerts, "ALERT_DIR", a.root)
    monkeypatch.setattr(heartbeat, "HEARTBEAT_DIR", desk.state_dir / "hb")
    desk.kill_file.write_text("on")
    v = venue_with_a_signal()
    run(v, crypto)
    a.fire("crypto:data-stale", "x", "y")
    a.fire("job:routine", "x", "y")                                 # the stocks desk's: not in this snapshot
    now = dt.datetime.fromtimestamp(v.now, dt.UTC)
    raw = snapshot.collect(now, desk, CFG)
    snap = snapshot.build(raw, publish.Sanitizer(lambda x: x, None, strict=False), "r1", now)
    assert snapshot.validate(snap) == []
    assert snap["schema"] == "trading-lab/crypto-snapshot" and snap["kill"]["on"] is True
    assert [x["key"] for x in snap["alerts"]["firing"]] == ["crypto:data-stale"]
    assert {m["pair"]: m["would_fire"] for m in snap["market"]} == {"BTC/AUD": True, "ETH/AUD": False, "SOL/AUD": False}
    assert snap["activity"]["refused_7d"] == {"kill": 1} and snap["activity"]["cycles_24h"] == 1
    assert snap["book"]["equity"] == CFG["account"]["start_equity"] and snap["perf"]["trades"] == 0
    w = snap["expected_windows"][0]
    assert w["session"] == "always" and w["start"] < snap["as_of"] < w["end"]      # the watchdog's window is open


def test_the_published_economics_are_the_fee_arithmetic():
    e = snapshot.economics(CFG["strategy"], CFG["costs"])
    assert e == {"net_win_pct": 0.1, "net_loss_pct": -1.4, "breakeven_win_rate": pytest.approx(0.9333, abs=1e-4)}
    assert snapshot.economics({"take_profit_pct": 0.5, "stop_loss_pct": 0.5}, CFG["costs"])["breakeven_win_rate"] == 1.0


def test_gate_c0_needs_enough_days_traded_bars_and_a_tight_median_spread():
    gate = {"min_days": 2, "min_traded_share": 0.9, "max_median_spread_pct": 0.15}

    def obs(pair, day, i, quality=(), spread=0.1):
        return {"pair": pair, "t": T0 + day * 86_400 + i * 900, "quality": list(quality), "spread_pct": spread}
    good = [obs("A", d, i) for d in range(2) for i in range(10)]
    thin = [obs("B", d, i, ["bar_untraded"] if i < 2 else ()) for d in range(2) for i in range(10)]
    wide = [obs("C", d, i, spread=0.3) for d in range(2) for i in range(10)]
    q = snapshot.quality_gate(good + thin + wide, ["A", "B", "C", "D"], gate)
    assert q["days"] == 2 and [p["passes"] for p in q["pairs"]] == [True, False, False, False]
    assert q["pairs"][1]["traded_share"] == 0.8 and q["pairs"][2]["median_spread_pct"] == 0.3
    assert snapshot.quality_gate(good[:10], ["A"], gate)["pairs"][0]["passes"] is False       # one day is not two


def test_verify_reads_the_crypto_desk_from_health(monkeypatch):
    health = {"snapshot_run_id": "stocks-run", "desks": {"crypto": {"run_id": "c1"}}}
    monkeypatch.setattr(publish, "read_health", lambda url, bypass: (health, ""))
    assert snapshot.verify_stored({"run_id": "c1"}, "https://x/api/ingest", None)[0]
    assert not snapshot.verify_stored({"run_id": "stocks-run"}, "https://x/api/ingest", None)[0]
    monkeypatch.setattr(publish, "read_health", lambda url, bypass: (None, "health unreadable (Timeout)"))
    assert snapshot.verify_stored({"run_id": "c1"}, "https://x/api/ingest", None) == (False, "health unreadable (Timeout)")
    monkeypatch.setattr(publish, "read_health", lambda url, bypass: ({"desks": None}, ""))
    assert not snapshot.verify_stored({"run_id": "c1"}, "https://x/api/ingest", None)[0]


def test_a_young_desk_is_held_to_the_cycles_it_could_have_run():
    """Day one read "1 of 96 cycles" and turned the page amber for a desk that had missed nothing."""
    now = dt.datetime(2026, 10, 4, 22, 46, tzinfo=dt.UTC)

    def cyc(minutes_ago, failed=False):
        return {"kind": "cycle", "t": (now - dt.timedelta(minutes=minutes_ago)).isoformat(), "failed": {"x": "y"} if failed else {}}
    a = snapshot.activity([cyc(31), cyc(16), cyc(1)], [], now, 15)
    assert (a["cycles_24h"], a["expected_24h"]) == (3, 3)
    a = snapshot.activity([cyc(31), cyc(16, failed=True), cyc(1)], [], now, 15)
    assert (a["cycles_24h"], a["expected_24h"], a["failed_24h"]) == (2, 3, 1)
    old = [cyc(m) for m in range(3 * 24 * 60, 0, -15)]
    assert snapshot.activity(old, [], now, 15)["expected_24h"] == 96
    assert snapshot.activity([], [], now, 15)["expected_24h"] == 0


def test_the_publish_job_shows_its_previous_finished_run_not_itself_running():
    running = {"job": "dashboard-crypto", "status": "running", "started": "t2", "ended": None}
    done = {"job": "dashboard-crypto", "status": "ok", "started": "t1", "ended": "t1b"}
    cycle_run = {"job": "crypto", "status": "ok"}
    last = {"crypto": cycle_run, "dashboard-crypto": running}
    assert snapshot._last_jobs(last, [done, cycle_run]) == {"crypto": cycle_run, "dashboard-crypto": done}
    assert snapshot._last_jobs(last, [cycle_run]) == {"crypto": cycle_run}             # the very first publish


def test_equity_marks_are_thinned_to_one_per_four_hours_and_the_newest_is_always_kept():
    marks = [(f"2026-10-01T{h:02d}:{m:02d}:10+00:00", 10_000.0 + h * 10 + m) for h in range(0, 10) for m in (0, 15, 30, 45)]
    got = snapshot.thinned(list(reversed(marks)))                # the order they arrive in does not matter
    assert got == [{"t": "2026-10-01T03:00:00+00:00", "equity": 10_075.0}, {"t": "2026-10-01T07:00:00+00:00", "equity": 10_115.0},
                   {"t": "2026-10-01T09:00:00+00:00", "equity": 10_135.0}]
    assert snapshot.thinned([]) == [] and snapshot.thinned([("not a time", 1.0)]) == []
    # 90 days of a book marked every 15 minutes stays small: at most six points a day.
    long = [(f"2026-{mo:02d}-{d:02d}T{h:02d}:00:00+00:00", 1.0) for mo in (7, 8, 9) for d in range(1, 31) for h in range(24)]
    assert len(snapshot.thinned(long)) == 90 * 24 // snapshot.CURVE_STEP_H and snapshot.CURVE_DAYS == 90
    assert len(json.dumps(snapshot.thinned(long))) * 8 < publish.BUDGET        # eight books' curves fit the budget



def test_a_sleeves_funnel_counts_every_signal_once_and_the_refusals_add_up():
    """What became of the signals a rule produced, as counts and codes only: nothing free-text can reach the page."""
    rows = [{"kind": "entry", "t": "2026-10-01T00:00:10+00:00", "pair": "BTC/USD"},
            {"kind": "refused", "t": "2026-10-01T04:00:10+00:00", "pair": "ETH/USD", "why": ["positions", "exposure"]},
            {"kind": "refused", "t": "2026-10-01T04:00:10+00:00", "pair": "SOL/USD", "why": ["positions"]},
            {"kind": "refused", "t": "2026-10-02T00:00:10+00:00", "pair": "BTC/USD", "why": ["desk_coin"]},
            {"kind": "refused", "t": "2026-10-02T04:00:10+00:00", "pair": "XRP/USD", "why": []},
            {"kind": "exit", "t": "2026-10-02T08:00:10+00:00", "pair": "BTC/USD"}, {"kind": "sleeve", "t": "x"}]
    got = snapshot.funnel(rows)
    assert got == {"since": "2026-10-01T00:00:10+00:00", "fired": 5, "entered": 1,
                   "refused": [{"code": "positions", "count": 2}, {"code": "desk_coin", "count": 1}, {"code": "unknown", "count": 1}]}
    assert sum(r["count"] for r in got["refused"]) == got["fired"] - got["entered"]
    assert snapshot.funnel([{"kind": "exit"}, {"kind": "sleeve"}]) is None          # a sleeve that has recorded no signal


def test_the_fixture_carries_the_market_monitor_and_the_books_holdings():
    """Built by the real builder from stored bars and books: every traded pair is read, the correlation is square,
    and the holdings add up to what `desk` reports for the same books."""
    snap = json.loads(gen.FIXTURE.read_text())
    m, e = snap["monitor"], snap["exposure"]
    pairs = [r["pair"] for r in m["pairs"]]
    assert sorted(pairs) == sorted(snap["sleeves"][0]["why_not"][i]["pair"] for i in range(8))
    assert [r["rank"] for r in m["pairs"]] == list(range(1, 9))
    assert [r["ret_30"] for r in m["pairs"]] == sorted((r["ret_30"] for r in m["pairs"]), reverse=True)
    c = m["correlation"]
    assert len(c["rows"]) == len(c["pairs"]) == 8 and all(len(r["with"]) == 8 for r in c["rows"])
    assert m["regime"]["code"] in ("up", "down", "mixed") and m["regime"]["pairs"] == 8
    assert e["equity"] == snap["desk"]["equity"] and [x["pair"] for x in e["coins"]] == snap["desk"]["coins"]
    assert len(e["books"]) == snap["desk"]["books"]
    for s in snap["sleeves"]:
        assert sum(b["n"] for b in s["r_bands"]) == s["trades"]


def test_a_desk_with_no_stored_bars_publishes_no_monitor(tmp_path):
    import dataclasses
    import datetime as dt

    from wt.core.config import load_yaml
    from wt.core.desk import DESKS
    desk = dataclasses.replace(DESKS["crypto"], state_dir=tmp_path)
    cfg = load_yaml("crypto.yaml")
    assert snapshot.monitor_view(desk, cfg, dt.datetime(2026, 10, 2, tzinfo=dt.UTC)) is None
    assert snapshot.monitor_view(desk, {**cfg, "sleeves": None}, dt.datetime(2026, 10, 2, tzinfo=dt.UTC)) is None
    assert snapshot.exposure_view(desk, {**cfg, "sleeves": None}, []) is None
    held = snapshot.exposure_view(desk, cfg, [])
    assert held["coins"] == [] and held["gross"] == 0 and len(held["books"]) == 3
