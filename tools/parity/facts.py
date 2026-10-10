#!/usr/bin/env python3
"""What one side's code says about itself, as JSON on stdout, for the parity proof (tools/parity.sh).

    PYTHONPATH=<root>/src python facts.py <root>

The same file is run under this repository and under the monorepo at the extraction commit. Everything here is
asked of the code under test, never read from a file of expectations: the snapshot schema and the fixture as the
builder generates them, the hashes the journal is stamped with, the limits as the desk loads them, the four job
records, the units they render to, and the names the desk is known by off the host.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
CRYPTO_JOBS = ("crypto", "dashboard-crypto", "crypto-challengers", "crypto-learn")


def main() -> int:
    import wt
    assert Path(wt.__file__).resolve().is_relative_to(ROOT / "src"), wt.__file__
    from wt.core.config import STATE_DIR, load_yaml
    from wt.core.desk import DESKS, desk_of_alert
    from wt.crypto import cycle, risk, sleeves, snapshot
    from wt.ops import jobs, units
    from wt.ops.schedule import JOBS
    from wt.research.trials import family_trial_count

    spec = importlib.util.spec_from_file_location("gen_crypto_fixture", ROOT / "scripts" / "gen_crypto_fixture.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    cfg = load_yaml("crypto.yaml")
    desk = DESKS["crypto"]
    install = Path("/opt/parity")                       # one made-up checkout path for both sides
    rendered = units.render_all(install)
    prefix = {n: next(name for name in rendered if name.endswith(f"-{n}.service") and name.split("-", 1)[1] == f"{n}.service")
              for n in CRYPTO_JOBS}
    out = {
        "schema_id": snapshot.SCHEMA_ID, "schema_version": snapshot.SCHEMA_VERSION,
        "schema_text": json.dumps(snapshot.to_schema(), indent=2) + "\n",
        "fixture_text": json.dumps(gen.build(), indent=1, sort_keys=True) + "\n",
        "committed_schema": snapshot.SCHEMA_PATH.read_text(), "committed_fixture": gen.FIXTURE.read_text(),
        "schema_path": snapshot.SCHEMA_PATH.relative_to(ROOT).as_posix(), "fixture_path": gen.FIXTURE.relative_to(ROOT).as_posix(),
        "config_hash": cycle.config_hash(cfg),
        "sleeve_hashes": {n: sleeves.sleeve_hash(cfg, n) for n in cfg["sleeves"] if n != "common"},
        "limits": {"C": repr(risk.load_limits("C")), "CT": repr(risk.load_sleeve_limits("CT")),
                   "CD": repr(risk.load_desk_limits("CD")), "CH": repr(risk.load_sleeve_limits("CH")) if "CH" in load_yaml("risk.yaml") else None},
        "risk_keys": {k: v for k, v in load_yaml("risk.yaml").items() if k != "B"},
        "trial_family_C": family_trial_count("C"),
        "jobs": {n: dataclasses.asdict(JOBS[n]) for n in CRYPTO_JOBS},
        "all_job_names": list(JOBS),
        "hc_slugs": {n: jobs.hc_slug(JOBS[n]) for n in CRYPTO_JOBS if n in jobs.HC_JOBS},
        "unit_names": sorted(rendered),
        "crypto_units": {n: {"service_name": prefix[n], "service": rendered[prefix[n]],
                             "timer": rendered[prefix[n].replace(".service", ".timer")]} for n in CRYPTO_JOBS},
        "desk": {"name": desk.name, "strategy": desk.strategy, "session": desk.session, "alert_prefix": desk.alert_prefix,
                 "snapshot_schema": desk.snapshot_schema, "state_dir": desk.state_dir.relative_to(STATE_DIR).as_posix(),
                 "kill_file": desk.kill_file.relative_to(STATE_DIR).as_posix(),
                 "chain_flag": desk.chain_flag.relative_to(STATE_DIR).as_posix(),
                 "ledgers": [[n, p.relative_to(STATE_DIR).as_posix()] for n, p in desk.ledgers]},
        "alert_owner": {k: desk_of_alert(k) for k in ("crypto:latch", "crypto:data-stale", "job:crypto", "backup")},
        "publish_out": snapshot.OUT.relative_to(STATE_DIR).as_posix(),
    }
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
