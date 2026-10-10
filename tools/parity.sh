#!/usr/bin/env bash
# The parity proof: this repository against the monorepo at the commit it was extracted from.
#
#   tools/parity.sh <monorepo clone> [--quick] [--keep]
#
# It exports the monorepo at the commit recorded in tools/from-monorepo/SOURCE.json into a scratch directory
# (the clone itself is only read) and shows, with this repository's own environments (.venv, .venv-ml):
#
#   1 files     every file that moved whole is byte-identical, except the ones the patches change, and those
#               are exactly the monorepo's file with the patch applied
#   2 config    the frozen configuration: crypto.yaml byte-identical; the crypto risk keys and the trial family
#               are the monorepo's lines; both sides' own code computes the same hashes and loads the same limits
#   3 contract  the snapshot schema (to_schema()) and the generated fixture are byte-identical, and so are the
#               committed copies; the schema id is unchanged
#   4 replay    the same Kraken fixture data through the bar cycle, the sleeves, the harvest, the challengers,
#               the learning run, the owner's controls and the snapshot builder leaves identical journals, books
#               and snapshot bodies (tools/parity/replay.py says what is pinned and why)
#   5 tests     the moved tests are the same tests on both sides (same ids, same count) and pass here: `make ci`
#               and the ML suite in its own environment
#   6 units     the four jobs keep their names, records and healthchecks slugs; the rendered units share no name
#               with the monorepo's and differ from its text only in the prefix, the checkout path and the
#               product name
#   7 history   the whole history of this repository holds none of the monorepo's restricted markers and no path
#               of a developer's machine
#
# --quick   a short replay (48 cycles, 3 random-entry controls) and no run of the moved tests inside the
#           monorepo export. The default replay is 240 cycles (two and a half days) with 24 controls.
# --keep    leave the scratch directory (.parity/) in place for a look at what was compared.
#
# Exit 0 only when every item holds. Nothing here writes outside .parity/ and nothing is pushed anywhere.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
usage() { sed -n '2,5p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }
(( $# >= 1 )) || usage
mono="$(cd "$1" 2>/dev/null && pwd -P)" || { echo "parity: not a directory: $1" >&2; exit 2; }
shift
quick=0; keep=0
while (( $# )); do
  case "$1" in --quick) quick=1 ;; --keep) keep=1 ;; *) usage ;; esac
  shift
done
py="$root/.venv/bin/python"; pyml="$root/.venv-ml/bin/python"
[[ -x "$py" ]] || { echo "parity: no .venv here (make bootstrap)" >&2; exit 2; }
commit="$("$py" -c "import json,sys; print(json.load(open(sys.argv[1]))['commit'])" "$root/tools/from-monorepo/SOURCE.json")"
git -C "$mono" cat-file -e "${commit}^{commit}" 2>/dev/null || { echo "parity: $mono does not have ${commit:0:12}" >&2; exit 2; }

work="$root/.parity"
rm -rf "$work"; mkdir -p "$work/monorepo" "$work/home"
(( keep )) || trap 'rm -rf "$work"' EXIT
git -C "$mono" archive "$commit" | tar -x -C "$work/monorepo" --exclude watchlist --exclude 'dashboard/public'
m="$work/monorepo"

fail=0
ok()  { printf '  PASS  %s\n' "$*"; }
bad() { printf '  FAIL  %s\n' "$*"; fail=1; }
say() { printf '\n== %s ==\n' "$*"; }
clean_env=(env -i PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin HOME="$work/home" TZ=UTC PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1)
on_lab()  { (cd "$root" && "${clean_env[@]}" PYTHONPATH="$root/src" "$@"); }
on_mono() { (cd "$m" && "${clean_env[@]}" PYTHONPATH="$m/src" "$@"); }

echo "parity: crypto-lab $(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo '(no git)') against trading-lab-research ${commit}"
echo "parity: both sides run in this repository's environments: $("$py" --version 2>&1), $(basename "$py") from .venv"

# ---------------------------------------------------------------------------------------------------------------
say "1 files: what moved whole is byte-identical, apart from the patches"
"$py" - "$root" "$m" <<'PY' || fail=1
import shutil, subprocess, sys, tempfile
from pathlib import Path
root, mono = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root / "tools" / "from-monorepo"))
import overlay
entries = overlay.allowlist()
lab = [f for f in (overlay.tracked() or []) if overlay.moved(f, entries)]
theirs = sorted(p.relative_to(mono).as_posix() for p in mono.rglob("*") if p.is_file() and overlay.moved(p.relative_to(mono).as_posix(), entries))
rc = 0
def line(ok, text):
    global rc
    print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    rc |= 0 if ok else 1
