#!/usr/bin/env python3
"""What changed in the monorepo since this repository's copies were made, and what a re-extraction has to redo.

    tools/from-monorepo/drift.py <monorepo clone> <commit>            report; exit 1 when anything needs a person
    tools/from-monorepo/drift.py <monorepo clone> <commit> --write    also redo what is mechanical (see below)

The files that move whole (paths.txt) need nothing redone: `extract.sh` takes them from the commit it is given.
The report lists the ones that changed since this repository was extracted (SOURCE.json), because only a new
extraction brings them here; they do not change the exit code. Everything else is looked at for what a person
has to do:

  copies    every file under overlay/ that was made from a monorepo file (copies.json): `verbatim` (the same
            bytes), `trimmed` (cut down by hand) or `split` (some top-level keys of a config file, byte for
            byte; under a kept key, `under` names the second-level keys that are kept and the ones that are
            dropped). Reported: the source changed since the copy was made; a verbatim copy that was edited here;
            a split that no longer equals what its rule gives.
  patches   every patch under patches/ (a deliberate change to a file that moves whole): does it still apply to
            the files at <commit>?
  allowlist entries of paths.txt that match nothing at <commit>, and files at <commit> that look like the
            desk's (they import wt.crypto or wt.ml, or are named for crypto, Kraken or ML) and are neither
            listed in paths.txt nor acknowledged in stays.txt.
  pins      the hashes of the frozen configuration at <commit> against the ones the tests here are pinned to
            (overlay/tests/fixtures/monorepo_pins.json). Needs PyYAML: run it with the repository's .venv.

--write redoes the mechanical part in overlay/ and copies.json: verbatim copies are taken again from <commit>,
splits are cut again, the pins are computed again, and their records are moved to <commit>. A `trimmed` copy and
a patch are never rewritten: they are listed for a person to redo, and a trimmed copy stays reported until that
person says it is done: `--record <path>` moves its record in copies.json to <commit>.

Standard library only (PyYAML for the pins). Reads the monorepo; never writes to it.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERLAY = HERE / "overlay"
COPIES = HERE / "copies.json"
PATHS = HERE / "paths.txt"
STAYS = HERE / "stays.txt"
PATCHES = HERE / "patches"
SOURCE = HERE / "SOURCE.json"
PINS =OVERLAY / "tests" / "fixtures" / "monorepo_pins.json"
LOOKS_LIKE = re.compile(r"(^|[/_.-])(crypto|kraken|ml)([/_.-]|$)", re.I)
IMPORTS = re.compile(rb"\bwt\.(crypto|ml)\b")
SEARCHED = ("src/", "scripts/", "tests/", "config/", "research/", "docs/", "deploy/")
RESEARCH_WORDS = re.compile(rb"crypto|kraken|coinbase|bitcoin", re.I)


def git(repo: Path, *args: str, check: bool = True) -> bytes:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args[:3])} failed: {r.stderr.decode(errors='replace').strip()[:300]}")
    return r.stdout


def blob_of(repo: Path, commit: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{commit}:{path}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def show(repo: Path, commit: str, path: str) -> bytes:
    return git(repo, "show", f"{commit}:{path}")


def hash_object(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---- splits: some top-level keys of a YAML file, byte for byte ----

def segments(text: str) -> list[tuple[str | None, list[str]]]:
    """The file cut at its top-level keys. A key's segment starts at the comment lines directly above it and runs
    to the next key's. Lines before the first key's comments are a segment with no key."""
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z_][\w-]*):", line)
        if m:
            j = i
            while j > 0 and lines[j - 1].startswith("#"):
                j -= 1
            starts.append((j, m.group(1)))
    out: list[tuple[str | None, list[str]]] = []
    if starts and starts[0][0] > 0:
        out.append((None, lines[:starts[0][0]]))
    for k, (j, key) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        out.append((key, lines[j:end]))
    return out


def under(key: str, lines: list[str], rule: dict) -> list[str]:
    """A kept top-level key's segment cut at its second-level keys (two spaces in): the ones `rule["keep"]` names
    stay exactly as written, the ones `rule["drop"]` names go, each with the comment lines directly above it. A
    second-level key named in neither stops the split, as a top-level one does, and so does a kept one that is
    not there. The lines before the first second-level key (the key's own line and its comments) stay."""
    keep, drop = list(rule["keep"]), list(rule.get("drop", []))
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^  ([A-Za-z_][\w-]*):", line)
        if m:
            j = i
            while j > 1 and lines[j - 1].startswith("  #"):
                j -= 1
            starts.append((j, m.group(1)))
    found = [name for _, name in starts]
    for name in found:
        if name not in keep and name not in drop:
            raise ValueError(f"{key} holds {found}: {name!r} is in neither `keep` nor `drop` of `under.{key}`")
    if [name for name in found if name in keep] != keep:
        raise ValueError(f"{key} holds {found}, expected the kept {keep} in that order")
    out = lines[:starts[0][0]] if starts else list(lines)
    for k, (j, name) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        if name in keep:
            out += lines[j:end]
    return out


def split(text: str, rule: dict) -> str:
    """The copy a `split` rule gives: the rule's header, then the kept keys' segments exactly as written. A key
    the rule names neither in `keep` nor in `drop` stops the split: somebody has to say whose it is. The same
    holds one level down for a kept key that `under` has a rule for (see `under`)."""
    keep, drop = list(rule["keep"]), list(rule.get("drop", []))
    kept: list[str] = []
    for key, lines in segments(text):
        if key is None or key in drop:
            continue
        if key not in keep:
            raise ValueError(f"top-level key {key!r} is in neither `keep` nor `drop`")
        if key in rule.get("under", {}):
            lines = under(key, lines, rule["under"][key])
        kept += lines
    body = "".join(kept)
    return "".join(h + "\n" for h in rule.get("header", [])) + body


# ---- the pins: hashes of the frozen configuration ----

def canon(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def host_from(repo: Path, commit: str) -> dict:
    """What the monorepo puts on the shared host at this commit, by name: the job units its schedule renders
    (wt-<job>.service and .timer), its static units and its helpers. This repository's names must share none."""
    jobs = re.findall(r'^    Job\("([a-z0-9-]+)"', show(repo, commit, "src/wt/ops/schedule.py").decode(), re.M)
    def names(folder: str) -> list[str]:
        return sorted(Path(x).name for x in git(repo, "ls-tree", "--name-only", commit, folder + "/").decode().splitlines())
    return {"jobs": jobs, "crypto_jobs": [j for j in jobs if "crypto" in j],
            "job_units": sorted(f"wt-{j}.{kind}" for j in jobs for kind in ("service", "timer")),
            "static_units": names("deploy/oci/systemd"), "helpers": names("deploy/oci/bin"),
            "install_root": "/home/wt/trading", "etc_dir": "/etc/wt"}


def pins_from(crypto_yaml: bytes, risk_yaml: bytes, registry_yaml: bytes, commit: str, host: dict | None = None) -> dict:
    """Every hash the tests pin, worked out here from the files' bytes and nothing of this repository's code."""
    import yaml
    cfg, risk, reg = yaml.safe_load(crypto_yaml), yaml.safe_load(risk_yaml), yaml.safe_load(registry_yaml)
    frozen = {k: cfg[k] for k in ("pairs", "timeframe_min", "bars", "strategy", "costs", "quality")}
    sleeves = {n: canon({"common": cfg["sleeves"]["common"], "rule": cfg["sleeves"][n], "costs": cfg["costs"],
                         "quality": cfg["quality"]})[:12]
               for n in cfg["sleeves"] if n != "common"}
    crypto_keys = sorted(k for k in risk if k != "B")
    family = reg["families"]["C"]
    accepted = [r for r in family if r.get("status") == "accepted"]
    return {
        "source": {"repo": "Algorythmos-AI/trading-lab-research", "commit": commit},
        "crypto_yaml_sha256": hashlib.sha256(crypto_yaml).hexdigest(),
        "sections": {k: canon(v) for k, v in sorted(cfg.items())},
        "config_hash": canon(frozen)[:12],
        "sleeve_hashes": sleeves,
        "harvest_explore_hash": canon(cfg["harvest"]["explore"])[:12] if (cfg.get("harvest") or {}).get("explore") else None,
        "risk_keys": {k: canon(risk[k]) for k in crypto_keys},
        "trial_family_C": {"rows": canon(family), "in_force": accepted[-1]["total"] if accepted else None},
        **({"host": host} if host is not None else {}),
    }


# ---- the report ----

def allowlist() -> list[str]:
    return [x.strip() for x in PATHS.read_text().splitlines() if x.strip() and not x.lstrip().startswith("#")]


def listed(path: str, entries: list[str]) -> bool:
    for e in entries:
        if e.startswith("glob:"):
            if fnmatch.fnmatchcase(path, e[5:]):
                return True
        elif path == e or (e.endswith("/") and path.startswith(e)):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="drift.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("monorepo", type=Path)
    ap.add_argument("commit")
    ap.add_argument("--write", action="store_true", help="redo the mechanical copies, splits and pins from <commit>")
    ap.add_argument("--record", action="append", default=[], metavar="PATH",
                    help="a trimmed copy was redone by hand against <commit>: move its record there (repeatable)")
    a = ap.parse_args(argv)
    repo = a.monorepo.resolve()
    commit = git(repo, "rev-parse", "--verify", f"{a.commit}^{{commit}}").decode().strip()
    manifest = json.loads(COPIES.read_text())
    todo: list[str] = []
    redone: list[str] = []
    print(f"monorepo {repo} at {commit[:12]}")

    print("\n== copies (overlay files made from monorepo files) ==")
    for c in manifest["copies"]:
        path, src, mode = c["path"], c["from"], c["mode"]
        now = blob_of(repo, commit, src)
        mine = OVERLAY / path
        notes = []
        if path in a.record and now is not None:
            c.update(commit=commit, blob=now)
            redone.append(path)
        was = blob_of(repo, c["commit"], src)           # after --record: the record just moved is the one checked
        if was != c["blob"]:
            notes.append(f"RECORD WRONG: {src} at {c['commit'][:12]} is {str(was)[:12]}, copies.json says {c['blob'][:12]}")
        if now is None:
            notes.append("SOURCE GONE at this commit")
        elif now != c["blob"]:
            stat = git(repo, "diff", "--numstat", c["blob"], now, check=False).decode().split()
            notes.append(f"SOURCE CHANGED since {c['commit'][:12]}" + (f" (+{stat[0]} -{stat[1]} lines)" if len(stat) >= 2 else ""))
        if not mine.exists():
            notes.append("MISSING from overlay/")
        elif mode == "verbatim" and hash_object(mine.read_bytes()) != c["blob"]:
            notes.append("EDITED HERE: no longer the recorded bytes (make it `trimmed`, or take the source again)")
        if mode == "split" and now is not None:
            try:
                want = split(show(repo, commit, src).decode(), c["rule"])
                if not mine.exists() or mine.read_text() != want:
                    notes.append("SPLIT DIFFERS from what its rule gives at this commit")
            except ValueError as e:
                want = None
                notes.append(f"SPLIT STOPPED: {e}")
        if a.write and now is not None and mode in ("verbatim", "split") and notes and "SPLIT STOPPED" not in " ".join(notes):
            data = show(repo, commit, src) if mode == "verbatim" else (want or "").encode()
            mine.parent.mkdir(parents=True, exist_ok=True)
            mine.write_bytes(data)
            c.update(commit=commit, blob=now)
            redone.append(path)
            notes = [f"redone from {commit[:12]}"]
        elif notes:
            todo.append(f"{path}: " + "; ".join(notes))
        print(f"  {mode:8} {path:44} " + ("same" if not notes else "; ".join(notes)))
        if mode == "trimmed" and notes and now is not None and now != c["blob"]:
            print(f"           what changed upstream: git -C {repo} diff {c['blob'][:12]} {now[:12]}")

    print("\n== patches (deliberate changes to files that move whole) ==")
    for p in sorted(PATCHES.glob("*.patch")):
        targets = sorted(set(re.findall(r"^\+\+\+ b/(\S+)", p.read_text(), re.M)))
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            ok = True
            for t in targets:
                if blob_of(repo, commit, t) is None:
                    ok = False
                    continue
                f = Path(tmp) / t
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(show(repo, commit, t))
            r = subprocess.run(["git", "-C", tmp, "apply", "--check", str(p)], capture_output=True, text=True)
            ok = ok and r.returncode == 0
        first = (r.stderr.strip().splitlines() or [""])[0][:120]
        print(f"  {p.name:44} " + ("applies" if ok else f"DOES NOT APPLY ({first or 'a target file is gone'})"))
        if not ok:
            todo.append(f"patches/{p.name}: no longer applies at {commit[:12]}; remake it against the new file")

    print("\n== allowlist (paths.txt) ==")
    entries = allowlist()
    stays = [x.strip() for x in STAYS.read_text().splitlines() if x.strip() and not x.lstrip().startswith("#")] if STAYS.exists() else []
    files = git(repo, "ls-tree", "-r", "--name-only", commit).decode().splitlines()
    for e in entries:
        if not any(listed(f, [e]) for f in files):
            print(f"  NOTHING MATCHES {e}")
            todo.append(f"paths.txt: {e} matches nothing at {commit[:12]}")
    stays += [c["from"] for c in manifest["copies"]]
    moved = [f for f in files if listed(f, entries)]
    print(f"  {len(moved)} files move whole at this commit ({len(entries)} entries)")
    unknown = []
    for f in files:
        if listed(f, entries) or listed(f, stays) or not f.startswith(SEARCHED):
            continue
        if f.startswith("research/"):
            hit = bool(RESEARCH_WORDS.search(show(repo, commit, f)))
        else:
            hit = bool(LOOKS_LIKE.search(f)) or (f.endswith((".py", ".sh", ".yaml", ".yml", ".toml")) and bool(IMPORTS.search(show(repo, commit, f))))
        if hit:
            unknown.append(f)
    for f in unknown:
        print(f"  NOT LISTED, NOT ACKNOWLEDGED: {f}")
        todo.append(f"{f}: looks like the desk's; add it to paths.txt (it moves) or to stays.txt (it stays, and why)")
    if not unknown:
        print(f"  every other file that looks like the desk's is acknowledged in stays.txt ({len(stays)} entries)")

    print("\n== moved whole: what this repository does not have yet ==")
    extracted = json.loads(SOURCE.read_text())["commit"] if SOURCE.exists() else None
    if extracted is None:
        print("  not compared: no SOURCE.json here")
    elif extracted == commit:
        print(f"  none: {commit[:12]} is the commit this repository was extracted from")
    else:
        r = subprocess.run(["git", "-C", str(repo), "diff", "--name-status", "--no-renames", extracted, commit],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  not compared: the clone does not have {extracted[:12]}, the commit this repository was extracted from")
        else:
            newer = [(s, p) for s, p in (line.split("\t", 1) for line in r.stdout.splitlines()) if listed(p, entries)]
            for s, p in newer:
                print(f"  {s}  {p}")
            print(f"  {len(newer)} file(s) that move whole changed between {extracted[:12]} and {commit[:12]}"
                  + (": nothing to redo by hand, but only a new extraction brings them here" if newer else ""))

    print("\n== pins (the frozen configuration's hashes) ==")
    try:
        now_pins = pins_from(show(repo, commit, "config/crypto.yaml"), show(repo, commit, "config/risk.yaml"),
                             show(repo, commit, "config/trial_registry.yaml"), commit, host_from(repo, commit))
    except ImportError:
        now_pins = None
        print("  skipped: PyYAML is not importable here (run this with the repository's .venv/bin/python)")
        todo.append("pins: not checked (no PyYAML)")
    if now_pins is not None:
        had = json.loads(PINS.read_text()) if PINS.exists() else {}
        same = {k: v for k, v in had.items() if k != "source"} == {k: v for k, v in now_pins.items() if k != "source"}
        if same:
            print(f"  same as the pins recorded at {str((had.get('source') or {}).get('commit'))[:12]}")
        else:
            diff = sorted(k for k in now_pins if k != "source" and had.get(k) != now_pins[k])
            print(f"  DIFFERENT from the recorded pins in: {', '.join(diff)}")
            print("  A change to the frozen configuration is a new trial under the pre-registration rule: read the "
                  "monorepo's decision record for it before taking it.")
            if a.write:
                PINS.parent.mkdir(parents=True, exist_ok=True)
                PINS.write_text(json.dumps(now_pins, indent=1, sort_keys=True) + "\n")
                redone.append(str(PINS.relative_to(OVERLAY)))
            else:
                todo.append("pins: the frozen configuration changed in the monorepo: " + ", ".join(diff))
        if a.write and same and (had.get("source") or {}).get("commit") != commit:
            # The same values, seen at a newer commit: the pins now say they were checked there.
            PINS.write_text(json.dumps(now_pins, indent=1, sort_keys=True) + "\n")
            redone.append(str(PINS.relative_to(OVERLAY)) + " (same values; source commit moved)")

    if redone:
        COPIES.write_text(json.dumps(manifest, indent=1) + "\n")
        print(f"\nredone from {commit[:12]}: " + ", ".join(redone))
    print()
    if todo:
        print(f"{len(todo)} thing(s) for a person:")
        for t in todo:
            print(f"  - {t}")
        return 1
    print("no drift: a re-extraction at this commit needs nothing redone by hand")
    return 0


if __name__ == "__main__":
    sys.exit(main())
