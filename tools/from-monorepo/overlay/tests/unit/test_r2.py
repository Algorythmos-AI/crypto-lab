"""SigV4 signing for R2 is checked against AWS's published S3 example ("GET Object", bytes=0-9)."""
import datetime as dt

from wt.ops import r2


def test_sigv4_matches_the_aws_get_object_example():
    headers = r2.sign(
        "GET", "examplebucket.s3.amazonaws.com", "/test.txt", {"range": "bytes=0-9"}, r2.EMPTY_SHA256,
        "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        dt.datetime(2013, 5, 24, tzinfo=dt.UTC), region="us-east-1")
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_conditional_headers_are_signed(monkeypatch):
    seen = {}

    class Resp:
        status_code, content = 412, b""
        headers = {"Date": "Wed, 30 Sep 2026 01:00:00 GMT", "ETag": '"x"'}

    def request(method, url, data, headers, timeout):
        seen.update(method=method, url=url, headers=headers)
        return Resp()
    monkeypatch.setattr(r2.requests, "request", request)
    c = r2.R2("acct", "ak", "sk")
    out = c.put("wt-leases", "paper-b", b"{}", if_none_match="*")
    assert out.status == 412 and out.server_time == dt.datetime(2026, 9, 30, 1, 0, tzinfo=dt.UTC)
    assert seen["url"] == "https://acct.r2.cloudflarestorage.com/wt-leases/paper-b"
    assert seen["headers"]["if-none-match"] == "*" and "if-none-match" in seen["headers"]["authorization"]


def test_a_weak_etag_is_sent_back_strong(monkeypatch):
    """Cloudflare returns W/"..." for a compressed (JSON) body; R2 answers 412 to that in If-Match."""
    from wt.ops import r2 as r2mod
    assert r2mod.strong_etag('W/"4522141b"') == '"4522141b"'
    assert r2mod.strong_etag('"4522141b"') == '"4522141b"' and r2mod.strong_etag(None) is None
    seen = []

    class Resp:
        status_code, content = 200, b"{}"
        headers = {"ETag": 'W/"abc"', "Date": "Thu, 01 Oct 2026 09:00:00 GMT"}

    def request(method, url, data=None, headers=None, timeout=None):
        seen.append((method, headers))
        return Resp()
    monkeypatch.setattr(r2mod.requests, "request", request)
    c = r2mod.R2("acct", "key", "secret")
    got = c.get("wt-leases", "paper-b")
    assert got.etag == '"abc"'
    c.put("wt-leases", "paper-b", b"{}", if_match=got.etag)
    assert seen[1][1]["if-match"] == '"abc"'
