"""Owner controls for the crypto desk: remove its kill switch, clear its loss latch.

    python -m wt.crypto.control unkill          (make unkill DESK=crypto)
    python -m wt.crypto.control reset-latch     (make reset-crypto-latch)

Both refuse while a bar cycle is running, so a control never changes under a decision in flight, and both leave a
line in the desk's journal: who removed a safety control, and when, is evidence too.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid

from wt.core import ledger
from wt.core.desk import DESKS, Desk
from wt.crypto.risk import latch_file
from wt.ops.locks import job_lock

JOB = "crypto"


def _apply(kind: str, desk: Desk) -> int:
    target = desk.kill_file if kind == "unkill" else latch_file(desk)
    what = "kill switch" if kind == "unkill" else "loss latch"
    if kind == "unkill" and desk.chain_flag.exists():
        print("Refusing: the crypto journal's hash chain is flagged broken; clear that first (see the runbook)")
        return 2
    with job_lock(JOB) as free:
        if not free:
            print("Refusing: a crypto bar cycle is running; try again in a few seconds")
            return 2
        if not target.exists():
            print(f"The crypto {what} is already off")
            return 0
        note = target.read_text(errors="replace").strip()[:200]
        target.unlink()
        ledger.append(desk.journal, {"id": uuid.uuid4().hex, "kind": "control", "action": kind,
                                     "t": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "was": note},
                      fsync=True)
    print(f"Crypto {what} removed")
    return 0


def main(argv: list[str] | None = None, desk: Desk | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.crypto.control")
    ap.add_argument("action", choices=("unkill", "reset-latch"))
    return _apply(ap.parse_args(argv).action, desk or DESKS["crypto"])


if __name__ == "__main__":
    sys.exit(main())
