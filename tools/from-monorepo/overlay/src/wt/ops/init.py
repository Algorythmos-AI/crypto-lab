"""No silent fresh desk (CRY-DEC-0001): a job runs only on a desk that has state, or on one the owner has just made.

    python -m wt.ops.init --check         say whether the desk may run, and why not (exit 0 or 2); changes nothing
    python -m wt.ops.init --new-desk      make a NEW desk: a fresh paper book, its kill switch on, no history

Inside the monorepo a missing `var/crypto` made the bar cycle create a new book and the publisher send a
healthy-looking snapshot of it. Here that is never automatic: every job, the job runner and the owner's controls
ask `problem()` first and refuse with one clear line when the desk has no state.

A desk may run when either holds:
  * it has its book and its journal (`book.json` and the desk's journal file): the desk that has been running,
    which after the cutover is the one whose state directory was moved here;
  * it carries the mark `init --new-desk` leaves (`NEW_DESK`): a desk made on purpose and not yet run. The job
    runner removes the mark after the first bar cycle that leaves a book and a journal behind, so from then on
    only the first rule applies and a later loss of the state is refused like any other.

A state directory alone is not state: a stray `mkdir`, a kill switch written before the move, or a move into an
existing directory must not turn into a new book.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from wt.core.desk import DESKS, Desk

MARK = "NEW_DESK"
BOOK = "book.json"
REFUSED = 2


def problem(desk: Desk | None = None) -> str | None:
    """Why the desk may not run, or None when it may."""
    d = desk or DESKS["crypto"]
    where = d.state_dir
    if not where.is_dir():
        return (f"the {d.name} desk has no state on this host ({where} does not exist). To continue an existing "
                "desk, move its state directory here (docs/runbooks/cutover-from-monorepo.md). To start a new "
                "one on purpose: python -m wt.ops.init --new-desk")
    if (where / MARK).exists():
        return None
    book, journal = (where / BOOK).exists(), d.journal.exists()
    if book and journal:
        return None
    missing = " and ".join(n for n, there in ((BOOK, book), (d.journal.name, journal)) if not there)
    return (f"the {d.name} desk's state in {where} is incomplete: {missing} missing. Nothing is created to fill "
            "the gap. Find the missing state (the last backup, or the directory it was moved from) before any "
            "job runs")


def guard(desk: Desk | None = None) -> int:
    """For an entry point: 0 when the desk may run; otherwise say why on stderr and return the refusal's exit code."""
    why = problem(desk)
    if why is None:
        return 0
    print(f"Refusing: {why}", file=sys.stderr)
    return REFUSED


def create(desk: Desk | None = None, now: dt.datetime | None = None) -> None:
    """Make a new desk: the state directory, its kill switch on, and the mark that says it was made on purpose.
    Raises FileExistsError when anything is already there: this never touches an existing desk."""
    d = desk or DESKS["crypto"]
    day = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).date().isoformat()
    if d.state_dir.exists():
        raise FileExistsError(str(d.state_dir))
    d.state_dir.mkdir(parents=True)
    # The kill switch's words are the ones the first bar cycle wrote when it made the desk itself.
    d.kill_file.write_text(f"created with the desk on {day}; the owner removes it (make unkill DESK=crypto)\n")
    (d.state_dir / MARK).write_text(f"made by `python -m wt.ops.init --new-desk` on {day}; removed after the "
                                    "first bar cycle\n")


def settle(desk: Desk | None = None) -> bool:
    """After a bar cycle: a new desk that now has its book and its journal stops being new. True when the mark
    was removed by this call."""
    d = desk or DESKS["crypto"]
    mark = d.state_dir / MARK
    if mark.exists() and (d.state_dir / BOOK).exists() and d.journal.exists():
        mark.unlink()
        return True
    return False


def main(argv: list[str] | None = None, desk: Desk | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.ops.init")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--check", action="store_true", help="say whether the desk may run; change nothing")
    what.add_argument("--new-desk", action="store_true", help="make a NEW desk with a fresh book and no history")
    a = ap.parse_args(argv)
    d = desk or DESKS["crypto"]
    if a.check:
        why = problem(d)
        print(f"the {d.name} desk has state in {d.state_dir}: jobs may run" if why is None else f"NOT READY: {why}")
        return 0 if why is None else REFUSED
    try:
        create(d)
    except FileExistsError:
        print(f"Refusing: {d.state_dir} already exists. `--new-desk` never touches an existing desk. If that "
              "directory is not a desk, look at what is in it and remove it yourself first.", file=sys.stderr)
        return REFUSED
    print(f"Made a NEW {d.name} desk in {d.state_dir}: a fresh paper book, no history, kill switch ON.\n"
          "If you meant to continue an existing desk, STOP: remove this directory and move the existing state "
          "directory here instead (docs/runbooks/cutover-from-monorepo.md).\n"
          "The desk records and publishes from its next bar cycle and opens nothing until the owner removes the "
          "kill switch (make unkill).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
