"""The research registry after the move: the desk's records are here under their old ids, nothing cites a record
that stayed behind, new records start CRY-, and the trial registry agrees with the decision records."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from wt.core.config import load_yaml
from wt.crypto import challengers

ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "research"
ID = re.compile(r"\b((?:CRY-)?(?:HYP|DEC|EXP|LL))-(\d{4})\b")
MOVED = {"HYP": {20, 21, 22, 23, 24}, "DEC": {12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 25, 26, 27},
         "EXP": {16, 17, 18, 19, 20, 21, 22}, "LL": {3, 4, 5, 6}}
FOLDER = {"HYP": "hypotheses", "DEC": "decisions", "EXP": "experiments", "LL": "lessons_learned"}


def present() -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for kind, folder in FOLDER.items():
        for p in (RESEARCH / folder).iterdir():
            m = re.match(rf"((?:CRY-)?{kind})-(\d{{4}})", p.name)
            if m:
                out.setdefault(m.group(1), set()).add(int(m.group(2)))
    return out


def test_the_moved_records_are_all_here_under_their_old_ids_and_nothing_else_is():
    have = present()
    assert {k: have.get(k, set()) for k in MOVED} == MOVED
    assert (RESEARCH / "CRYPTO_SUMMARY.md").exists()
    assert set(have) - set(MOVED) <= {"CRY-DEC", "CRY-HYP", "CRY-EXP", "CRY-LL"}


def test_new_records_start_cry_and_are_numbered_from_one_without_gaps():
    have = present()
    assert have.get("CRY-DEC") and min(have["CRY-DEC"]) == 1
    for kind in ("CRY-DEC", "CRY-HYP", "CRY-EXP", "CRY-LL"):
        ids = sorted(have.get(kind, ()))
        assert ids == list(range(1, len(ids) + 1)), kind
    readme = (RESEARCH / "README.md").read_text()
    assert "start `CRY-`" in readme and "keep their ids" in readme


def test_nothing_of_the_desk_cites_a_record_that_stayed_behind():
    """Every id named in the registry, the desk's code and its configuration is a file here."""
    have = present()
    where = [*(f for f in RESEARCH.rglob("*.md") if f != RESEARCH / "README.md"),     # the index shows the id forms
             *RESEARCH.rglob("*.yaml"), *RESEARCH.rglob("*.json"),
             *(ROOT / "src/wt/crypto").glob("*.py"), *(ROOT / "src/wt/ml").glob("*.py"),
             ROOT / "config/crypto.yaml", ROOT / "config/risk.yaml", ROOT / "config/trial_registry.yaml",
             ROOT / "docs/runbooks/crypto-desk.md"]
    missing = sorted({f"{m.group(1)}-{m.group(2)} in {p.relative_to(ROOT)}" for p in where
                      for m in ID.finditer(p.read_text(errors="replace")) if int(m.group(2)) not in have.get(m.group(1), set())})
    assert missing == []


def test_every_trial_row_names_an_accepted_decision_that_states_the_count():
    rows = yaml.safe_load((ROOT / "config/trial_registry.yaml").read_text())["families"]["C"]
    last = 0
    for row in rows:
        hits = sorted((RESEARCH / "decisions").glob(f"{row['decision']}-*.md"))
        assert len(hits) == 1, row
        text = hits[0].read_text()
        assert re.search(rf"(?<!\d){row['total']}(?!\d)", text), f"{row['decision']} never states {row['total']}"
        assert "Status: ACCEPTED" in text and row["status"] == "accepted" and row["total"] >= last, row
        last = row["total"]


def test_the_challengers_job_finds_the_experiment_it_reads_at_run_time():
    """`wt.crypto.challengers.experiment` globs research/experiments/EXP-0016-*/result.json under the checkout: the
    record has to sit at the same relative path it had."""
    cfg = load_yaml("crypto.yaml")
    assert cfg["learning"]["c1"]["experiment"] == "EXP-0016"
    exp = challengers.experiment(cfg)
    on_disk = json.loads((RESEARCH / "experiments/EXP-0016-crypto-c1/result.json").read_text())
    assert exp == on_disk and exp["id"] == "EXP-0016" and int(exp["end"]) > int(exp["start"]) and exp["data_hash"]
    assert {s["name"] for s in exp["sleeves"]} == {"trend", "break", "dip"}
