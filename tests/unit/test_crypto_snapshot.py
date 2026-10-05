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
