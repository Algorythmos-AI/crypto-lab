"""Host identity (CRY-DEC-0001): the same four jobs and healthchecks slugs, and nothing else shared with the units,
helpers and bookkeeping of the repository this desk came from, which keeps running on the same host.

`tests/fixtures/monorepo_pins.json` holds that repository's names at the extraction commit, taken from its files by
tools/from-monorepo/drift.py: its rendered job units, its static units, its helpers and its directories."""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

from wt.core import desk as desks
from wt.ops import host, jobs, units
from wt.ops.schedule import JOBS

ROOT = Path(__file__).resolve().parents[2]
THEIRS = json.loads((ROOT / "tests/fixtures/monorepo_pins.json").read_text())["host"]
THEIR_NAMES = set(THEIRS["job_units"]) | set(THEIRS["static_units"]) | set(THEIRS["helpers"])
RENDERED = units.render_all(units.INSTALL_ROOT)
OURS = {Path(p).name for p in units.installed_files(ROOT, units.INSTALL_ROOT)}


def test_the_four_jobs_keep_their_names_and_their_healthchecks_slugs():
    assert list(JOBS) == THEIRS["crypto_jobs"] == ["crypto", "dashboard-crypto", "crypto-challengers", "crypto-learn"]
    assert {jobs.hc_slug(JOBS[n]) for n in jobs.HC_JOBS} == {"wt-crypto", "wt-crypto-challengers", "wt-crypto-learn"}
    assert "dashboard-crypto" not in jobs.HC_JOBS                    # as before: the dashboard's watchdog covers it


def test_no_installed_name_is_one_of_theirs_or_matches_their_glob():
    assert len(OURS) == 8 + 5 + 3 and not OURS & THEIR_NAMES
    assert sorted(RENDERED) == sorted(f"cryptolab-{j}.{k}" for j in JOBS for k in ("service", "timer"))
    for name in OURS:
        assert name.startswith(host.UNIT_PREFIX) and not fnmatch.fnmatch(name, "wt-*"), name
    assert units.foreign_names(ROOT) == []
    # their installer lists and enables `wt-*.service` and `wt-*.timer` from its own render: ours match neither
    assert not [n for n in RENDERED if fnmatch.fnmatch(n, "wt-*.service") or fnmatch.fnmatch(n, "wt-*.timer")]
    for theirs in THEIRS["job_units"]:
        assert theirs not in RENDERED


def test_the_install_path_and_the_bookkeeping_are_this_repositorys_own():
    assert str(units.INSTALL_ROOT) == "/home/wt/crypto-lab" != THEIRS["install_root"]
    assert str(units.ETC_DIR) == "/etc/cryptolab" != THEIRS["etc_dir"]
    install = (ROOT / "deploy/bin/cryptolab-install-units").read_text()
    code = "\n".join(line for line in install.splitlines() if not line.lstrip().startswith("#"))
    assert "root=/home/wt/crypto-lab" in code and "etc=/etc/cryptolab" in code and "prefix=cryptolab-" in code
    assert "/etc/wt" not in code and "/home/wt/trading" not in code and "wt-*" not in code
    # the only thing of the shared host it names is the secrets file, which it reads
    assert [w for w in code.split() if "wt-" in w and "cryptolab" not in w] == ["secrets=/run/wt-secrets/env"]
    for helper in ("cryptolab-deploy", "cryptolab-alert"):
        text = (ROOT / "deploy/bin" / helper).read_text()
        assert "/home/wt/trading" not in text and "wt-install-units" not in text


def test_the_installer_can_install_without_enabling_and_never_enables_a_desk_without_state():
    install = (ROOT / "deploy/bin/cryptolab-install-units").read_text()
    assert "--no-enable" in install and "wt.ops.init --check" in install
    enable = install.index("systemctl enable --now")
    assert install.index("wt.ops.init --check") < enable and install.index("if (( enable )); then\n  for t in") < enable
    assert install.count("systemctl enable --now") == 2 and "systemctl disable --now" in install


@pytest.mark.parametrize("name", list(JOBS))
def test_a_job_unit_runs_this_checkout_as_the_job_user_and_pages_through_this_repositorys_alert_unit(name):
    s = RENDERED[f"cryptolab-{name}.service"]
    assert "WorkingDirectory=/home/wt/crypto-lab\n" in s
    assert f"ExecStart=/home/wt/crypto-lab/.venv/bin/python -m wt.ops.jobs run {name}\n" in s
    assert "User=wt\n" in s and "OnFailure=cryptolab-alert@%n.service\n" in s and "Restart=no\n" in s
    assert "EnvironmentFile=/run/wt-secrets/env\n" in s and "Wants=wt-secrets.service " in s        # read, not owned
    assert f"Unit=cryptolab-{name}.service\n" in RENDERED[f"cryptolab-{name}.timer"]
    assert (ROOT / "deploy/systemd/cryptolab-alert@.service").exists()