line(sorted(lab) == theirs, f"the same {len(theirs)} paths are on both sides (allowlist: {len(entries)} entries)")
differ = sorted(f for f in lab if (mono / f).exists() and (mono / f).read_bytes() != (root / f).read_bytes())
patches = sorted((root / "tools/from-monorepo/patches").glob("*.patch"))
touched = sorted({l[6:] for p in patches for l in p.read_text().splitlines() if l.startswith("+++ b/")})
line(differ == touched, f"{len(lab) - len(differ)} files byte-identical; {len(differ)} differ, and they are the {len(touched)} the patches name")
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run(["git", "init", "-q", tmp], check=True)
    for f in touched:
        (Path(tmp) / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mono / f, Path(tmp) / f)
    applied = all(subprocess.run(["git", "-C", tmp, "apply", str(p)], capture_output=True).returncode == 0 for p in patches)
    same = applied and all((Path(tmp) / f).read_bytes() == (root / f).read_bytes() for f in touched)
line(same, f"the monorepo's files with the {len(patches)} patches applied are this repository's files, byte for byte")
for f in differ:
    print(f"        patched: {f}")
sys.exit(rc)
PY

# ---------------------------------------------------------------------------------------------------------------
say "2 config and 3 contract and 6 units: what each side's own code says"
on_lab  "$py" "$root/tools/parity/facts.py" "$root" > "$work/facts-lab.json"  2> "$work/facts-lab.err"  || bad "facts on this repository (see $work/facts-lab.err)"
on_mono "$py" "$root/tools/parity/facts.py" "$m"    > "$work/facts-mono.json" 2> "$work/facts-mono.err" || bad "facts on the monorepo (see $work/facts-mono.err)"
"$py" - "$root" "$m" "$work" <<'PY' || fail=1
import hashlib, json, sys
from pathlib import Path
root, mono, work = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(root / "tools" / "from-monorepo"))
import drift
a, b = json.loads((work / "facts-lab.json").read_text()), json.loads((work / "facts-mono.json").read_text())
rc = 0
def line(ok, text):
    global rc
    print(f"  {'PASS' if ok else 'FAIL'}  {text}")
    rc |= 0 if ok else 1
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()[:16]

print("  -- 2 config")
line((root / "config/crypto.yaml").read_bytes() == (mono / "config/crypto.yaml").read_bytes(), "config/crypto.yaml is byte-identical")
copies = {c["path"]: c for c in json.loads((root / "tools/from-monorepo/copies.json").read_text())["copies"]}
for name in ("config/risk.yaml", "config/trial_registry.yaml"):
    want = drift.split((mono / name).read_text(), copies[name]["rule"])
    header = "".join(h + "\n" for h in copies[name]["rule"]["header"])
    body = (root / name).read_text()[len(header):]
    line((root / name).read_text() == want and body in (mono / name).read_text(),
         f"{name}: after this repository's header, every line is the monorepo's, in one piece ({len(body.splitlines())} lines)")
line(a["risk_keys"] == b["risk_keys"] and set(a["risk_keys"]) == {"C", "CT", "CD", "CH"}, f"the crypto risk keys parse to the same values: {sorted(a['risk_keys'])}")
line(a["limits"] == b["limits"], "wt.crypto.risk loads the same limits on both sides (C, CT, CD, CH)")
line(a["config_hash"] == b["config_hash"], f"cycle.config_hash: {a['config_hash']} on both sides")
line(a["sleeve_hashes"] == b["sleeve_hashes"], f"sleeves.sleeve_hash: {a['sleeve_hashes']}")
line(a["trial_family_C"] == b["trial_family_C"], f"family C trial count in force: {a['trial_family_C']}")

