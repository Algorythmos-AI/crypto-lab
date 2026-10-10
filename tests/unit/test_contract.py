"""The snapshot contract (contracts/): what the dashboard in another repository vendors and validates with."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from wt.crypto import snapshot
from wt.ops import contract

ROOT = Path(__file__).resolve().parents[2]


def test_the_schema_id_did_not_change_when_the_desk_moved():
    """The dashboard files a snapshot by its `schema`: a new id would be a snapshot it has no slot for."""
    schema = json.loads((ROOT / contract.SCHEMA).read_text())
    assert schema["$id"] == snapshot.SCHEMA_ID == "trading-lab/crypto-snapshot" and snapshot.SCHEMA_VERSION == 1
    assert schema == snapshot.to_schema() and schema["additionalProperties"] is False
    assert snapshot.SCHEMA_PATH == ROOT / contract.SCHEMA
    fixture = json.loads((ROOT / contract.FIXTURE).read_text())
    assert fixture["schema"] == "trading-lab/crypto-snapshot" and fixture["schema_version"] == 1
    assert snapshot.validate(fixture) == []


def test_the_recorded_hashes_are_the_files_and_the_check_says_so(capsys):
    lines = (ROOT / contract.SUMS).read_text().splitlines()
    assert [line.split("  ")[1] for line in lines] == ["crypto.schema.json", "crypto.v1.json"]
    for line in lines:
        digest, name = line.split("  ")
        assert hashlib.sha256((ROOT / contract.DIR / name).read_bytes()).hexdigest() == digest
    assert contract.main(["--check"]) == 0 and "matches" in capsys.readouterr().out
    assert contract.main([]) == 0 and capsys.readouterr().out == (ROOT / contract.SUMS).read_text()
    assert contract.main(["--nonsense"]) == 2


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_the_guard_passes_an_unchanged_schema_and_wants_the_exact_hash_for_a_changed_one(tmp_path, monkeypatch):
    monkeypatch.delenv(contract.ACK_ENV, raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    f = tmp_path / contract.SCHEMA
    f.parent.mkdir()
    f.write_text("one\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "one")
    one = git(tmp_path, "rev-parse", "HEAD")
    same = contract.guard(tmp_path, one)
    assert same.ok and not same.acknowledged and same.live == same.target == contract.sha256(b"one\n")
    git(tmp_path, "checkout", "-q", "-b", "next")
    f.write_text("two\n")
    git(tmp_path, "commit", "-q", "-am", "two")
    two = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    refused = contract.guard(tmp_path, two)
    assert not refused.ok and "must be deployed with the new schema first" in refused.why
    want = contract.sha256(b"two\n")
    assert not contract.guard(tmp_path, two, ack="deadbeefdead").ok
    assert not contract.guard(tmp_path, two, ack=want[:8]).ok
    yes = contract.guard(tmp_path, two, ack=want.upper())
    assert yes.ok and yes.acknowledged and (yes.live, yes.target) == (contract.sha256(b"one\n"), want)
    monkeypatch.setenv(contract.ACK_ENV, want)
    assert contract.guard(tmp_path, two).ok
    assert not contract.guard(tmp_path, "0" * 40).ok                      # not a commit: no contract to agree on
