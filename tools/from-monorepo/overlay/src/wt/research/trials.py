"""The trial count of a family, read from the append-only registry config/trial_registry.yaml.

Why a config file and not the decision records: the records state the count in prose, which no parser reads
reliably. The YAML is the one number code reads; tests tie every row to its record.

Origin: trading-lab-research `src/wt/research/trials.py` at 217a4eb (trimmed). Removed: the global (equity)
trial history and its count; this repository's registry holds family C only.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from wt.core.config import CONFIG_DIR

REGISTRY = CONFIG_DIR / "trial_registry.yaml"
STATUSES = ("accepted", "proposed")


def family_trial_count(family: str, path: Path | None = None) -> int:
    """The trial count in force for a family (the crypto desk's `C`)."""
    rows = (yaml.safe_load((path or REGISTRY).read_text()).get("families") or {}).get(family) or []
    accepted = [r for r in rows if r.get("status") == "accepted" and isinstance(r.get("total"), int)]
    if not accepted:
        raise ValueError(f"trial registry has no accepted row for family {family}")
    return int(accepted[-1]["total"])
