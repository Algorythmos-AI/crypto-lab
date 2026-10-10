"""Dead-man's-switch pings to healthchecks.io, the second off-host monitor after the dashboard's watchdog.

Each job pings its own check by slug (the ping key and slug URLs, so no check id ever lives in this repository):
    https://hc-ping.com/<HC_PING_KEY>/<slug>          success
    https://hc-ping.com/<HC_PING_KEY>/<slug>/fail     failure or refusal (a refusal exits 0 but is not success)
Slugs: wt-crypto, wt-crypto-challengers, wt-crypto-learn (the ones the jobs had before they moved here, so the
checks carry on), and wt-crypto-backup for this repository's own backup.

Never raises, never blocks longer than the timeout, and does nothing without HC_PING_KEY (unset in tests).
Bodies are short, scrubbed of $ amounts and account ids, and say which host sent them.

A shadow host pings no check: the slugs are shared, so its successes would mask a run the primary missed.

Origin: trading-lab-research `src/wt/ops/hc.py` at 217a4eb (trimmed). Removed: the host's own heartbeat check
(`heartbeat`, `heartbeat_slug`), which the stocks publisher on the shared host keeps sending, and `ping_async`,
which only the stocks trading loop used.
"""
from __future__ import annotations

import os
import socket

import requests

from wt.ops.alerts import scrub

BASE = "https://hc-ping.com"


def host_id() -> str:
    return os.environ.get("WT_HOST_ID") or socket.gethostname().split(".")[0]


def _host_tag() -> str:
    return f"host={host_id()} role={os.environ.get('WT_ROLE', 'primary')}"


def ping(slug: str, signal: str = "", body: str = "", timeout: float = 5.0) -> bool:
    """signal: "" (success), "fail", "start" or "log". Returns True when healthchecks.io accepted it."""
    key = os.environ.get("HC_PING_KEY")
    if not key:
        return False
    if os.environ.get("WT_ROLE", "primary") == "shadow":
        return False                               # a shadow's success would hide a missed primary run
    url = f"{os.environ.get('HC_PING_BASE', BASE).rstrip('/')}/{key}/{slug}" + (f"/{signal}" if signal else "")
    text = scrub(f"{_host_tag()} {body}".strip())[:2000]
    try:
        return requests.post(url, data=text.encode("utf-8"), timeout=timeout).ok
    except requests.RequestException:
        return False
