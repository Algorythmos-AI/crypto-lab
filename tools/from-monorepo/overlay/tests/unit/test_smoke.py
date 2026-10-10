"""Smoke checks `make deploy` runs against the live venv after a fast-forward. Fast, offline, no state, no data.

Origin: trading-lab-research `tests/unit/test_smoke.py` at 217a4eb (trimmed and rewritten). Removed: the stocks
modules and scripts. The modules, scripts and jobs checked are this repository's.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


@pytest.mark.parametrize("module", ["wt.crypto.cycle", "wt.crypto.snapshot", "wt.crypto.challengers", "wt.crypto.learn",
                                    "wt.crypto.control", "wt.crypto.harvest", "wt.ops.jobs", "wt.ops.deploy",
                                    "wt.ops.units", "wt.ops.backup", "wt.ops.init", "wt.ops.contract"])
def test_the_desk_and_the_host_layer_import(module):
    importlib.import_module(module)


@pytest.mark.parametrize("script", ["crypto_backtest", "gen_crypto_fixture"])
def test_scripts_import(script):
    sys.path.insert(0, str(SCRIPTS))
    try:
        importlib.import_module(script)
    finally:
        sys.path.remove(str(SCRIPTS))


def test_every_job_has_a_known_command_and_belongs_to_the_desk():
    from wt.core.desk import DESKS
    from wt.ops.schedule import JOBS
    assert list(JOBS) == ["crypto", "dashboard-crypto", "crypto-challengers", "crypto-learn"]
    for job in JOBS.values():
        assert job.command[0] == "-m" and job.desk in DESKS and not job.trading, job.name
        importlib.import_module(job.command[1])


def test_the_frozen_configuration_loads_and_the_contract_is_there():
    from wt.core.config import load_yaml
    from wt.crypto import cycle, snapshot
    from wt.ops import contract
    assert cycle.config_hash(load_yaml("crypto.yaml")) == "01aec7d7c569"     # the hash the desk has always stamped
    assert set(load_yaml("risk.yaml")) == {"C", "CT", "CD", "CH"}
    assert snapshot.SCHEMA_ID == "trading-lab/crypto-snapshot" and snapshot.SCHEMA_PATH.exists()
    assert (ROOT / contract.SUMS).read_text() == contract.sums(ROOT)
