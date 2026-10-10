"""Off-host protection of the desk's evidence (wt.ops.backup): its own restic host and anchor path, write-once
anchors, the chain flag, restic outcomes, the restore test, and the check that a moved journal extends an old anchor.

Origin: trading-lab-research `tests/unit/test_backup.py` at 217a4eb (trimmed and rewritten for this repository's
backup). Removed: the stocks ledgers' cases. The fake anchor store and the chained-file helper are unchanged."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wt.core import desk as desks
from wt.core import ledger
from wt.ops import alerts, backup
from wt.ops.r2 import Response

DAY = dt.date(2026, 10, 20)


class Anchors:
    configured = True

    def __init__(self):
        self.objs: dict[str, bytes] = {}

    def put(self, bucket, key, body, if_match=None, if_none_match=None, content_type=""):
        if if_none_match == "*" and key in self.objs:
            return Response(412, b"", None, None)
        self.objs[key] = body
        return Response(200, b"", '"e"', None)

    def get(self, bucket, key):
        return Response(200, self.objs[key], '"e"', None) if key in self.objs else Response(404, b"", None, None)


def chained(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prev = ledger.GENESIS
    lines = []
    for i in range(n):
        line = json.dumps({"i": i, "prev_sha256": prev}).encode()
        lines.append(line)
        prev = hashlib.sha256(line).hexdigest()
    path.write_bytes(b"\n".join(lines) + b"\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    d = tmp_path / "var" / "crypto"
    desk = dataclasses.replace(desks.DESKS["crypto"], state_dir=d, kill_file=d / "KILL",
                               ledgers=(("crypto-harvest", d / "harvest" / "harvest_journal.jsonl"),
                                        ("crypto", d / "crypto_journal.jsonl")), chain_flag=d / "chain-broken")
    monkeypatch.setitem(desks.DESKS, "crypto", desk)
    monkeypatch.setattr(backup, "DIGEST", tmp_path / "digest.json")
    monkeypatch.setattr(backup, "ROOT", tmp_path)
    monkeypatch.setenv("WT_HOST_ID", "host-a")
    monkeypatch.delenv("WT_ROLE", raising=False)
    box: list[dict] = []
    pings: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts, "_send", lambda msg, topic, server, timeout=5.0: box.append(msg) or True)
    monkeypatch.setattr(backup.hc, "ping", lambda slug, signal="", body="", timeout=5.0: pings.append((slug, signal)) or True)
    calls: list[tuple] = []
    monkeypatch.setattr(backup, "restic", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    return desk, tmp_path, box, alerts.Alerts(root=tmp_path / "alerts", topic="t"), pings, calls


def test_its_restic_host_and_anchor_path_are_its_own(world, monkeypatch):
    """The repository and the bucket are shared with the repository the desk came from, whose snapshots carry the
    machine's name as their host and whose crypto anchors were `anchors/crypto/<day>.json`."""
    assert backup.host() == "host-a-crypto-lab" != "host-a"
    assert backup.anchor_key(DAY) == "anchors/crypto-lab/2026-10-20.json"
    assert not backup.anchor_key(DAY).startswith("anchors/crypto/") and backup.HC_SLUG == "wt-crypto-backup"
    monkeypatch.setenv("WT_ROLE", "shadow")
    assert backup.anchor_key(DAY) == "shadow/host-a-crypto-lab/anchors/crypto-lab/2026-10-20.json"


def test_a_backup_with_no_desk_refuses_and_says_so(world):
    desk, tmp, box, a, pings, calls = world
    assert backup.nightly(alerts=a, client=Anchors(), today=DAY) == 2
    assert calls == [] and pings == [("wt-crypto-backup", "fail")] and box[0]["title"] == "Crypto Lab: backup found no desk"
    assert not desk.state_dir.exists()


def test_the_nightly_run_backs_up_under_its_own_host_and_anchors_both_journals(world):
    desk, tmp, box, a, pings, calls = world
    chained(desk.journal, 4)
    chained(desk.ledgers[0][1], 2)
    (tmp / "var").mkdir(exist_ok=True)
    (tmp / "logs").mkdir()
    s = Anchors()
    assert backup.nightly(alerts=a, client=s, today=DAY) == 0
    args = calls[0]
    assert args[:3] == ("backup", "--host", "host-a-crypto-lab") and "crypto-lab" in args and "role:primary" in args
    assert args[-2:] == ("var", "logs")                                     # a Tuesday: the history cache is not in it
    anchored = json.loads(s.objs["anchors/crypto-lab/2026-10-20.json"])
    assert set(anchored) == {"crypto", "crypto-harvest"} and anchored["crypto"]["lines"] == 4
    assert pings == [("wt-crypto-backup", "")] and box == [] and not desk.chain_flag.exists()
    digest = json.loads((tmp / "digest.json").read_text())
    assert digest["verified"] and digest["anchored"] and digest["day"] == "2026-10-20"
    # A second backup of the same day: the journal has grown, which is normal and not an alarm.
    with open(desk.journal, "ab") as fh:
        prev = hashlib.sha256(desk.journal.read_bytes().splitlines()[-1]).hexdigest()
        fh.write(json.dumps({"i": 9, "prev_sha256": prev}).encode() + b"\n")
    assert backup.nightly(alerts=a, client=s, today=DAY) == 0
    (tmp / "data").mkdir()
    backup.nightly(alerts=a, client=s, today=dt.date(2026, 10, 23))         # a Friday: the cache goes too
    assert calls[-1][-3:] == ("var", "logs", "data")


