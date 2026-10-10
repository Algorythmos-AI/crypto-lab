"""Is a commit green? The required GitHub checks of one commit, read from the public API (plan v7 B2).

    python -m wt.ops.ci <sha>          # prints each required check's conclusion; exit 0 only when all succeeded

A deploy (wt.ops.deploy) takes only a commit on main whose required checks all concluded "success" on that exact
commit. Pending, cancelled, skipped, failed or missing count as not green. When a check was re-run, its newest run
decides. Required names come from WT_REQUIRED_CHECKS (comma-separated; default "test,ml").

Auth: GH_TOKEN or GITHUB_TOKEN when set (a read-only token; the repository may be private), else the `gh` CLI's
token when it is logged in (the Mac), else unauthenticated (a public repository, 60 requests an hour per IP).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

import requests

from wt.core.config import ROOT

API = "https://api.github.com"
DEFAULT_REQUIRED = ("test", "ml")    # ml: the learning job and the scorer run what that job tests


@dataclass(frozen=True)
class Verdict:
    green: bool
    detail: str
    checks: dict[str, str]


def required_names() -> tuple[str, ...]:
    raw = os.environ.get("WT_REQUIRED_CHECKS", "")
    names = tuple(n.strip() for n in raw.split(",") if n.strip())
    return names or DEFAULT_REQUIRED


def repo_slug(remote_url: str | None = None) -> str:
    """owner/name of origin, from an https or ssh remote URL."""
    url = remote_url
    if url is None:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True,
                             timeout=10).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError(f"origin is not a GitHub repository: {url!r}")
    return m.group(1)


def token() -> str | None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if v := os.environ.get(name, "").strip():
            return v
    gh = shutil.which("gh")
    if gh:
        try:
            r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None


def conclusions(runs: list[dict[str, Any]]) -> dict[str, str]:
    """Check name -> the newest run's conclusion ("pending" while it hasn't completed)."""
    newest: dict[str, dict[str, Any]] = {}
    for r in runs:
        name = str(r.get("name", ""))
        if name not in newest or int(r.get("id", 0)) > int(newest[name].get("id", 0)):
            newest[name] = r
    return {n: (str(r.get("conclusion")) if r.get("status") == "completed" else "pending") for n, r in newest.items()}


def verdict(runs: list[dict[str, Any]], required: tuple[str, ...]) -> Verdict:
    got = conclusions(runs)
    per = {n: got.get(n, "missing") for n in required}
    bad = {n: c for n, c in per.items() if c != "success"}
    detail = "all required checks succeeded" if not bad else "; ".join(f"{n}: {c}" for n, c in bad.items())
    return Verdict(not bad, detail, per)


def check(sha: str, required: tuple[str, ...] | None = None, slug: str | None = None,
          session: Any = requests) -> Verdict:
    """The required checks of `sha`. Any API failure is "not green" (fails closed)."""
    required = required or required_names()
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok := token():
        headers["Authorization"] = f"Bearer {tok}"
    runs: list[dict[str, Any]] = []
    try:
        url: str | None = f"{API}/repos/{slug or repo_slug()}/commits/{sha}/check-runs?per_page=100"
        while url:
            r = session.get(url, headers=headers, timeout=20)
            if r.status_code != 200:
                return Verdict(False, f"GitHub API answered HTTP {r.status_code}", {})
            runs += list(r.json().get("check_runs", []))
            url = r.links.get("next", {}).get("url") if hasattr(r, "links") else None
    except (requests.RequestException, ValueError) as e:
        return Verdict(False, f"GitHub API unreachable ({e.__class__.__name__})", {})
    return verdict(runs, required)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m wt.ops.ci <sha>", file=sys.stderr)
        return 2
    v = check(args[0])
    for n, c in v.checks.items():
        print(f"  {n}: {c}")
    print(("GREEN: " if v.green else "NOT GREEN: ") + v.detail)
    return 0 if v.green else 1


if __name__ == "__main__":
    sys.exit(main())
