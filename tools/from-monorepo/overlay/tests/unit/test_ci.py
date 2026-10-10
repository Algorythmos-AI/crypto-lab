"""wt.ops.ci: a commit is green only when every required check's newest run concluded "success" on it."""
from __future__ import annotations

import pytest
import requests

from wt.ops import ci


def run(name, conclusion, id_, status="completed"):
    return {"name": name, "conclusion": conclusion, "status": status, "id": id_}


def test_the_newest_run_of_each_check_decides():
    runs = [run("test", "failure", 1), run("test", "success", 2), run("security / scan", "success", 3)]
    v = ci.verdict(runs, ("test", "security / scan"))
    assert v.green and v.checks == {"test": "success", "security / scan": "success"}
    v = ci.verdict([run("test", "success", 2), run("test", "failure", 5)], ("test",))
    assert not v.green and "test: failure" in v.detail


@pytest.mark.parametrize("runs, why", [([], "test: missing"), ([run("test", None, 1, "in_progress")], "test: pending"),
                                       ([run("test", "cancelled", 1)], "test: cancelled"),
                                       ([run("test", "skipped", 1)], "test: skipped")])
def test_anything_but_success_is_not_green(runs, why):
    v = ci.verdict(runs, ("test",))
    assert not v.green and why in v.detail


def test_repo_slug_from_https_and_ssh():
    assert ci.repo_slug("https://github.com/Org/repo.git") == "Org/repo"
    assert ci.repo_slug("git@github.com:Org/repo.git") == "Org/repo"
    with pytest.raises(ValueError):
        ci.repo_slug("https://example.com/x.git")


class Resp:
    def __init__(self, code, body, next_url=None):
        self.status_code, self._body = code, body
        self.links = {"next": {"url": next_url}} if next_url else {}

    def json(self):
        return self._body


class Session:
    def __init__(self, pages):
        self.pages, self.urls, self.headers = pages, [], []

    def get(self, url, headers, timeout):
        self.urls.append(url)
        self.headers.append(headers)
        return self.pages.pop(0)


def test_check_follows_pages_and_sends_the_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    s = Session([Resp(200, {"check_runs": [run("security", "success", 1)]}, "https://api.github.com/next"),
                 Resp(200, {"check_runs": [run("test", "success", 2)]})])
    v = ci.check("abc", ("test", "security"), "O/r", session=s)
    assert v.green and len(s.urls) == 2 and s.urls[0].endswith("/repos/O/r/commits/abc/check-runs?per_page=100")
    assert s.headers[0]["Authorization"] == "Bearer tok"


def test_check_fails_closed_on_api_trouble(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(ci.shutil, "which", lambda name: None)
    assert not ci.check("abc", ("test",), "O/r", session=Session([Resp(403, {})])).green

    class Down:
        def get(self, *a, **k):
            raise requests.ConnectionError("down")
    v = ci.check("abc", ("test",), "O/r", session=Down())
    assert not v.green and "unreachable" in v.detail


def test_required_names_come_from_the_environment(monkeypatch):
    monkeypatch.delenv("WT_REQUIRED_CHECKS", raising=False)
    assert ci.required_names() == ("test", "ml")           # a red ML suite stops a deploy too
    monkeypatch.setenv("WT_REQUIRED_CHECKS", "test, security / scan ,dashboard-gate")
    assert ci.required_names() == ("test", "security / scan", "dashboard-gate")