def test_the_schedules_and_memory_limits_are_the_ones_the_jobs_had():
    t = {n: RENDERED[f"cryptolab-{n}.timer"] for n in JOBS}
    assert "OnCalendar=*:0/15:10 UTC\n" in t["crypto"] and "OnCalendar=*:1/15:00 UTC\n" in t["dashboard-crypto"]
    assert "OnCalendar=Mon..Sun 03:30 America/New_York\n" in t["crypto-challengers"]
    assert "OnCalendar=Mon..Sun 04:30 America/New_York\n" in t["crypto-learn"]
    s = {n: RENDERED[f"cryptolab-{n}.service"] for n in JOBS}
    assert "MemoryMax=512M\nMemorySwapMax=128M\n" in s["crypto"] and "MemoryMax=512M\n" in s["dashboard-crypto"]
    assert "MemoryHigh=1G\nMemoryMax=1536M\n" in s["crypto-challengers"] and "MemoryHigh=1536M\nMemoryMax=2G\n" in s["crypto-learn"]
    assert "StartLimitIntervalSec=0\n" in s["crypto"] and "StartLimitBurst=3\n" in s["crypto-learn"]
    assert set(units.MEMORY) == set(JOBS)


def test_the_backup_units_are_this_repositorys_and_fire_in_a_quiet_hour():
    svc = (ROOT / "deploy/systemd/cryptolab-backup.service").read_text()
    assert "ExecStart=/home/wt/crypto-lab/.venv/bin/python -m wt.ops.backup nightly\n" in svc
    assert "WorkingDirectory=/home/wt/crypto-lab\n" in svc and "OnFailure=cryptolab-alert@%n.service\n" in svc
    assert "OnCalendar=*-*-* 05:20 UTC\n" in (ROOT / "deploy/systemd/cryptolab-backup.timer").read_text()
    assert "Unit=cryptolab-restore-test.service\n" in (ROOT / "deploy/systemd/cryptolab-restore-test.timer").read_text()


def test_the_fingerprint_changes_with_a_helper_and_with_the_install_path(tmp_path):
    a = units.fingerprint(ROOT, units.INSTALL_ROOT)
    assert a == units.fingerprint(ROOT, units.INSTALL_ROOT) and len(a.splitlines()) == len(OURS)
    assert a != units.fingerprint(ROOT, Path("/home/wt/elsewhere"))
    assert all(line.split("  ")[1].startswith(("/etc/systemd/system/cryptolab-", "/usr/local/bin/cryptolab-")) for line in a.splitlines())


def test_units_diff_names_what_is_missing_or_different(tmp_path):
    (tmp_path / "cryptolab-crypto.service").write_text(RENDERED["cryptolab-crypto.service"])
    (tmp_path / "cryptolab-crypto.timer").write_text("old\n")
    d = units.diff(RENDERED, tmp_path)
    assert "cryptolab-crypto.timer: differs from the code" in d and "cryptolab-crypto-learn.service: not installed" in d
    assert not [x for x in d if x.startswith("cryptolab-crypto.service")]


def test_the_host_kinds(monkeypatch):
    monkeypatch.setenv("WT_HOST", "local")
    local = host.current()
    assert local.kind == "local" and not local.loaded(JOBS["crypto"])
    assert local.running_jobs(list(JOBS.values())) == {f"cryptolab-{n}": False for n in JOBS}
    monkeypatch.setenv("WT_HOST", "systemd")
    sd = host.current()
    assert sd.kind == "systemd" and host.unit(JOBS["crypto-learn"]) == "cryptolab-crypto-learn"
    monkeypatch.setattr(host, "_run", lambda cmd, timeout=10.0: (_ for _ in ()).throw(OSError("no systemctl")))
    assert sd.running_jobs([JOBS["crypto"]]) == {"systemctl": True}        # can't tell: fail closed
    seen = []

    def fake(cmd, timeout=10.0):
        seen.append(cmd[-1])
        return type("R", (), {"returncode": 0, "stdout": "active\n" if "learn" in cmd[-1] else "inactive\n"})()
    monkeypatch.setattr(host, "_run", fake)
    assert sd.running_jobs([JOBS["crypto"], JOBS["crypto-learn"]]) == {"cryptolab-crypto": False, "cryptolab-crypto-learn": True}
    assert seen == ["cryptolab-crypto.service", "cryptolab-crypto-learn.service"]


def test_the_desk_record_is_the_one_the_desk_had_and_alert_keys_without_its_prefix_are_the_hosts():
    d = desks.DESKS["crypto"]
    assert list(desks.DESKS) == ["crypto"]
    assert (d.strategy, d.session, d.alert_prefix, d.snapshot_schema) == ("C", "always_open", "crypto:", "trading-lab/crypto-snapshot")
    assert d.state_dir == desks.CRYPTO_DIR and d.kill_file == d.state_dir / "KILL" and d.chain_flag == d.state_dir / "chain-broken"
    assert d.ledgers == (("crypto-harvest", d.state_dir / "harvest" / "harvest_journal.jsonl"),
                         ("crypto", d.state_dir / "crypto_journal.jsonl")) and d.journal.name == "crypto_journal.jsonl"
    assert desks.desk_of_alert("crypto:latch") == "crypto"
    assert desks.desk_of_alert("job:crypto") == desks.desk_of_alert("backup") == "host" != d.name
