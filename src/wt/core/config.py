"""Configuration loading. Secrets come only from .env; tunables from config/*.yaml.

Origin: trading-lab-research `src/wt/core/config.py` at 217a4eb (trimmed). Removed: the stocks desk's state
paths (the forward ledger, the routine, watchlist and scorecard directories).
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / ".env")
# On the host the secrets live in a tmpfs file written at boot; systemd passes it as EnvironmentFile and names it
# here, so interactive commands see the same values.
if os.environ.get("WT_ENV_FILE"):
    load_dotenv(os.environ["WT_ENV_FILE"], override=False)

DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"

# Runtime state written by the jobs. Git-ignored, so a fast-forward of the live checkout can never collide with a
# file a job is writing.
STATE_DIR = Path(os.environ.get("WT_STATE") or ROOT / "var")


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise KeyError(f"missing required env var {name}")
    return val


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f)
