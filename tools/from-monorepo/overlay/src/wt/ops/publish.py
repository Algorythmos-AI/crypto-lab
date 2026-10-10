"""The publishing machinery the crypto desk's snapshot is built with (`wt.crypto.snapshot`).

  1. An allowlist that fails closed: a field that is not listed is dropped (`Sanitizer.apply`), and the JSON Schema
     generated from the same allowlist (`to_schema`) rejects unknown properties everywhere.
  2. Signing: HMAC-SHA256 over "<timestamp>.<body>", sent with the deployment-protection bypass header (`send`).
  3. The record of the last attempts (`record_outcome`) and the read-back of the dashboard's health (`read_health`).

The snapshot itself, its allowlist and its schema id belong to the desk (`wt.crypto.snapshot`). The dashboard that
receives it lives in another repository and vendors the schema from `contracts/`.

Origin: trading-lab-research `src/wt/ops/publish.py` at 217a4eb (trimmed). Removed: the stocks snapshot (its
allowlist `ALLOW`, schema path and version, `from_docs`, `extras`, `v3_views`, the series and window builders,
`build`, `validate`, `verify_stored`), the collector child process and `main`, the phrase-overlap check against
restricted text (`LeakIndex`, `corpus_files`; the crypto snapshot has no free-text field and never used it),
`codeify` and the other free-text helpers, and the host heartbeat ping after a publish. Kept unchanged: the leaf
types, `to_schema` for a nested spec, `Sanitizer`, `sign`, `send`, `error_code`, `record_outcome`, `health_url`,
`read_health`, `shadow_key_problem`, `_nonempty_readable` and every limit they use. `to_schema` and
`record_outcome` lost the defaults that named the stocks snapshot: the spec and the state path are now required.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import requests

from wt.ops import safeio

# The schema id of the stocks desk's snapshot, which is not built here. Kept so that the crypto snapshot's own id
# can be shown never to be this one: the dashboard files a snapshot by its id.
SCHEMA_ID = "trading-lab/snapshot"
MAX_BODY = 3_000_000                      # Vercel's request limit is 4.5 MB
BUDGET = 300_000                          # what a snapshot should stay under
TEXT_MAX, SHORT_MAX = 300, 200
LIST_MAX = 2000


# ---- the allowlist -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Leaf:
    kind: str                             # S short string · T free text · N number · I integer · B boolean


@dataclass(frozen=True)
class OneOf:
    values: tuple[str, ...]               # a short string from a closed set (anything else publishes as null)


@dataclass(frozen=True)
class Map:
    value: Any                            # a dict with arbitrary short keys, each value following `value`


S, T, N, I, B = Leaf("S"), Leaf("T"), Leaf("N"), Leaf("I"), Leaf("B")
REQUIRED = ("schema", "schema_version", "run_id", "as_of")
_KEY = re.compile(r"^[\w.:+\- /|]{1,64}$")


def to_schema(spec: Any, top: bool = False) -> dict[str, Any]:
    """JSON Schema (draft-07) for a sanitized snapshot. Unknown properties are rejected everywhere. The desk adds
    the document's own header (`$schema`, `$id`, `title`, `required`) around what this returns."""
    if isinstance(spec, Leaf):
        leaves: dict[str, dict[str, Any]] = {"S": {"type": ["string", "null"], "maxLength": SHORT_MAX},
                "T": {"type": ["string", "null"], "maxLength": TEXT_MAX},
                "N": {"type": ["number", "null"]}, "I": {"type": ["integer", "null"]},
                "B": {"type": ["boolean", "null"]}}
        return leaves[spec.kind]
    if isinstance(spec, OneOf):
        return {"enum": [*spec.values, None]}
    if isinstance(spec, Map):
        return {"type": ["object", "null"], "propertyNames": {"pattern": _KEY.pattern},
                "additionalProperties": to_schema(spec.value, False), "maxProperties": 200}
    if isinstance(spec, list):
        return {"type": ["array", "null"], "items": to_schema(spec[0], False), "maxItems": LIST_MAX}
    return {"type": "object" if top else ["object", "null"], "additionalProperties": False,
            "properties": {k: to_schema(v, False) for k, v in spec.items()}}


# ---- sanitizing ----------------------------------------------------------------------------------------------------

class Leak(Protocol):
    """A phrase check a free text is held back by. None is in use here: the crypto snapshot has no free text."""

    def leaks(self, text: str) -> bool: ...


