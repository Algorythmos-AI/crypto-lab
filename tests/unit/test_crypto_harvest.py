"""The data harvest (DEC-0027): the registered rules on more coins and their own roomy books, run after the
tournament in the same cycle, recorded in their own journal, and unable to change the tournament or the baseline.
The real cycle on the scripted venue of test_crypto_sleeves.py."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest
import yaml
from test_crypto_sleeves import B0, CFG, NOW, PAIRS, ROOT, VENUE_PAIRS, Book, break_rows, journal, rows_of, venue
from test_crypto_sleeves import crypto  # noqa: F401 — the fixture

from wt.core import desk as desks
from wt.core import ledger
from wt.crypto import cycle, harvest, risk, rules

FILE_CFG = yaml.safe_load((ROOT / "config/crypto.yaml").read_text())
# The harvest as configured, on the eight pairs the scripted venue knows.
HCFG = {**CFG, "harvest": {**FILE_CFG["harvest"], "pairs": dict(PAIRS)}}


def run(v, crypto, cfg=HCFG) -> int:  # noqa: F811
    d, a, _ = crypto
    return cycle.run(now=v.now, api=v.api(), desk=d, cfg=cfg, limits=risk.load_limits("C"), alerts=a)


def hjournal(d) -> list[dict]:
    p = harvest.desk_of(d).journal
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def hrows(d, sleeve: str, kind: str) -> list[dict]:
    return [r for r in hjournal(d) if r.get("sleeve") == sleeve and r["kind"] == kind]


def hbook(d, sleeve: str) -> Book:
    return Book.load(risk.sleeve_dir(harvest.desk_of(d), sleeve) / "book.json", Decimal(10_000))


# ---------------------------------------------------------------- the configuration

def test_the_harvest_is_the_registered_rules_unchanged_on_the_thirty_training_pairs():
    hv = FILE_CFG["harvest"]
    assert hv["decision"] == "DEC-0027" and hv["rules"] == list(rules.NAMES)
    assert list(hv["pairs"]) == FILE_CFG["learning"]["training_pairs"]
    assert {k: v for k, v in hv["pairs"].items() if k in FILE_CFG["sleeves"]["common"]["pairs"]} == \
        FILE_CFG["sleeves"]["common"]["pairs"]
    sp = harvest.specs(FILE_CFG)
    assert list(sp) == ["h-trend", "h-break", "h-dip"]
    for n in rules.NAMES:
        assert sp[f"h-{n}"].p == FILE_CFG["sleeves"][n] and sp[f"h-{n}"].c == rules.Common.of(FILE_CFG["sleeves"]["common"])
    two = harvest.specs({**FILE_CFG, "harvest": {**FILE_CFG["harvest"], "timeframes": [240, 60]}})
    assert list(two) == ["h-trend", "h-break", "h-dip", "h-trend-60m", "h-break-60m", "h-dip-60m"]
    assert two["h-dip-60m"].c.timeframe_min == 60 and two["h-dip-60m"].p == FILE_CFG["sleeves"]["dip"]


def test_the_harvest_limits_leave_room_for_a_position_in_every_coin():
    ch, ct = risk.load_sleeve_limits("CH"), risk.load_sleeve_limits("CT")
    assert ch.max_positions >= 30 and ch.max_position_pct * 20 <= ch.max_exposure_pct <= 100
    assert ch.risk_pct < ct.risk_pct and ch.max_entries_per_day > ct.max_entries_per_day


def test_the_harvest_journal_is_a_crypto_ledger_and_the_desk_journal_is_unchanged():
    d = desks.DESKS["crypto"]
    assert d.journal == desks.CRYPTO_DIR / "crypto_journal.jsonl"
    assert ("crypto-harvest", desks.CRYPTO_DIR / "harvest" / "harvest_journal.jsonl") in d.ledgers
    assert harvest.desk_of(d).journal == desks.CRYPTO_DIR / "harvest" / "harvest_journal.jsonl"


def test_the_pairs_start_at_a_different_place_every_cycle():
    p = {"A": "a", "B": "b", "C": "c"}
    assert [k for k, _ in harvest.rotated(p, 0)] == ["A", "B", "C"]
    assert [k for k, _ in harvest.rotated(p, 900)] == ["B", "C", "A"]
    assert [k for k, _ in harvest.rotated(p, 2 * 900 + 30)] == ["C", "A", "B"]


# ---------------------------------------------------------------- the cycle

def test_a_signal_the_full_tournament_book_refuses_is_traded_by_the_harvest(crypto):  # noqa: F811
    d, _, _ = crypto
    five = ("XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "LINKUSD")
    v = venue({k: break_rows() for k in five})
    assert run(v, crypto) == 0
    # The tournament's BREAK book holds three positions at most (CT); the harvest's takes all five.
    assert len(rows_of(d, "break", "entry")) == 3
    assert "positions" in sum((r["why"] for r in rows_of(d, "break", "refused")), [])
    entries = hrows(d, "h-break", "entry")
    assert sorted(e["pair"] for e in entries) == sorted(k for k, v_ in PAIRS.items() if v_ in five)
    assert all(e["stage"] == "harvest" and e["decision"] == "DEC-0027" for e in entries)
    assert len(hbook(d, "h-break").positions) == 5
    # Every signal is recorded with its inputs, bought or not, for the learning data.
    assert len(hrows(d, "h-break", "signal")) == 5 and all("inputs" in r for r in hrows(d, "h-break", "signal"))
    # The tournament's journal holds no harvest row, and the harvest's holds no tournament row.
    assert not [r for r in journal(d) if str(r.get("sleeve", "")).startswith("h-") or r.get("stage") == "harvest"]
    assert all(str(r.get("sleeve", "")).startswith("h-") for r in hjournal(d))
    assert ledger.verify_chain(harvest.desk_of(d).journal) == []
    obs = (harvest.desk_of(d).state_dir / "observations" / "harvest-2026-10-04.jsonl").read_text().splitlines()
    assert len(obs) == 3 * 8


def test_the_harvest_changes_nothing_the_tournament_or_the_baseline_does(crypto, tmp_path):  # noqa: F811
    import dataclasses
    v = venue({k: break_rows() for k in ("XBTUSD", "ETHUSD")})
    d, a, box = crypto
    other = dataclasses.replace(d, state_dir=tmp_path / "other", kill_file=tmp_path / "other/KILL",
                                ledgers=(("crypto", tmp_path / "other/crypto_journal.jsonl"),),
                                chain_flag=tmp_path / "other/chain-broken")
    other.state_dir.mkdir()
    run(v, crypto)
    cycle.run(now=v.now, api=venue({k: break_rows() for k in ("XBTUSD", "ETHUSD")}).api(), desk=other, cfg=CFG,
              limits=risk.load_limits("C"), alerts=a)

    def strip(rows):
        return [{k: x for k, x in r.items() if k not in ("id", "prev_sha256", "t")} for r in rows]
    assert strip(journal(d)) == strip(journal(other))


def test_the_kill_switch_and_the_harvest_switch_stop_harvest_entries_and_only_the_harvest_switch_spares_the_rest(crypto):  # noqa: F811
    d, _, _ = crypto
    d.kill_file.write_text("on")
    run(venue({"XBTUSD": break_rows()}), crypto)
    assert [r["why"] for r in hrows(d, "h-break", "refused")] == [["kill"]] and not hbook(d, "h-break").positions
    d.kill_file.unlink()
    harvest.off_file(d).write_text("off")
    v = venue({"ETHUSD": break_rows(B0 + 14_400)})
    v.now = NOW + 14_400
    run(v, crypto)
    assert [r["why"] for r in hrows(d, "h-break", "refused")][-1] == [harvest.OFF]
    assert not hbook(d, "h-break").positions
    assert [e["pair"] for e in rows_of(d, "break", "entry")] == ["ETH/USD"]           # the tournament trades on


def test_a_pair_the_venue_does_not_know_costs_the_others_nothing(crypto):  # noqa: F811
    d, _, _ = crypto
    cfg = {**HCFG, "harvest": {**HCFG["harvest"], "pairs": {**PAIRS, "NOPE/USD": "NOPEUSD"}}}
    v = venue({"XBTUSD": break_rows()})
    assert run(v, crypto, cfg) == 0
    assert [e["pair"] for e in hrows(d, "h-break", "entry")] == ["BTC/USD"]
    state = json.loads((harvest.desk_of(d).state_dir / "data.json").read_text())
    assert set(state["info"]) == set(VENUE_PAIRS)


def test_a_fault_in_the_harvest_never_fails_the_cycle(crypto, monkeypatch):  # noqa: F811
    d, _, box = crypto

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(harvest, "run", boom)
    assert run(venue({"XBTUSD": break_rows()}), crypto) == 0
    assert [e["pair"] for e in rows_of(d, "break", "entry")] == ["BTC/USD"]
    assert any("data harvest did not run" in str(m) for m in box)


def test_the_harvest_spends_only_its_own_budget_of_calls(crypto):  # noqa: F811
    d, _, _ = crypto
    cfg = {**HCFG, "harvest": {**HCFG["harvest"], "max_calls": 5}}
    v = venue()
    run(v, crypto, cfg)
    state = json.loads((harvest.desk_of(d).state_dir / "data.json").read_text())
    assert state["info_day"] == "2026-10-04"                                       # the books were saved
    assert len([r for r in hjournal(d) if r["kind"] == "sleeve"]) == 3


def test_the_harvest_switch_is_an_owner_control_with_a_journal_line(crypto, monkeypatch, tmp_path):  # noqa: F811
    from wt.crypto import control
    from wt.ops import locks
    d, _, _ = crypto
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    assert control.main(["harvest-off"], d) == 0 and harvest.off_file(d).exists()
    assert control.main(["harvest-off"], d) == 0
    assert control.main(["harvest-on"], d) == 0 and not harvest.off_file(d).exists()
    assert [(r["kind"], r["action"]) for r in journal(d)] == [("control", "harvest-off"), ("control", "harvest-on")]
    latch = risk.sleeve_dir(harvest.desk_of(d), "h-dip") / "latch"
    latch.parent.mkdir(parents=True)
    latch.write_text("2026-10-04: realised -2600")
    assert control.main(["reset-latch"], d) == 0 and not latch.exists()


@pytest.mark.parametrize("off", [False, True])
def test_the_status_lines_name_every_book(crypto, off):  # noqa: F811
    d, _, _ = crypto
    if off:
        harvest.off_file(d).write_text("off")
    out = harvest.status(d, HCFG)
    assert out.splitlines()[0].endswith("OFF" if off else "on") and len(out.splitlines()) == 4
