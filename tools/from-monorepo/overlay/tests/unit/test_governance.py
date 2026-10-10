"""The repository's own rules, where a machine can check them: governance files, CI, and the separation from the
other trading platforms (no import of their code, no venue credential, nothing of theirs on disk)."""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "wt"
DESCRIPTION = ("Crypto Lab: crypto paper-trading desk on public market data and its own simulator, with a learning "
               "loop (Python)")


def test_the_readme_opens_with_the_description_and_the_org_header_table():
    text = (ROOT / "README.md").read_text()
    assert text.startswith("# Crypto Lab\n") and f"> {DESCRIPTION}\n" in text
    for row in ("**Status**", "**Owner**", "**Runs at**", "**Run locally**", "**Context**"):
        assert f"| {row}" in text, row
    assert "[@Algorythmos-AI/maintainers](https://github.com/orgs/Algorythmos-AI/teams/maintainers)" in text
    assert "`make bootstrap && make ci`" in text and "paper only" in text


def test_the_licence_is_proprietary_and_names_the_product_and_the_company():
    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("Crypto Lab — Proprietary Software License\nCopyright © 2026 Algorythmos Pty Ltd. All rights reserved.\n")
    assert "ALL RIGHTS RESERVED." in text and "NOT FINANCIAL ADVICE." in text and "ACN 701 006 626" in text


def test_codeowners_agents_and_the_pointer_file_are_what_the_org_expects():
    owners = [x for x in (ROOT / ".github/CODEOWNERS").read_text().splitlines() if x and not x.startswith("#")]
    assert owners == ["* @Algorythmos-AI/maintainers"]
    assert (ROOT / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    agents = (ROOT / "AGENTS.md").read_text()
    for rule in ("**Paper only, by construction.**", "**One platform per pull request, and no imports from other platforms.**",
                 "**Pre-registration.**", "**Commits and PRs.**", "No `Co-Authored-By` trailer", "skalaliya <skalaliya@gmail.com>",
                 "`CRY-DEC-NNNN`", "`test` and `ml` checks"):
        assert rule in agents, rule
    security = (ROOT / "SECURITY.md").read_text()
    assert "info@algorythmos.com.au" in security and "Never open a public issue" in security


def test_ci_has_exactly_the_two_required_jobs_reads_only_and_holds_no_secret():
    wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert list(wf["jobs"]) == ["test", "ml"] and wf["permissions"] == {"contents": "read"}
    on = wf.get("on", wf.get(True))
    assert set(on) == {"pull_request", "push"} and on["push"] == {"branches": ["main"]} and not on["pull_request"]
    for name in ("ci.yml", "security.yml"):
        text = (ROOT / ".github/workflows" / name).read_text()
        assert "secrets." not in text and "pull_request_target" not in text, name
        assert yaml.safe_load(text)["permissions"] == {"contents": "read"}, name
        for use in re.findall(r"uses:\s*(\S+)", text):
            assert re.search(r"@[0-9a-f]{40}$", use), f"{use} is not pinned to a commit"
    test_steps = yaml.safe_dump(wf["jobs"]["test"])
    for needed in ("requirements.lock.txt", "--require-hashes", "ruff check", "make typecheck", "make contracts",
                   "git diff --exit-code -- contracts/", "make overlay-check", "pytest"):
        assert needed in test_steps, needed


def test_the_pre_commit_config_blocks_runtime_state_and_runs_gitleaks():
    cfg = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = {h["id"]: h for r in cfg["repos"] for h in r["hooks"]}
    assert {"gitleaks", "check-runtime", "ruff", "detect-private-key"} <= set(hooks)
    blocked = re.compile(hooks["check-runtime"]["files"])
    for path in ("var/crypto/book.json", "data/crypto/history/BTC-USD-3600.jsonl", "logs/crypto_20261011.log", ".env",
                 "KILL", "models/m1.pkl", "x.parquet"):
        assert blocked.search(path), path
    for path in (".env.example", "config/crypto.yaml", "tests/fixtures/kraken/ticker.json", "contracts/crypto.v1.json"):
        assert not blocked.search(path), path
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    for line in ("/var/", "/data/", "/logs/", ".env", ".venv/", ".venv-ml/", "/KILL"):
        assert line in ignored, line


def imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    return out


def test_every_wt_import_resolves_inside_this_repository():
    """One platform, one repository: the desk imports nothing that is not here. In its origin the same package
    also held the stocks desk (brokers, order management, the paper runner, the scanner); none of that came."""
    assert sorted(p.name for p in SRC.iterdir() if p.is_dir() and p.name != "__pycache__") == \
        ["backtest", "core", "crypto", "ml", "ops", "research"]
    for base in (SRC, ROOT / "scripts", ROOT / "tests"):
        for path in sorted(base.rglob("*.py")):
            for name in imports(path):
                if not (name == "wt" or name.startswith("wt.")):
                    continue
                parts = name.split(".")[1:]
                found = any((SRC.joinpath(*parts[:k]).with_suffix(".py").exists() or SRC.joinpath(*parts[:k], "__init__.py").exists())
                            for k in range(len(parts), 0, -1) if k >= min(len(parts), 2))
                assert found, f"{path.relative_to(ROOT)} imports {name}, which is not in this repository"


def test_no_broker_or_exchange_client_library_and_no_other_platforms_package():
    banned = ("alpaca", "ccxt", "krakenex", "ib_async", "ib_insync", "ibapi", "hftlab", "websockets", "pandas")
    for base in (SRC, ROOT / "scripts"):
        for path in sorted(base.rglob("*.py")):
            hit = [m for m in imports(path) if m.split(".")[0] in banned]
            assert not hit, f"{path.relative_to(ROOT)} imports {hit}"
    for lock in ("requirements.lock.txt", "requirements-dev.lock.txt", "requirements-ml.lock.txt"):
        text = (ROOT / lock).read_text().lower()
        for name in ("alpaca", "ccxt", "krakenex", "ib-async", "ib_async"):
            assert name not in text, (lock, name)
    assert [p.name for p in (SRC / "crypto").glob("*.py") if "requests" in imports(p)] == ["data.py"]


def test_the_repository_carries_its_own_identity_in_git():
    def cfg(key: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), "config", "--local", "--get", key], capture_output=True, text=True).stdout.strip()
    if not (ROOT / ".git").exists():
        return                                                  # an export of the tree: nothing to check
    if cfg("user.name"):                                        # set by the extraction; a fresh clone sets its own
        assert (cfg("user.name"), cfg("user.email")) == ("skalaliya", "skalaliya@gmail.com")
    own = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%an <%ae>%n%B", "--author=skalaliya@gmail.com", "-50"],
                         capture_output=True, text=True).stdout
    assert "co-authored-by" not in own.lower()


def test_no_tracked_file_carries_a_path_of_the_machine_it_was_made_on():
    """AGENTS.md "Data classes": nothing of a developer's machine is committed. A lockfile compiled against a
    scratch copy once kept that directory in its notes, user name and all. The only home directory this
    repository names is the job user's on the host. The pattern is spelled in pieces: this file is tracked too."""
    listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True)
    if listed.returncode != 0:
        return                                                  # an export of the tree: nothing to list
    local = re.compile(("/Us" "ers/|/private/" "(tmp|var)/|/var/fold" "ers/|/ho" "me/(?!wt\\b)").encode())
    hits = []
    for name in filter(None, listed.stdout.split(b"\0")):
        path = ROOT / name.decode()
        if path.is_file() and (m := local.search(path.read_bytes())):
            hits.append(f"{name.decode()}: {m.group().decode()}")
    assert not hits, hits
