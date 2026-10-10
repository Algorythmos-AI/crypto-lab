"""The hash-chained ledger (wt.core.ledger): the desk's journal and the harvest's journal are written with it.

Origin: trading-lab-research `tests/unit/test_journal_chain.py` at 217a4eb (trimmed). Removed: the tests of the
stocks paper runner's own log and books, which used the ledger. The tests kept are unchanged.
"""
import json
import multiprocessing as mp
import os

from wt.core import ledger


def test_append_chains_from_genesis_and_verifies(tmp_path):
    p = tmp_path / "j.jsonl"
    for i in range(5):
        ledger.append(p, {"event": "x", "i": i})
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    assert rows[0]["prev_sha256"] == ledger.GENESIS
    assert ledger.verify_chain(p) == []


def test_an_unchained_prefix_is_accepted_and_the_chain_starts_after_it(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text('{"event": "old"}\n{"event": "older"}\n')
    ledger.append(p, {"event": "new"})
    assert ledger.verify_chain(p) == []


def test_unchained_lines_after_a_rollback_are_notes_not_breaks(tmp_path):
    """Older code (a rollback) writes unchained lines. That must not turn entries off for good; the next chained
    line commits to them, so changing them later is still a break."""
    p = tmp_path / "j.jsonl"
    ledger.append(p, {"event": "new code"})
    with open(p, "a") as f:
        f.write('{"event": "old code after a rollback"}\n')
    ledger.append(p, {"event": "new code again"})
    assert ledger.verify_chain(p) == []
    assert ledger.notes(p) == ["line 2: unchained line after the chain started (older code?)"]
    p.write_text(p.read_text().replace("after a rollback", "EDITED"))
    assert ledger.verify_chain(p) == ["line 3: prev_sha256 does not match line 2"]


def test_edits_and_deletions_are_detected(tmp_path):
    p = tmp_path / "j.jsonl"
    for i in range(4):
        ledger.append(p, {"event": "x", "R": i})
    lines = p.read_text().splitlines()
    p.write_text("\n".join([lines[0], lines[1].replace('"R": 1', '"R": 9'), *lines[2:]]) + "\n")
    assert ledger.verify_chain(p) == ["line 3: prev_sha256 does not match line 2"]
    p.write_text("\n".join([lines[0], *lines[2:]]) + "\n")
    assert ledger.verify_chain(p)


def test_a_torn_last_line_is_ended_not_glued(tmp_path):
    p = tmp_path / "j.jsonl"
    ledger.append(p, {"event": "a"})
    with open(p, "ab") as f:
        f.write(b'{"event": "torn')                       # a writer killed mid-line
    ledger.append(p, {"event": "b"})
    lines = p.read_text().splitlines()
    assert lines[1] == '{"event": "torn' and json.loads(lines[2])["event"] == "b"
    assert ledger.verify_chain(p) == []                                 # sealed by the next line: not a break
    assert ledger.notes(p) == ["line 2: not a JSON object (a torn line?)"]


def test_a_last_line_longer_than_the_tail_window(tmp_path):
    p = tmp_path / "j.jsonl"
    ledger.append(p, {"event": "big", "blob": "x" * (ledger.TAIL * 2)})
    ledger.append(p, {"event": "after"})
    assert ledger.verify_chain(p) == []


def _writer(path, n):
    for i in range(n):
        ledger.append(path, {"event": "w", "pid": os.getpid(), "i": i})


def test_concurrent_writers_keep_one_chain(tmp_path):
    p = tmp_path / "j.jsonl"
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(p, 40)) for _ in range(3)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(30)
    assert len(p.read_text().splitlines()) == 120 and ledger.verify_chain(p) == []


def test_a_chained_line_commits_to_every_line_since_the_last_chained_one(tmp_path):
    """Second review: editing the chained line before an unchained run, or dropping a line of the run, is a break."""
    p = tmp_path / "j.jsonl"
    ledger.append(p, {"event": "a", "R": -1})
    with open(p, "a") as f:
        f.write('{"event": "old code 1"}\n{"event": "old code 2"}\n')
    ledger.append(p, {"event": "b"})
    assert ledger.verify_chain(p) == []
    lines = p.read_text().splitlines()
    p.write_text("\n".join([lines[0].replace('"R": -1', '"R": 3'), *lines[1:]]) + "\n")
    assert ledger.verify_chain(p)                                   # the edited chained line is caught
    p.write_text("\n".join([lines[0], lines[1], lines[3]]) + "\n")
    assert ledger.verify_chain(p)                                   # a dropped unchained line is caught


def test_fully_chained_and_legacy_files_hash_as_before(tmp_path):
    """Existing ledgers must keep verifying: the new rule equals the old one without unchained runs."""
    import hashlib
    p = tmp_path / "j.jsonl"
    p.write_text('{"event": "legacy 1"}\n{"event": "legacy 2"}\n')
    ledger.append(p, {"event": "x"})
    ledger.append(p, {"event": "y"})
    lines = p.read_bytes().split(b"\n")
    assert json.loads(lines[2])["prev_sha256"] == hashlib.sha256(lines[1]).hexdigest()
    assert json.loads(lines[3])["prev_sha256"] == hashlib.sha256(lines[2]).hexdigest()


def test_a_long_unchained_run_beyond_the_tail_window(tmp_path):
    p = tmp_path / "j.jsonl"
    ledger.append(p, {"event": "start"})
    with open(p, "a") as f:
        for i in range(3000):
            f.write(json.dumps({"event": "old", "i": i, "pad": "x" * 40}) + "\n")
    ledger.append(p, {"event": "after"})
    assert ledger.verify_chain(p) == []
