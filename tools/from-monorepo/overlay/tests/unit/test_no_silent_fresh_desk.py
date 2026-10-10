"""No silent fresh desk (CRY-DEC-0001): a job runs only on a desk that has state, or one the owner has just made.

The difference from the repository this desk came from, where a missing `var/crypto` made the bar cycle create a
new book and the publisher send a healthy-looking snapshot of it."""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wt.core import desk as desks
from wt.core import ledger
from wt.ops import alerts as alerts_mod
from wt.ops import heartbeat, init, jobs, locks
from wt.ops.schedule import JOBS, Job

ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINTS = (("wt.crypto.cycle",), ("wt.crypto.snapshot", "--dry-run"), ("wt.crypto.challengers",),
                ("wt.crypto.learn",), ("wt.crypto.control", "unkill"), ("wt.crypto.control", "learning-off"))


@pytest.fixture
def desk(tmp_path):
    d = tmp_path / "var" / "crypto"
    return dataclasses.replace(desks.DESKS["crypto"], state_dir=d, kill_file=d / "KILL",
                               ledgers=(("crypto-harvest", d / "harvest" / "harvest_journal.jsonl"),
                                        ("crypto", d / "crypto_journal.jsonl")),
                               chain_flag=d / "chain-broken")


def existing(desk) -> None:
    """A desk that has run: its book and its journal are there."""
    desk.state_dir.mkdir(parents=True)
    (desk.state_dir / init.BOOK).write_text("{}")
    ledger.append(desk.journal, {"id": "c0", "kind": "cycle"})


# ---------------------------------------------------------------- the rule

def test_a_missing_state_directory_is_refused_and_the_words_say_what_to_do(desk):
    why = init.problem(desk)
    assert why and "has no state on this host" in why and "move its state directory here" in why
    assert "python -m wt.ops.init --new-desk" in why and not desk.state_dir.exists()       # asking made nothing


def test_a_desk_with_its_book_and_journal_may_run(desk):
    existing(desk)
    assert init.problem(desk) is None


@pytest.mark.parametrize("have, named", [((), "book.json and crypto_journal.jsonl"), (("book",), "crypto_journal.jsonl"),
                                         (("journal",), "book.json")])
def test_a_directory_alone_or_half_a_desk_is_not_state(desk, have, named):
    """A stray mkdir, a kill switch written before the move, or a move left half done must not become a new book."""
    desk.state_dir.mkdir(parents=True)
    desk.kill_file.write_text("paused before the move\n")
    if "book" in have:
        (desk.state_dir / init.BOOK).write_text("{}")
    if "journal" in have:
        ledger.append(desk.journal, {"id": "c0", "kind": "cycle"})
    why = init.problem(desk)
    assert why and "incomplete" in why and f"{named} missing" in why and "Nothing is created" in why


def test_init_makes_a_new_desk_with_its_kill_switch_on_and_never_touches_an_existing_one(desk, capsys):
    assert init.main(["--check"], desk) == init.REFUSED and "NOT READY" in capsys.readouterr().out
    assert init.main(["--new-desk"], desk) == 0
    said = capsys.readouterr().out
    assert "NEW crypto desk" in said and "kill switch ON" in said and "STOP" in said
    # What the first cycle of the origin made, word for word, plus the mark that it was made on purpose.
    day = dt.datetime.now(dt.UTC).date().isoformat()
    assert desk.kill_file.read_text() == f"created with the desk on {day}; the owner removes it (make unkill DESK=crypto)\n"
    assert sorted(p.name for p in desk.state_dir.iterdir()) == ["KILL", init.MARK]
    assert init.problem(desk) is None and init.main(["--check"], desk) == 0
    assert init.main(["--new-desk"], desk) == init.REFUSED                                 # a second time: refused
    assert "never touches an existing desk" in capsys.readouterr().err
    with pytest.raises(FileExistsError):
        init.create(desk)


def test_init_needs_one_of_its_two_flags(desk):
    with pytest.raises(SystemExit):
        init.main([], desk)
    with pytest.raises(SystemExit):
        init.main(["--check", "--new-desk"], desk)