@dataclass
class Sanitizer:
    redact: Callable[[Any], Any]
    leak: Leak | None
    strict: bool
    withheld: int = 0
    truncated: int = 0                    # list items dropped by the per-list cap (the oldest go first)

    def text(self, v: Any, limit: int) -> str | None:
        if v is None:
            return None
        s = str(self.redact(str(v)))
        s = s if len(s) <= limit else s[: limit - 1] + "…"
        if self.leak is not None and self.leak.leaks(s):
            self.withheld += 1
            return None
        return s

    def apply(self, spec: Any, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(spec, Leaf):
            k = spec.kind
            if k == "B":
                return v if isinstance(v, bool) else None
            if k == "N":
                return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and np.isfinite(v) else None
            if k == "I":
                if isinstance(v, bool):
                    return None
                if isinstance(v, int):
                    return v
                return int(v) if isinstance(v, float) and v.is_integer() else None
            if k == "S":
                return self.text(v, SHORT_MAX) if isinstance(v, str | int | float) else None
            return None if self.strict else self.text(v, TEXT_MAX) if isinstance(v, str) else None
        if isinstance(spec, OneOf):
            return v if isinstance(v, str) and v in spec.values else None
        if isinstance(spec, Map):
            if not isinstance(v, dict):
                return None
            return {str(k): self.apply(spec.value, x) for k, x in list(v.items())[:200] if _KEY.match(str(k))}
        if isinstance(spec, list):
            if not isinstance(v, list):
                return None
            if len(v) > LIST_MAX:                 # builders append in time order: keep the newest
                self.truncated += len(v) - LIST_MAX
                v = v[-LIST_MAX:]
            return [self.apply(spec[0], x) for x in v]
        if not isinstance(v, dict):
            return None
        return {k: self.apply(s, v[k]) for k, s in spec.items() if k in v}


# ---- signing and sending -------------------------------------------------------------------------------------------

def sign(body: bytes, secret: str, ts: int) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()


def send(body: bytes, url: str, secret: str, bypass: str | None, tries: int = 3,
         deadline: float | None = None) -> tuple[bool, str]:
    """POST the snapshot, retrying transient failures with backoff (2, 4, 8 ... s). `deadline` (time.monotonic()):
    never start a wait that would end after it, so a network drop can't run the job past its own limit."""
    last = ""
    for i in range(tries):
        ts = int(time.time())
        headers = {"content-type": "application/json", "x-wt-timestamp": str(ts),
                   "x-wt-signature": sign(body, secret, ts)}
        if key_id := os.environ.get("DASHBOARD_KEY_ID"):
            headers["x-wt-key-id"] = key_id          # this host's own key; absent = "default"
        if bypass:
            headers["x-vercel-protection-bypass"] = bypass
        try:
            r = requests.post(url, data=body, headers=headers, timeout=SEND_TIMEOUT_S)
            if 200 <= r.status_code < 300:
                return True, f"HTTP {r.status_code}"
            last = f"HTTP {r.status_code}: {r.text[:160]}"
            if r.status_code in (400, 401, 403, 409, 413, 422):
                return r.status_code == 409, last          # 409: a newer snapshot is already stored; not an error
        except requests.RequestException as e:
            last = e.__class__.__name__
        wait = 2 ** (i + 1)
        if i == tries - 1 or (deadline is not None and time.monotonic() + wait + SEND_TIMEOUT_S > deadline):
            break
        time.sleep(wait)
    return False, last


SEND_TIMEOUT_S = 20.0
SEND_TRIES = 7                    # backoff 2..64 s, bounded by PUBLISH_BUDGET_S
PUBLISH_BUDGET_S = 330.0          # under the publish job's 6-minute deadline (wt.ops.schedule)
ALERT_AFTER_FAILURES = 2          # one failed publish is noise; the second in a row fails the job (and alerts)
LOCK_WAIT_S = 60.0                # a publish by hand while the 15-minute job runs: wait a little, then give way


def error_code(detail: str) -> str:
    """A fixed code for a send failure ("http_503", "network_ConnectTimeout"): never the response text."""
    m = re.match(r"HTTP (\d{3})", detail)
    if m:
        return f"http_{m.group(1)}"
    return "network_" + re.sub(r"[^A-Za-z0-9]", "", detail)[:40] if detail else "unknown"


def record_outcome(ok: bool, detail: str, now: dt.datetime, path: Path) -> dict[str, Any]:
    """Keep {last_attempt, last_ok, consecutive_failures, last_error_code} across runs. Never raises."""
    try:
        st: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st["last_attempt"] = now.isoformat()
    if ok:
        st.update(last_ok=now.isoformat(), consecutive_failures=0, last_error_code=None)
    else:
        st.update(consecutive_failures=int(st.get("consecutive_failures") or 0) + 1, last_error_code=error_code(detail))
    try:
        safeio.atomic_write(path, json.dumps(st, indent=1), path.parent)
    except OSError:
        pass
    return st


def health_url(ingest_url: str) -> str:
    return re.sub(r"/api/ingest/?$", "/api/health", ingest_url)


def read_health(url: str, bypass: str | None) -> tuple[dict[str, Any] | None, str]:
    """(/api/health's body, "") or (None, why)."""
    headers = {"x-vercel-protection-bypass": bypass} if bypass else {}
    try:
        h = requests.get(health_url(url), headers=headers, timeout=20).json()
    except (requests.RequestException, ValueError) as e:
        return None, f"health unreadable ({e.__class__.__name__})"
    return (h, "") if isinstance(h, dict) else (None, "health unreadable (not an object)")


def _nonempty_readable(p: Path) -> bool:
    try:
        return bool(p.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def shadow_key_problem(env: dict[str, str] | None = None) -> str | None:
    """A shadow host must sign with its own key id: under the default key the dashboard takes its snapshots as the
    primary's and overwrites them. None when it is fine to send."""
    e = os.environ if env is None else env
    if e.get("WT_ROLE", "primary") == "shadow" and e.get("DASHBOARD_KEY_ID", "").strip() in ("", "default"):
        return ("shadow host without its own DASHBOARD_KEY_ID: not publishing (it would overwrite the primary's "
                "snapshot). Add DASHBOARD_KEY_ID to the vault and its key to the dashboard's DASHBOARD_INGEST_KEYS.")
    return None
