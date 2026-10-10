"""The frozen configuration is the one the desk ran with (CRY-DEC-0001, AGENTS.md rule 3).

`tests/fixtures/monorepo_pins.json` was computed by tools/from-monorepo/drift.py from the files of
trading-lab-research at the extraction commit, with none of this repository's code. Each test here computes the
same hash from this repository's files, through the desk's own functions where the desk has one. A pin that fails
means a number every journal row is stamped with, or a limit that can refuse an entry, is no longer what it was:
that is a new trial and needs a decision record, in the same pull request that moves the pin."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from wt.crypto import cycle, sleeves
from wt.research.trials import family_trial_count

ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT / "tests/fixtures/monorepo_pins.json").read_text())
CFG = yaml.safe_load((ROOT / "config/crypto.yaml").read_text())
RISK = yaml.safe_load((ROOT / "config/risk.yaml").read_text())


def canon(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def test_the_pins_say_where_they_came_from():
    src = PINS["source"]
    assert src["repo"] == "Algorythmos-AI/trading-lab-research" and len(src["commit"]) == 40
    extracted = json.loads((ROOT / "tools/from-monorepo/SOURCE.json").read_text())["commit"]
    assert extracted == src["commit"], ("the pins were not checked at the commit this repository was extracted from: "
                                        "run tools/from-monorepo/drift.py <clone> <commit> --write before extract.sh")


def test_crypto_yaml_is_byte_identical():
    assert hashlib.sha256((ROOT / "config/crypto.yaml").read_bytes()).hexdigest() == PINS["crypto_yaml_sha256"]


def test_strategy_costs_and_quality_are_pinned_section_by_section():
    for section in ("strategy", "costs", "quality"):
        assert canon(CFG[section]) == PINS["sections"][section], section
    assert {k: canon(v) for k, v in CFG.items()} == PINS["sections"]          # and every other section with them


def test_the_hashes_the_journal_is_stamped_with_are_pinned():
    assert cycle.config_hash(CFG) == PINS["config_hash"] == "01aec7d7c569"
    assert {n: sleeves.sleeve_hash(CFG, n) for n in ("trend", "break", "dip")} == PINS["sleeve_hashes"]
    explore = hashlib.sha256(json.dumps(CFG["harvest"]["explore"], sort_keys=True).encode()).hexdigest()[:12]
    assert explore == PINS["harvest_explore_hash"]


def test_the_crypto_risk_keys_are_pinned_and_are_the_only_keys():
    assert set(RISK) == set(PINS["risk_keys"]) == {"C", "CT", "CD", "CH"}
    assert {k: canon(v) for k, v in RISK.items()} == PINS["risk_keys"]


def test_the_trial_registry_holds_family_c_as_it_was():
    reg = yaml.safe_load((ROOT / "config/trial_registry.yaml").read_text())
    assert list(reg) == ["families"] and list(reg["families"]) == ["C"]
    assert canon(reg["families"]["C"]) == PINS["trial_family_C"]["rows"]
    assert family_trial_count("C") == PINS["trial_family_C"]["in_force"] == 12


def test_the_locked_environments_are_the_ones_the_desk_ran_in():
    """Same numbers need the same libraries: every pin of the trading lock is in the origin's lock, and the ML lock
    moved whole."""
    lock = (ROOT / "requirements.lock.txt").read_text()
    for pin in ("numpy==2.5.3", "scipy==1.18.1", "pyyaml==6.0.3", "requests==2.34.2", "jsonschema==4.25.1"):
        assert f"\n{pin} \\\n" in lock, pin
    for absent in ("alpaca", "pandas", "lightgbm", "scikit-learn"):
        assert absent not in lock, absent
