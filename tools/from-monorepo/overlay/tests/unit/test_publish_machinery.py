"""The publishing machinery the desk's snapshot is built with (wt.ops.publish): the allowlist that fails closed,
the signature, the send, the record of attempts. The stocks snapshot it also built in its origin is not here."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json

import pytest

from wt.ops import publish
from wt.ops.publish import B, I, Map, N, OneOf, S, Sanitizer, T
from wt.ops.safeio import Redactor

NOW = dt.datetime(2026, 10, 11, 4, 0, tzinfo=dt.UTC)


def san(strict: bool = False, leak=None) -> Sanitizer:
    return Sanitizer(Redactor({"s3cr3t-value"}, home="/home/wt"), leak, strict)


def test_only_what_the_allowlist_names_gets_out_and_wrong_types_become_null():
    spec = {"n": N, "i": I, "b": B, "s": S, "m": Map(I), "rows": [{"k": S}], "one": OneOf(("a", "b"))}
    raw = {"n": 1.5, "i": 2.0, "b": True, "s": "x", "m": {"ok": 1, "bad key!": 2}, "rows": [{"k": "v", "extra": 1}],
           "one": "c", "not_listed": "never"}
    assert san().apply(spec, raw) == {"n": 1.5, "i": 2, "b": True, "s": "x", "m": {"ok": 1}, "rows": [{"k": "v"}], "one": None}
    assert san().apply(spec, {"n": float("nan"), "i": 1.5, "b": 1, "s": {"a": 1}, "rows": "x"}) == \
        {"n": None, "i": None, "b": None, "s": None, "rows": None}
    assert san().apply(N, True) is None and san().apply(I, True) is None


def test_strings_are_redacted_and_cut_and_free_text_is_dropped_in_strict_mode():
    s = san()
    assert s.apply(S, "key s3cr3t-value in /home/wt/crypto-lab") == "key [redacted] in ~/crypto-lab"
    assert len(s.apply(S, "x" * 500)) == publish.SHORT_MAX and len(s.apply(T, "y" * 500)) == publish.TEXT_MAX
    assert san(strict=True).apply(T, "free words") is None and san(strict=True).apply(S, "code") == "code"

    class Leak:
        def leaks(self, text):
            return "forbidden" in text
    held = san(leak=Leak())
    assert held.apply(S, "a forbidden phrase") is None and held.withheld == 1 and held.apply(S, "fine") == "fine"


def test_a_long_list_keeps_its_newest_items_and_says_how_many_it_dropped():
    s = san()
    out = s.apply([I], list(range(publish.LIST_MAX + 5)))
    assert len(out) == publish.LIST_MAX and out[-1] == publish.LIST_MAX + 4 and s.truncated == 5


def test_the_schema_rejects_unknown_properties_at_every_level():
    schema = publish.to_schema({"a": S, "rows": [{"n": N}], "m": Map({"x": I}), "one": OneOf(("u",))})
    assert schema["additionalProperties"] is False and schema["type"] == ["object", "null"]
    assert schema["properties"]["rows"]["items"]["additionalProperties"] is False
    assert schema["properties"]["m"]["additionalProperties"]["additionalProperties"] is False
    assert schema["properties"]["a"] == {"type": ["string", "null"], "maxLength": 200}
    assert schema["properties"]["one"] == {"enum": ["u", None]} and publish.to_schema({}, top=True)["type"] == "object"
    assert publish.REQUIRED == ("schema", "schema_version", "run_id", "as_of")


def test_the_signature_is_hmac_sha256_over_the_timestamp_a_dot_and_the_body():
    body = b'{"a":1}'
    want = "sha256=" + hmac.new(b"k", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert publish.sign(body, "k", 1_700_000_000) == want


class Reply:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_send_signs_and_a_409_is_a_newer_snapshot_already_stored_not_an_error(monkeypatch):
    seen = []

    def post(url, data, headers, timeout):
        seen.append((url, headers))
        return Reply([200, 409, 422][len(seen) - 1])
    monkeypatch.setattr(publish.requests, "post", post)
    monkeypatch.setenv("DASHBOARD_KEY_ID", "host-a")
    assert publish.send(b"{}", "https://x/api/ingest", "k", "bypass") == (True, "HTTP 200")
    h = seen[0][1]
    assert h["x-wt-signature"].startswith("sha256=") and h["x-wt-key-id"] == "host-a" and h["x-vercel-protection-bypass"] == "bypass"
    assert publish.send(b"{}", "https://x/api/ingest", "k", None)[0] is True          # 409: counts as stored
    assert publish.send(b"{}", "https://x/api/ingest", "k", None)[0] is False         # 422: rejected, not retried
    assert len(seen) == 3


def test_a_send_that_keeps_failing_stops_at_its_deadline(monkeypatch):
    calls = []
    monkeypatch.setattr(publish.requests, "post", lambda *a, **k: calls.append(1) or Reply(503, "down"))
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    ok, detail = publish.send(b"{}", "u", "k", None, tries=4)
    assert not ok and detail.startswith("HTTP 503") and len(calls) == 4
    calls.clear()
    assert publish.send(b"{}", "u", "k", None, tries=4, deadline=publish.time.monotonic())[0] is False and len(calls) == 1


def test_error_codes_never_carry_the_response_text():
    assert publish.error_code("HTTP 503: <html>secret</html>") == "http_503"
    assert publish.error_code("ConnectTimeout") == "network_ConnectTimeout" and publish.error_code("") == "unknown"


def test_the_outcome_record_counts_failures_in_a_row(tmp_path):
    p = tmp_path / "out" / "publish_state.json"
    assert publish.record_outcome(False, "HTTP 500: x", NOW, p)["consecutive_failures"] == 1
    st = publish.record_outcome(False, "ReadTimeout", NOW, p)
    assert st["consecutive_failures"] == 2 == publish.ALERT_AFTER_FAILURES and st["last_error_code"] == "network_ReadTimeout"
    st = publish.record_outcome(True, "HTTP 200", NOW, p)
    assert st["consecutive_failures"] == 0 and st["last_ok"] == NOW.isoformat() and json.loads(p.read_text()) == st


def test_health_is_read_from_the_ingest_urls_sibling(monkeypatch):
    assert publish.health_url("https://x/api/ingest/") == "https://x/api/health"
    monkeypatch.setattr(publish.requests, "get", lambda url, headers, timeout: type("R", (), {"json": lambda self: {"desks": {}}})())
    assert publish.read_health("https://x/api/ingest", "b") == ({"desks": {}}, "")
    monkeypatch.setattr(publish.requests, "get", lambda url, headers, timeout: type("R", (), {"json": lambda self: [1]})())
    assert publish.read_health("https://x/api/ingest", None)[0] is None


@pytest.mark.parametrize("env, refused", [({}, False), ({"WT_ROLE": "shadow"}, True), ({"WT_ROLE": "shadow", "DASHBOARD_KEY_ID": "default"}, True),
                                          ({"WT_ROLE": "shadow", "DASHBOARD_KEY_ID": "host-b"}, False)])
def test_a_shadow_host_never_publishes_under_the_default_key(env, refused):
    assert (publish.shadow_key_problem(env) is not None) is refused


def test_the_stocks_snapshot_is_not_built_here():
    for gone in ("ALLOW", "from_docs", "extras", "build", "validate", "collect", "LeakIndex", "corpus_files", "main"):
        assert not hasattr(publish, gone), gone
    assert publish.SCHEMA_ID == "trading-lab/snapshot"            # named only so the crypto id can be shown to differ
