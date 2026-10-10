#!/usr/bin/env python3
"""Keep the files made from overlay/ and overlay/ itself the same.

    tools/from-monorepo/overlay.py check          exit 1 when a root file differs from its overlay source, when
                                                  an overlay file is missing at the root, or when a tracked file
                                                  is accounted for by nothing (see below)
    tools/from-monorepo/overlay.py pull           after editing files at the root: copy them back into overlay/
    tools/from-monorepo/overlay.py pull --add P   also take a new root file P into overlay/

`extract.sh` builds this repository from three things: the files that move whole from the monorepo (paths.txt),
the overlay, and this directory. So every tracked file must be one of those, or the next extraction loses it.
Until the desk's code has left the monorepo, a new file is added to overlay/ as well as at the root.

Standard library only.
"""
from __future__ import annotations

import fnmatch
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OVERLAY = HERE / "overlay"
TOOLS = HERE.relative_to(ROOT).as_posix() + "/"


def overlay_files() -> list[str]:
    return sorted(p.relative_to(OVERLAY).as_posix() for p in OVERLAY.rglob("*") if p.is_file())


def allowlist() -> list[str]:
    return [x.strip() for x in (HERE / "paths.txt").read_text().splitlines()
            if x.strip() and not x.lstrip().startswith("#")]


def moved(path: str, entries: list[str]) -> bool:
    for e in entries:
        if e.startswith("glob:"):
            if fnmatch.fnmatchcase(path, e[5:]):
                return True
        elif path == e or (e.endswith("/") and path.startswith(e)):
            return True
    return False


def same(a: Path, b: Path) -> bool:
    return a.read_bytes() == b.read_bytes() and (a.stat().st_mode & 0o111) == (b.stat().st_mode & 0o111)


def tracked() -> list[str] | None:
    r = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else None


def check() -> int:
    bad = []
    mine = overlay_files()
    for rel in mine:
        root = ROOT / rel
        if not root.exists():
            bad.append(f"missing at the root: {rel}")
        elif not same(OVERLAY / rel, root):
            bad.append(f"differs from overlay/: {rel}")
    entries = allowlist()
    for rel in mine:
        if moved(rel, entries):
            bad.append(f"overlay/ holds a path that paths.txt moves whole: {rel}")
    files = tracked()
    if files is not None:
        known = set(mine)
        for f in files:
            if f in known or f.startswith(TOOLS) or moved(f, entries):
                continue
            bad.append(f"tracked, but neither moved (paths.txt), nor in overlay/, nor under {TOOLS}: {f}")
    if bad:
        print("\n".join(bad))
        print(f"\n{len(bad)} problem(s). After editing a file at the root: tools/from-monorepo/overlay.py pull")
        return 1
    print(f"overlay: {len(mine)} files, each the same at the root; every tracked file is accounted for")
    return 0


def pull(add: list[str]) -> int:
    n = 0
    for rel in [*overlay_files(), *add]:
        root = ROOT / rel
        if not root.is_file():
            print(f"skipped (not a file at the root): {rel}")
            continue
        dst = OVERLAY / rel
        if dst.exists() and same(dst, root):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root, dst)
        print(f"pulled {rel}")
        n += 1
    print(f"{n} file(s) copied into overlay/")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["check"] and len(argv) == 1:
        return check()
    if argv[:1] == ["pull"]:
        rest, add = argv[1:], []
        while rest[:1] == ["--add"] and len(rest) >= 2:
            add.append(rest[1])
            rest = rest[2:]
        if not rest:
            return pull(add)
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