print("  -- 3 contract")
line(a["schema_id"] == b["schema_id"] == "trading-lab/crypto-snapshot" and a["schema_version"] == b["schema_version"], f"schema id {a['schema_id']!r}, version {a['schema_version']}, on both sides")
line(a["schema_text"] == b["schema_text"], f"to_schema() is byte-identical ({len(a['schema_text'])} bytes, sha256 {sha(a['schema_text'])})")
line(a["fixture_text"] == b["fixture_text"], f"the generated fixture is byte-identical ({len(a['fixture_text'])} bytes, sha256 {sha(a['fixture_text'])})")
line(a["committed_schema"] == a["schema_text"] == b["committed_schema"], f"the committed schemas are that text ({a['schema_path']} here, {b['schema_path']} there)")
line(a["committed_fixture"] == a["fixture_text"] == b["committed_fixture"], f"the committed fixtures are that text ({a['fixture_path']} here, {b['fixture_path']} there)")
import subprocess
tag = "contract-" + hashlib.sha256(a["committed_schema"].encode()).hexdigest()[:12]
at_tag = subprocess.run(["git", "-C", str(root), "show", f"{tag}:contracts/crypto.schema.json"], capture_output=True, text=True)
line(at_tag.returncode == 0 and at_tag.stdout == a["committed_schema"], f"the contract is tagged by its schema's hash: {tag}")

print("  -- 6 units and host identity")
line(a["jobs"] == b["jobs"], "the four job records are equal field for field: " + ", ".join(a["jobs"]))
line(a["hc_slugs"] == b["hc_slugs"], f"healthchecks slugs: {sorted(a['hc_slugs'].values())}")
line(a["desk"] == b["desk"] and a["publish_out"] == b["publish_out"], f"the desk record is the same: state {a['desk']['state_dir']}, journal {a['desk']['ledgers'][-1][1]}, alert prefix {a['desk']['alert_prefix']!r}")
line(not set(a["unit_names"]) & set(b["unit_names"]), f"no rendered unit name is shared: {len(a['unit_names'])} here (cryptolab-*), {len(b['unit_names'])} there (wt-*)")
host = json.loads((root / "tests/fixtures/monorepo_pins.json").read_text())["host"]
theirs = set(host["job_units"]) | set(host["static_units"]) | set(host["helpers"])
ours = set(a["unit_names"]) | {p.name for p in (root / "deploy/systemd").iterdir()} | {p.name for p in (root / "deploy/bin").iterdir()}
real = {p.name for p in (mono / "deploy/oci/systemd").iterdir()} | {p.name for p in (mono / "deploy/oci/bin").iterdir()} | set(b["unit_names"])
line(theirs == real, f"the monorepo's installed names in the test pins are the ones its tree has ({len(real)})")
line(not ours & real and all(n.startswith("cryptolab-") for n in ours) and not any(n.startswith("wt-") for n in ours),
     f"nothing this repository installs ({len(ours)} names) shares a name with what the monorepo installs")
swaps = (("cryptolab-", "wt-"), ("Crypto Lab", "Trading Lab"))
kinds = set()
for n in a["crypto_units"]:
    for part in ("service", "timer"):
        x, y = a["crypto_units"][n][part], b["crypto_units"][n][part]
        for old, new in swaps:
            x = x.replace(old, new)
        if x != y:
            line(False, f"{n}.{part} differs beyond the prefix and the product name")
        for la, lb in zip(a["crypto_units"][n][part].splitlines(), b["crypto_units"][n][part].splitlines()):
            if la != lb:
                kinds.add(la.split("=")[0])
line(True, f"the eight unit files differ from the monorepo's only in these lines: {sorted(kinds)}, and only by the prefix and the product name (the checkout path is the argument they are rendered with)")
line(a["alert_owner"]["crypto:latch"] == b["alert_owner"]["crypto:latch"] == "crypto", "alert keys with the desk's prefix belong to the desk on both sides")
print(f"        intended difference: a key without it belongs to {a['alert_owner']['job:crypto']!r} here, {b['alert_owner']['job:crypto']!r} there (CRY-DEC-0001, 5)")
sys.exit(rc)
PY

