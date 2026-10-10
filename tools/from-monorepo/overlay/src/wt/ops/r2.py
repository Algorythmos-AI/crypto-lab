"""A minimal S3 client for Cloudflare R2 (AWS Signature V4), for the lease and the daily anchors (ADR 0004).

Only what we need: GET, PUT with If-Match / If-None-Match (conditional writes are what make the lease safe and the
anchors write-once), and the server's Date header (the lease's clock is the store's, not the host's). No new
dependency: requests is already locked. Credentials: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (one
token per bucket, docs/runbooks/oci-host.md).
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import hmac
import os
import urllib.parse
from dataclasses import dataclass

import requests

REGION, SERVICE = "auto", "s3"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass
class Response:
    status: int
    body: bytes
    etag: str | None
    server_time: dt.datetime | None


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, headers: dict[str, str], payload_sha256: str, access_key: str,
         secret_key: str, now: dt.datetime, region: str = REGION, service: str = SERVICE,
         query: str = "") -> dict[str, str]:
    """Headers (including Authorization) for a SigV4-signed request. `path` must already be URI-encoded."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    h = {k.lower(): v.strip() for k, v in headers.items()}
    h.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_sha256})
    names = sorted(h)
    canonical = "\n".join([method, path, query, "".join(f"{k}:{h[k]}\n" for k in names), ";".join(names),
                           payload_sha256])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in h.items() if k != "host"}
    out["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, "
                            f"Signature={signature}")
    return out


def strong_etag(etag: str | None) -> str | None:
    """The entity tag to send back in If-Match. Cloudflare marks the ETag of a response it compressed as weak
    (W/"..."), and R2 refuses a weak tag in If-Match with 412: every lease renewal was "lost a race". The object's
    bytes are what the tag names, so the strong form is the right one to send."""
    return etag.removeprefix("W/") if etag else etag


class R2:
    def __init__(self, account_id: str | None = None, access_key: str | None = None, secret_key: str | None = None,
                 timeout: float = 10.0) -> None:
        self.account = account_id or os.environ.get("R2_ACCOUNT_ID", "")
        self.access = access_key or os.environ.get("R2_ACCESS_KEY_ID", "")
        self.secret = secret_key or os.environ.get("R2_SECRET_ACCESS_KEY", "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.account and self.access and self.secret)

    def _request(self, method: str, bucket: str, key: str, body: bytes = b"",
                 headers: dict[str, str] | None = None) -> Response:
        host = f"{self.account}.r2.cloudflarestorage.com"
        path = "/" + urllib.parse.quote(f"{bucket}/{key}", safe="/-_.~")
        payload = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        signed = sign(method, host, path, headers or {}, payload, self.access, self.secret, dt.datetime.now(dt.UTC))
        r = requests.request(method, f"https://{host}{path}", data=body or None, headers=signed, timeout=self.timeout)
        date = r.headers.get("Date")
        server = email.utils.parsedate_to_datetime(date) if date else None
        return Response(r.status_code, r.content, strong_etag(r.headers.get("ETag")), server)

    def get(self, bucket: str, key: str) -> Response:
        return self._request("GET", bucket, key)

    def put(self, bucket: str, key: str, body: bytes, if_match: str | None = None,
            if_none_match: str | None = None, content_type: str = "application/json") -> Response:
        headers = {"content-type": content_type}
        if if_match:
            headers["if-match"] = if_match
        if if_none_match:
            headers["if-none-match"] = if_none_match
        return self._request("PUT", bucket, key, body, headers)
