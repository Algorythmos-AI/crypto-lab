"""Owner controls for the crypto desk: remove its kill switch, clear its loss latch, switch learning off and on.

    python -m wt.crypto.control unkill          (make unkill DESK=crypto)
    python -m wt.crypto.control reset-latch     (make reset-crypto-latch)
    python -m wt.crypto.control learning-off    (make crypto-learning-off)
    python -m wt.crypto.control learning-on     (make crypto-learning-on)

All refuse while a bar cycle is running, so a control never changes under a decision in flight, and both leave a
line in the desk's journal: who changed a control, and when, is evidence too.

The learning switch (DEC-0016, 6) is one file. While it exists no model acts, no challenger is drawn, tested or
admitted, and live challengers open nothing. The baseline and the three registered sleeves trade on, and no exit
is ever stopped by it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid

from wt.core import ledger
from wt.core.desk import DESKS, Desk
from wt.crypto.risk import latch_file, learning_file
from wt.ops.locks import job_lock

JOB = "crypto"


def _learning(kind: str, desk: Desk) -> int:
    target, off = learning_file(desk), kind == "learning-off"
    with job_lock(JOB) as free:
        if not free:
            print("Refusing: a crypto bar cycle is running; try again in a few seconds")
            return 2
        if target.exists() == off:
            print(f"Crypto learning is already {'off' if off else 'on'}")
            return 0
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        if off:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"switched off {now}\n")
        else:
            target.unlink()
        ledger.append(desk.journal, {"id": uuid.uuid4().hex, "kind": "control", "action": kind, "t": now}, fsync=True)
    print("Crypto learning switched off: no challenger is drawn or admitted, live challengers open nothing. "
          "Exits are still managed." if off else "Crypto learning switched on")
    return 0


def _apply(kind: str, desk: Desk) -> int:
    if kind.startswith("learning-"):
        return _learning(kind, desk)
    target = desk.kill_file if kind == "unkill" else latch_file(desk)
    what = "kill switch" if kind == "unkill" else "loss latch"
    if kind == "unkill" and desk.chain_flag.exists():
        print("Refusing: the crypto journal's hash chain is flagged broken; clear that first (see the runbook)")
        return 2
    with job_lock(JOB) as free:
        if not free:
            print("Refusing: a crypto bar cycle is running; try again in a few seconds")
            return 2
        # The tournament sleeves keep a latch each (DEC-0015); "reset the latch" clears those too.
        more = sorted((desk.state_dir / "sleeves").glob("*/latch")) if kind == "reset-latch" else []
        if not target.exists() and not more:
            print(f"The crypto {what} is already off")
            return 0
        note = "; ".join(f.read_text(errors="replace").strip() for f in [*([target] if target.exists() else []), *more])[:200]
        for f in [target, *more]:
            f.unlink(missing_ok=True)
        ledger.append(desk.journal, {"id": uuid.uuid4().hex, "kind": "control", "action": kind,
                                     "t": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "was": note},
                      fsync=True)
    print(f"Crypto {what} removed")
    return 0


def main(argv: list[str] | None = None, desk: Desk | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m wt.crypto.control")
    ap.add_argument("action", choices=("unkill", "reset-latch", "learning-off", "learning-on"))
    return _apply(ap.parse_args(argv).action, desk or DESKS["crypto"])


if __name__ == "__main__":
    sys.exit(main())