# ---------------------------------------------------------------------------------------------------------------
say "4 replay: the same market through both desks"
if (( quick )); then cycles=48; controls=3; else cycles=240; controls=24; fi
echo "  $cycles bar cycles from 2026-10-04 00:00 UTC, $controls random-entry controls per challenger; both sides in parallel ..."
on_lab  env WT_STATE="$work/replay-lab/var"  "$py" "$root/tools/parity/replay.py" --root "$root" --out "$work/replay-lab"  --cycles "$cycles" --controls "$controls" > "$work/replay-lab.log" 2>&1 &
p1=$!
on_mono env WT_STATE="$work/replay-mono/var" "$py" "$root/tools/parity/replay.py" --root "$m"    --out "$work/replay-mono" --cycles "$cycles" --controls "$controls" > "$work/replay-mono.log" 2>&1 &
p2=$!
wait "$p1"; r1=$?
wait "$p2"; r2=$?
if (( r1 == 0 && r2 == 0 )); then
  ok "both replays ran to the end"
  if "$py" "$root/tools/parity/compare.py" "$work/replay-lab" "$work/replay-mono"; then
    ok "journals, books, stored bars, observations, alert and heartbeat state and every snapshot body are byte-identical"
  else
    bad "the replays differ (above)"
  fi
  if diff <(sed 1d "$work/replay-lab.log") <(sed 1d "$work/replay-mono.log") > "$work/replay-log.diff"; then
    ok "what the jobs printed is identical too ($(wc -l < "$work/replay-lab.log" | tr -d ' ') lines)"
  else
    bad "the jobs' own output differs ($work/replay-log.diff)"
  fi
else
  bad "a replay failed (this repository: exit $r1, monorepo: exit $r2; logs in $work)"
  tail -5 "$work/replay-lab.log" "$work/replay-mono.log" 2>/dev/null | sed 's/^/        /'
fi

# ---------------------------------------------------------------------------------------------------------------
say "5 tests: the same tests on both sides, passing here"
moved_tests() { (cd "$1" && ls tests/unit/test_crypto*.py tests/unit/test_ml_*.py); }
collect() { # <root> -> sorted test ids of the moved pytest files
  local files=() f
  while IFS= read -r f; do files+=("$f"); done < <(moved_tests "$1")     # no mapfile: macOS ships bash 3.2
  (cd "$1" && "${clean_env[@]}" PYTHONPATH="$1/src" "$py" -m pytest --collect-only -q -o addopts= -p no:cacheprovider "${files[@]}" 2>/dev/null | grep '::' | LC_ALL=C sort)
}
collect "$root" > "$work/ids-lab.txt"; collect "$m" > "$work/ids-mono.txt"
n_lab=$(wc -l < "$work/ids-lab.txt" | tr -d ' '); n_mono=$(wc -l < "$work/ids-mono.txt" | tr -d ' ')
if [[ "$n_lab" == "$n_mono" && "$n_lab" != 0 ]] && cmp -s "$work/ids-lab.txt" "$work/ids-mono.txt"; then
  ok "moved pytest tests: $n_lab here, $n_mono there, the same ids, in $(moved_tests "$root" | wc -l | tr -d ' ') files"
else
  bad "moved pytest tests: $n_lab here, $n_mono there"; diff "$work/ids-lab.txt" "$work/ids-mono.txt" | head -10 | sed 's/^/        /'
