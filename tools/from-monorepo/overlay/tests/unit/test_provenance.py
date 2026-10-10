"""Where every file came from (tools/from-monorepo): the repository can be built again from the monorepo.

Three kinds of file, and nothing else: moved whole (paths.txt), made from the overlay, or the extraction tooling
itself. Every overlay file made from a monorepo file is recorded with its origin, and says so itself when it was
trimmed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools" / "from-monorepo"
OVERLAY = TOOLS / "overlay"
COPIES = json.loads((TOOLS / "copies.json").read_text())["copies"]
SOURCE = json.loads((TOOLS / "SOURCE.json").read_text())


def tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_every_file_made_from_the_overlay_is_still_what_the_overlay_holds():
    r = subprocess.run([sys.executable, str(TOOLS / "overlay.py"), "check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-1500:]
    assert "every tracked file is accounted for" in r.stdout


def test_the_source_record_names_one_commit_of_the_monorepo():
    assert SOURCE["repo"] == "Algorythmos-AI/trading-lab-research" and len(SOURCE["commit"]) == 40
    assert SOURCE["filtered_commits"] > 0 and SOURCE["files_moved_whole"] > 0


def test_every_copy_is_recorded_with_its_origin_and_says_what_was_removed():
    paths = [c["path"] for c in COPIES]
    assert len(paths) == len(set(paths)) and len(paths) > 40
    for c in COPIES:
        assert c["mode"] in ("verbatim", "trimmed", "split") and len(c["commit"]) == 40 and len(c["blob"]) == 40, c["path"]
        assert (OVERLAY / c["path"]).is_file(), c["path"]
        if c["mode"] == "trimmed":
            assert c["removed"].strip(), c["path"]
        if c["mode"] == "split":
            assert c["rule"]["keep"] and "header" in c["rule"], c["path"]


def test_a_verbatim_copy_is_byte_for_byte_the_recorded_blob():
    verbatim = [c for c in COPIES if c["mode"] == "verbatim"]
    assert len(verbatim) >= 15
    for c in verbatim:
        assert blob((ROOT / c["path"]).read_bytes()) == c["blob"], f"{c['path']} was edited: make it `trimmed`, or take the source again"


def test_a_trimmed_or_split_copy_names_its_origin_in_its_own_text():
    """The record is in copies.json; the file says it too, so a reader of the file alone knows it is a copy."""
    said = [c for c in COPIES if c["mode"] in ("trimmed", "split")
            and c["path"].endswith((".py", ".yaml", ".yml", ".toml", "Makefile")) or c["path"].startswith("deploy/bin/")]
    assert len(said) >= 25
    for c in said:
        if c["path"] in ("src/wt/research/__init__.py", "pyproject.toml", ".github/dependabot.yml",
                         ".github/workflows/ci.yml", ".github/workflows/security.yml", ".pre-commit-config.yaml") \
                or c["path"].startswith("deploy/"):
            continue                    # a one-line module, and files whose origin is recorded in copies.json only
        text = (ROOT / c["path"]).read_text()
        assert "Origin: trading-lab-research" in text and f"`{c['from']}`" in text and c["commit"][:7] in text, c["path"]


def test_the_split_configs_are_what_their_rule_gives_and_only_their_header_is_new():
    drift = tool("drift")
    for c in (c for c in COPIES if c["mode"] == "split"):
        text = (ROOT / c["path"]).read_text()
        header = "".join(h + "\n" for h in c["rule"]["header"])
        assert text.startswith(header), c["path"]
        body = text[len(header):]
        keys = [k for k, _ in drift.segments(body)]
        assert keys == c["rule"]["keep"], c["path"]
        # cutting this repository's file by its own rule changes nothing: no dropped key is left in it
        assert drift.split(body, c["rule"]) == text, c["path"]
    assert drift.split("# h\nA:\n  x: 1\n\n# about b\nB:\n  y: 2\n", {"keep": ["B"], "drop": ["A"]}) == "# about b\nB:\n  y: 2\n"
    try:
        drift.split("A: 1\nZ: 2\n", {"keep": ["A"], "drop": []})
    except ValueError as e:
        assert "'Z'" in str(e)
    else:
        raise AssertionError("an unknown top-level key must stop the split")


def test_the_allowlist_names_no_wildcard_under_research_and_every_entry_matches_something():
    overlay = tool("overlay")
    entries = overlay.allowlist()
    assert not [e for e in entries if e.startswith("glob:research")]
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True).stdout.splitlines()
    if files:                                                   # a source tree without git (an export) skips this
        for e in entries:
            assert any(overlay.moved(f, [e]) for f in files), f"{e} matches no file"
        assert sum(1 for f in files if overlay.moved(f, entries)) == SOURCE["files_moved_whole"]


def test_the_patches_are_the_only_changes_to_moved_files_and_each_is_in_the_decision_record():
    patches = sorted(p.name for p in (TOOLS / "patches").glob("*.patch"))
    assert patches == ["0001-no-silent-fresh-desk.patch", "0002-contract-in-contracts.patch",
                       "0003-host-packages.patch", "0004-runbook-for-this-repository.patch"]
    record = (ROOT / "research/decisions/CRY-DEC-0001-standalone-repository.md").read_text()
    for p in patches:
        assert p in record, p
    touched = set()
    for p in patches:
        touched |= {line[6:] for line in (TOOLS / "patches" / p).read_text().splitlines() if line.startswith("+++ b/")}
    assert touched == {"src/wt/crypto/cycle.py", "src/wt/crypto/learn.py", "src/wt/crypto/challengers.py",
                       "src/wt/crypto/control.py", "src/wt/crypto/snapshot.py", "scripts/gen_crypto_fixture.py",
                       "tests/unit/test_crypto.py", "tests/unit/test_ml_platform.py", "docs/runbooks/crypto-desk.md"}