def test_a_broken_chain_turns_the_desks_entries_off_and_pages(world):
    desk, tmp, box, a, pings, calls = world
    chained(desk.journal, 4)
    lines = desk.journal.read_bytes().splitlines()
    lines[1] = lines[1].replace(b'"i": 1', b'"i": 7')
    desk.journal.write_bytes(b"\n".join(lines) + b"\n")
    assert backup.nightly(alerts=a, client=Anchors(), today=DAY) == 0       # the backup itself worked
    assert desk.chain_flag.exists() and "crypto: line 3" in desk.chain_flag.read_text()
    assert box[0]["title"] == "Evidence chain broken: crypto entries off" and pings == [("wt-crypto-backup", "fail")]
    assert "crypto:evidence-chain" in a.firing()


def test_anchors_are_write_once_and_a_rewrite_is_detected(world):
    desk, tmp, box, a, pings, calls = world
    chained(desk.journal, 3)
    s = Anchors()
    paths = {"crypto": desk.journal}
    assert backup.anchor(DAY, backup.chains(), s, paths) == (True, "anchored anchors/crypto-lab/2026-10-20.json")
    assert backup.anchor(DAY, backup.chains(), s, paths)[1].endswith("already anchored")
    chained(desk.journal, 2)                                                # the tail was cut off after anchoring
    ok, why = backup.anchor(DAY, backup.chains(), s, paths)
    assert not ok and "DIFFERENT heads" in why
    chained(desk.journal, 3)
    lines = desk.journal.read_bytes().splitlines()
    desk.journal.write_bytes(b"\n".join([*lines[:2], lines[2].replace(b'"i": 2', b'"i": 8')]) + b"\n")
    ok, why = backup.anchor(DAY, backup.chains(), s, paths)                 # the last line was edited: a new head
    assert not ok and "DIFFERENT heads" in why
    assert backup.anchor(DAY, backup.chains(), SimpleNamespace(configured=False)) == (False, "R2 not configured")


def test_a_failed_restic_run_pages_and_fails_the_check(world, monkeypatch):
    desk, tmp, box, a, pings, calls = world
    chained(desk.journal, 2)
    monkeypatch.setattr(backup, "restic", lambda *x, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    assert backup.nightly(alerts=a, client=Anchors(), today=DAY) == 1
    assert box[0]["title"] == "Crypto Lab: backup failed" and pings == [("wt-crypto-backup", "fail")]


def test_the_restore_test_restores_this_repositorys_own_latest_and_wants_every_ledger_back(world, monkeypatch):
    desk, tmp, box, a, pings, calls = world
    chained(desk.journal, 3)
    chained(desk.ledgers[0][1], 2)

    def restore(*args, **k):
        calls.append(args)
        target = Path(args[args.index("--target") + 1])
        for src in (desk.journal, desk.ledgers[0][1]):
            (target / src.name).write_bytes(src.read_bytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(backup, "restic", restore)
    assert backup.restore_test() == 0
    assert calls[-1][:4] == ("restore", "latest", "--host", "host-a-crypto-lab")
    assert "crypto_journal.jsonl" in calls[-1] and "harvest_journal.jsonl" in calls[-1]

    def only_one(*args, **k):
        target = Path(args[args.index("--target") + 1])
        (target / desk.journal.name).write_bytes(desk.journal.read_bytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(backup, "restic", only_one)
    assert backup.restore_test() == 1                                       # a ledger that did not come back


def test_the_moved_journal_can_be_shown_to_extend_the_old_checkouts_last_anchor(world, capsys):
    """The day the state directory moves between checkouts: the journal on disk must be the one the origin's last
    anchor described, grown and not rewritten."""
    desk, tmp, box, a, pings, calls = world
    chained(desk.journal, 5)
    chained(desk.ledgers[0][1], 2)
    s = Anchors()
    old_key = "anchors/crypto/2026-10-19.json"                              # the origin's path for this desk
    head3 = hashlib.sha256(desk.journal.read_bytes().splitlines()[2]).hexdigest()
    s.objs[old_key] = json.dumps({"crypto": {"lines": 3, "head": head3},
                                  "crypto-harvest": {"lines": 2, "head": ledger.head(desk.ledgers[0][1])[1]}}).encode()
    assert backup.check_extends(old_key, s) == 0
    out = capsys.readouterr().out
    assert "EXTEND it" in out and "crypto: anchored 3 lines" in out and "on disk 5 lines" in out
    s.objs[old_key] = json.dumps({"crypto": {"lines": 3, "head": "0" * 64}}).encode()
    assert backup.check_extends(old_key, s) == 1 and "DO NOT extend" in capsys.readouterr().out
    assert backup.check_extends("anchors/crypto/1999-01-01.json", s) == 2
    assert backup.check_extends(old_key, SimpleNamespace(configured=False)) == 2