fi
(cd "$root" && cut -d: -f1 "$work/ids-lab.txt" | uniq -c | sed 's/^/        /')
if [[ -x "$pyml" ]]; then
  count_ml() { (cd "$1" && "${clean_env[@]}" PYTHONPATH="$1/src" WT_ML_REQUIRED=1 "$pyml" -c "
import unittest
def ids(s):
    for t in s:
        if isinstance(t, unittest.TestSuite): yield from ids(t)
        else: yield t.id()
print('\n'.join(sorted(ids(unittest.defaultTestLoader.discover('tests/ml', top_level_dir='.')))))" 2>/dev/null); }
  count_ml "$root" > "$work/ml-lab.txt"; count_ml "$m" > "$work/ml-mono.txt"
  k_lab=$(wc -l < "$work/ml-lab.txt" | tr -d ' '); k_mono=$(wc -l < "$work/ml-mono.txt" | tr -d ' ')
  if [[ "$k_lab" == "$k_mono" && "$k_lab" != 0 ]] && cmp -s "$work/ml-lab.txt" "$work/ml-mono.txt"; then
    ok "moved ML tests (tests/ml): $k_lab here, $k_mono there, the same ids"
  else
    bad "moved ML tests: $k_lab here, $k_mono there"
  fi
  if (cd "$root" && "${clean_env[@]}" PYTHONPATH="$root/src" WT_ML_REQUIRED=1 "$pyml" -m unittest discover -s tests/ml -t . > "$work/ml-run.txt" 2>&1); then
    ok "the ML suite passes here in .venv-ml: $(grep -E '^Ran ' "$work/ml-run.txt")"
  else
    bad "the ML suite fails here ($work/ml-run.txt)"; tail -5 "$work/ml-run.txt" | sed 's/^/        /'
  fi
else
  bad "no .venv-ml here: the ML suite was NOT run (make bootstrap-ml)"
fi
if (cd "$root" && "${clean_env[@]}" PYTHONPATH="$root/src" make -s ci PY="$py" > "$work/ci.txt" 2>&1); then
  total=$( (cd "$root" && "${clean_env[@]}" PYTHONPATH="$root/src" "$py" -m pytest --collect-only -q -o addopts= -p no:cacheprovider 2>/dev/null | grep -c '::') )
  ok "make ci passes here (ruff, mypy --strict, pytest): $total tests collected, of which $n_lab moved; last line: $(grep -E 'passed|failed|error' "$work/ci.txt" | tail -1)"
else
  bad "make ci fails here ($work/ci.txt)"; tail -15 "$work/ci.txt" | sed 's/^/        /'
fi
if (( ! quick )); then
  theirs=()
  while IFS= read -r f; do theirs+=("$f"); done < <(moved_tests "$m")
  if (cd "$m" && "${clean_env[@]}" PYTHONPATH="$m/src" "$py" -m pytest -q -o addopts= -p no:cacheprovider "${theirs[@]}" > "$work/mono-moved.txt" 2>&1); then
    ok "for reference, the same tests pass inside the monorepo export in the same environment: $(tail -1 "$work/mono-moved.txt")"
  else
    bad "the moved tests fail inside the monorepo export in this environment ($work/mono-moved.txt)"; tail -8 "$work/mono-moved.txt" | sed 's/^/        /'
  fi
fi

# ---------------------------------------------------------------------------------------------------------------
say "7 history: no restricted marker anywhere in this repository's history"
if git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
  # Spelled in pieces so that this file, which is part of the history it reads, does not contain them.
  markers="$(printf 'w%srior|SP%s-0001|knowl%s/|research/sp%s' ar EC edge ecs)"
  hits="$(git -C "$root" log -p --all | grep -ciE "$markers" || true)"
  commits="$(git -C "$root" rev-list --all --count)"
  if [[ "$hits" == 0 ]]; then ok "git log -p --all over $commits commits: 0 lines match"; else bad "git log -p --all: $hits line(s) match"; fi
  # Nor a path of the machine a file was generated on (a home or a scratch directory): the history is published.
  machine="$(printf '/Us%s/|/private/(tmp|var)/|/var/fold%s/' ers ers)"
  local_hits="$(git -C "$root" log -p --all | grep -cE "$machine" || true)"
  if [[ "$local_hits" == 0 ]]; then
    ok "no line of the history holds a path of a developer's machine"
  else
    bad "git log -p --all: $local_hits line(s) hold a path of a developer's machine; only a new extraction (tools/from-monorepo/extract.sh) removes them from the history"
  fi
  dirty="$(git -C "$root" status --porcelain | grep -vc '^?? .parity/' || true)"
  [[ "$dirty" == 0 ]] || echo "        note: the working tree has $dirty uncommitted change(s); the history check reads commits only"
else
  bad "not a git repository: the history was not read"
fi

echo
if (( fail )); then echo "PARITY: FAILED (see FAIL lines above)"; else echo "PARITY: every item holds"; fi
(( keep )) && echo "scratch kept in $work"
exit "$fail"
