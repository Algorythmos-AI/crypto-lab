#!/usr/bin/env python3
"""Compare two replays (tools/parity/replay.py), byte for byte.

    compare.py <out of side A> <out of side B>

Everything a replay leaves is compared: every file of the state directory (journals, books, bars, observations,
the sleeves' and the harvest's state, alerts, heartbeats, the publisher's outbox), every snapshot body, the
state after the first cycle, and the summary. Exit 0 only when nothing differs. Standard library only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def first_difference(a: Path, b: Path) -> str:
    try:
        la, lb = a.read_text().splitlines(), b.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return "binary or unreadable"
    for i, (x, y) in enumerate(zip(la, lb, strict=False), 1):
        if x != y:
            try:
                dx, dy = json.loads(x), json.loads(y)
                keys = sorted(k for k in set(dx) | set(dy) if dx.get(k) != dy.get(k)) if isinstance(dx, dict) and isinstance(dy, dict) else []
            except ValueError:
                keys = []
            return f"line {i}" + (f", keys {keys}" if keys else f": {x[:80]!r} / {y[:80]!r}")
    return f"length: {len(la)} lines / {len(lb)} lines"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    a, b = Path(argv[0]), Path(argv[1])
    ma, mb = json.loads((a / "manifest.json").read_text()), json.loads((b / "manifest.json").read_text())
    bad = 0
    for part, sub in (("state", "var"), ("snapshots", "snapshots")):
        fa, fb = ma[part], mb[part]
        only_a, only_b = sorted(set(fa) - set(fb)), sorted(set(fb) - set(fa))
        differ = [k for k in sorted(set(fa) & set(fb)) if fa[k] != fb[k]]
        size = sum(v["bytes"] for v in fa.values())
        print(f"  {part}: {len(fa)} files, {size} bytes; only in A {len(only_a)}, only in B {len(only_b)}, different {len(differ)}")
        for k in only_a[:10]:
            print(f"    only in A: {k}")
        for k in only_b[:10]:
            print(f"    only in B: {k}")
        for k in differ[:20]:
            print(f"    DIFFERENT {k}: {first_difference(a / sub / k, b / sub / k)}")
        bad += len(only_a) + len(only_b) + len(differ)
    for name in ("after-first-cycle.json", "summary.json"):
        same = (a / name).read_bytes() == (b / name).read_bytes()
        print(f"  {name}: {'identical' if same else 'DIFFERENT'}")
        bad += 0 if same else 1
    s = json.loads((a / "summary.json").read_text())
    kinds = ", ".join(f"{k} {v}" for k, v in s["journal_kinds"].items())
    print(f"  what the replay did: {s['cycles']} bar cycles, {s['runs']['challengers']} challengers runs, {s['runs']['learn']} learning "
          f"runs, {s['runs']['control']} owner controls, {s['snapshots']} snapshots, {s['venue_calls']} venue calls")
    print(f"  journal: {s['journal_rows']} rows ({kinds}); chain breaks {s['journal_chain_breaks']}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