def test_a_new_desk_stops_being_new_after_its_first_cycle_and_a_later_loss_is_refused(desk):
    init.create(desk)
    assert not init.settle(desk) and (desk.state_dir / init.MARK).exists()                 # no cycle has run yet
    (desk.state_dir / init.BOOK).write_text("{}")
    assert not init.settle(desk)                                                           # a book and no journal: not yet
    ledger.append(desk.journal, {"id": "c0", "kind": "cycle"})
    assert init.settle(desk) and not (desk.state_dir / init.MARK).exists()
    assert init.problem(desk) is None
    (desk.state_dir / init.BOOK).unlink()                                                  # the book is lost later
    assert "incomplete" in str(init.problem(desk))                                         # refused, not remade


# ---------------------------------------------------------------- the job runner

@pytest.fixture
def host(tmp_path, monkeypatch, desk):
    """A checkout in a temp dir whose interpreter is this one, with the runner's state redirected there."""
    root = tmp_path / "checkout"
    (root / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, root / ".venv" / "bin" / "python")
    monkeypatch.setattr(locks, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(heartbeat, "HEARTBEAT_DIR", tmp_path / "heartbeats")
    monkeypatch.setitem(desks.DESKS, "crypto", desk)
    box: list[dict] = []
    pings: list[tuple[str, str]] = []
    monkeypatch.setattr(alerts_mod, "_send", lambda msg, topic, server, timeout=5.0: box.append(msg) or True)
    monkeypatch.setattr(jobs.hc, "ping", lambda slug, signal="", body="", timeout=5.0: pings.append((slug, signal)) or True)
    marker = tmp_path / "the-child-ran"
    job = dataclasses.replace(JOBS["crypto"], command=("-c", f"open({str(marker)!r}, 'w').write('x')"), preflight=False)
    return root, job, alerts_mod.Alerts(root=tmp_path / "alerts", topic="t"), box, pings, marker


def test_every_job_refuses_without_state_starts_nothing_creates_nothing_and_says_so(host, desk, tmp_path):
    root, _, a, box, pings, marker = host
    for name in JOBS:
        job = dataclasses.replace(JOBS[name], command=("-c", f"open({str(marker)!r}, 'w').write('x')"), preflight=False)
        assert jobs.run_job(job, root, alerts=a) == 0                     # a refusal exits 0: systemd must not retry it
        last = heartbeat.last_runs(tmp_path / "heartbeats")[name]
        assert last["status"] == "refused" and "has no state on this host" in last["detail"]
    assert not marker.exists() and not desk.state_dir.exists()            # no child ran; nothing was made
    assert [m["title"] for m in box] == [f"{n} refused to run" for n in JOBS]             # one page per job
    assert "move its state directory here" in box[0]["message"]
    assert pings == [("wt-crypto", "fail"), ("wt-crypto-challengers", "fail"), ("wt-crypto-learn", "fail")]
    assert jobs.run_job(dataclasses.replace(JOBS["crypto"], preflight=False), root, alerts=a) == 0
    assert len(box) == len(JOBS)                                           # the same day: no second page


def test_a_job_runs_on_an_existing_desk_and_on_a_new_one_whose_mark_goes_after_the_first_cycle(host, desk, tmp_path):
    root, job, a, box, pings, marker = host
    init.create(desk)
    assert jobs.run_job(job, root, alerts=a) == 0 and marker.exists()
    assert heartbeat.last_runs(tmp_path / "heartbeats")["crypto"]["status"] == "ok" and pings == [("wt-crypto", "")]
    assert (desk.state_dir / init.MARK).exists()                           # the child wrote no book: still new
    (desk.state_dir / init.BOOK).write_text("{}")
    ledger.append(desk.journal, {"id": "c0", "kind": "cycle"})
    assert jobs.run_job(job, root, alerts=a) == 0
    assert not (desk.state_dir / init.MARK).exists() and init.problem(desk) is None and box == []
    # Only the bar cycle settles a new desk: another job finishing does not.
    other = tmp_path / "var2" / "crypto"
    d2 = dataclasses.replace(desk, state_dir=other, kill_file=other / "KILL", ledgers=(("crypto", other / "crypto_journal.jsonl"),))
    init.create(d2)
    (other / init.BOOK).write_text("{}")
    ledger.append(d2.journal, {"id": "c0", "kind": "cycle"})
    desks.DESKS["crypto"] = d2
    publish = dataclasses.replace(job, name="dashboard-crypto")
    assert jobs.run_job(publish, root, alerts=a) == 0 and (other / init.MARK).exists()


def test_a_failing_job_on_an_existing_desk_pages_and_fails_its_check(host, desk, tmp_path):
    root, job, a, box, pings, _ = host
    existing(desk)
    bad = dataclasses.replace(job, command=("-c", "import sys; sys.exit(3)"))
    assert jobs.run_job(bad, root, alerts=a) == 3
    assert heartbeat.last_runs(tmp_path / "heartbeats")["crypto"]["status"] == "failed"
    assert pings == [("wt-crypto", "fail")] and box[0]["title"] == "crypto failed"
    assert jobs.run_job(job, root, alerts=a) == 0 and box[-1]["title"] == "crypto recovered"


def test_status_says_whether_the_desk_has_state(host, desk, monkeypatch, capsys):
    root, *_ = host
    monkeypatch.setattr(jobs.preflight, "run_checks", lambda root=None: [])
    monkeypatch.setattr(jobs, "Alerts", lambda: type("A", (), {"firing": lambda self: {}})())
    assert jobs.status(root) == 0 and "Desk state: NOT READY" in capsys.readouterr().out
    existing(desk)
    assert jobs.status(root) == 0
    out = capsys.readouterr().out
    assert f"Desk state: ok ({desk.state_dir})" in out and "KILL switch: off" in out


# ---------------------------------------------------------------- every entry point, as the units start them

@pytest.mark.parametrize("argv", ENTRY_POINTS, ids=[" ".join(a) for a in ENTRY_POINTS])
def test_each_entry_point_refuses_without_state_before_it_does_anything(tmp_path, argv):
    """`python -m <module>` with no state: exit 2, one clear line, nothing created, and it never got as far as the
    network (the venue's host does not resolve here, and the refusal comes back at once)."""
    state = tmp_path / "var"
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "WT_STATE": str(state), "HOME": str(tmp_path), "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}
    r = subprocess.run([sys.executable, "-m", *argv], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == init.REFUSED, r.stderr[-400:]
    assert r.stderr.startswith("Refusing: the crypto desk has no state on this host")
    assert not (state / "crypto").exists() and not (state / "dashboard-crypto").exists()
    assert not any(p.name in ("crypto", "alerts", "heartbeats") for p in state.glob("*"))


def test_an_entry_point_refuses_a_directory_that_holds_only_a_kill_switch(tmp_path):
    state = tmp_path / "var"
    (state / "crypto").mkdir(parents=True)
    (state / "crypto" / "KILL").write_text("paused\n")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "WT_STATE": str(state), "HOME": str(tmp_path), "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}
    r = subprocess.run([sys.executable, "-m", "wt.crypto.cycle"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == init.REFUSED and "incomplete" in r.stderr
    assert sorted(p.name for p in (state / "crypto").iterdir()) == ["KILL"]                # still only the switch


def test_the_cycle_itself_never_makes_a_state_directory(desk):
    """The library call the entry point wraps: with no directory it returns the refusal and makes nothing."""
    from wt.crypto import cycle
    from wt.crypto.data import KrakenPublic

    def no_network(url, params):
        raise AssertionError("the venue was asked before the state was checked")
    assert cycle.run(now=1_791_072_910.0, api=KrakenPublic(get=no_network, min_interval_s=0), desk=desk,
                     alerts=alerts_mod.Alerts(root=desk.state_dir.parent / "alerts", topic=None)) == init.REFUSED
    assert not desk.state_dir.exists()


def test_the_makefile_kill_target_does_not_make_a_directory():
    text = (ROOT / "Makefile").read_text()
    kill = text[text.index("\nkill:"):text.index("\nunkill:")]
    assert "mkdir" not in kill and "wt.ops.init --check" in kill


def test_job_is_a_known_shape():
    assert isinstance(JOBS["crypto"], Job)
