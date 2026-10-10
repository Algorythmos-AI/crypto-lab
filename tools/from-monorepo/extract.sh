#!/usr/bin/env bash
# Build this repository from the monorepo, repeatably.
#
#   tools/from-monorepo/extract.sh <monorepo clone> <commit> [--out DIR]
#
# What it does, in order:
#   1. makes a scratch copy of the monorepo that holds <commit> and nothing else (no other branch, no tag);
#   2. runs `git filter-repo` on the scratch copy with the allowlist (paths.txt): only the listed paths survive,
#      with their history; commit messages, authors and dates are kept as they were;
#   3. starts a new repository in DIR from that filtered history alone (nothing unreachable comes along);
#   4. lays overlay/ on top (everything that is not a whole-file move) together with this directory, as one commit;
#   5. applies patches/ (the deliberate changes to files that moved whole), as a second commit, and tags the
#      snapshot contract (contracts/) by the schema's own hash;
#   6. checks the result: no overlay file shadows a moved path, the two new commits carry the repository's
#      identity, and the whole history holds none of the monorepo's restricted markers and no path of the
#      machine it was built on.
#
# It never writes to the monorepo clone and never pushes anywhere. DIR must not exist yet (default:
# ./crypto-lab-<commit, 12 digits>). Run it again at a newer commit after `drift.py` says what, if anything,
# has to be redone by hand first.
#
# SOURCE_DATE_EPOCH, when set, is the date of the two commits this script makes: two runs with the same
# inputs then give the same commit ids. Without it they carry the time of the run and only the trees are equal.
set -euo pipefail

die() { echo "extract: $*" >&2; exit 2; }
usage() { sed -n '2,5p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
(( $# >= 2 )) || usage
mono="$(cd "$1" 2>/dev/null && pwd -P)" || die "not a directory: $1"
commit="$2"; shift 2
out=""
while (( $# )); do
  case "$1" in
    --out) [[ $# -ge 2 ]] || usage; out="$2"; shift 2 ;;
    *) usage ;;
  esac
done

git -C "$mono" rev-parse --git-dir >/dev/null 2>&1 || die "$mono is not a git repository"
git filter-repo --version >/dev/null 2>&1 || die "git-filter-repo is not installed"
sha="$(git -C "$mono" rev-parse --verify --quiet "${commit}^{commit}")" || die "$commit is not a commit in $mono"
[[ -n "$out" ]] || out="$PWD/crypto-lab-${sha:0:12}"
[[ ! -e "$out" ]] || die "$out already exists; choose another --out (nothing is ever overwritten)"
mkdir -p "$(dirname "$out")"
out="$(cd "$(dirname "$out")" && pwd -P)/$(basename "$out")"
case "$here/" in "$out"/*) die "--out contains this script; choose a directory outside this repository" ;; esac
case "$out/" in "$mono"/*) die "--out is inside the monorepo clone; it is never written to" ;; esac

tmp="$(mktemp -d "${TMPDIR:-/tmp}/extract.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
identity=(-c user.name=skalaliya -c user.email=skalaliya@gmail.com)
if [[ -n "${SOURCE_DATE_EPOCH:-}" ]]; then
  export GIT_AUTHOR_DATE="@${SOURCE_DATE_EPOCH} +0000" GIT_COMMITTER_DATE="@${SOURCE_DATE_EPOCH} +0000"
fi

echo "extract: $mono at ${sha:0:12} -> $out"

# 1. A scratch copy that holds <commit> and no other ref.
git init -q -b main "$tmp/mono"
git -C "$tmp/mono" fetch -q --no-tags "$mono" '+refs/heads/*:refs/src/heads/*' '+refs/remotes/*:refs/src/remotes/*'
git -C "$tmp/mono" cat-file -e "${sha}^{commit}" 2>/dev/null \
  || die "${sha:0:12} is not reachable from a branch of $mono (fetch it there first)"
git -C "$tmp/mono" update-ref refs/heads/main "$sha"
git -C "$tmp/mono" for-each-ref --format='%(refname)' refs/src | while read -r ref; do
  git -C "$tmp/mono" update-ref -d "$ref"
done

# 2. Only the allowlisted paths survive, with their history.
grep -vE '^[[:space:]]*(#|$)' "$here/paths.txt" > "$tmp/paths"
before="$(git -C "$tmp/mono" rev-list --count refs/heads/main)"
(cd "$tmp/mono" && git filter-repo --force --quiet --refs refs/heads/main --preserve-commit-hashes \
   --paths-from-file "$tmp/paths")
tip="$(git -C "$tmp/mono" rev-parse refs/heads/main)"
kept="$(git -C "$tmp/mono" rev-list --count refs/heads/main)"
moved="$(git -C "$tmp/mono" ls-tree -r --name-only refs/heads/main | wc -l | tr -d ' ')"
(( moved > 0 )) || die "the allowlist matched nothing at ${sha:0:12}"
echo "extract: history filtered: $kept of $before commits touch the allowlist; $moved files move whole"

# 3. The new repository starts from the filtered history alone.
git init -q -b main "$out"
git -C "$out" config user.name skalaliya
git -C "$out" config user.email skalaliya@gmail.com
git -C "$out" fetch -q --no-tags "$tmp/mono" refs/heads/main
git -C "$out" reset -q --hard FETCH_HEAD

# 4. The overlay and this directory, as one commit. An overlay file may never shadow a moved one.
(cd "$here/overlay" && find . -type f ! -name .DS_Store ! -path '*/__pycache__/*' | sed 's|^\./||' | LC_ALL=C sort) > "$tmp/overlay.list"
git -C "$out" ls-files | LC_ALL=C sort > "$tmp/moved.list"
clash="$(LC_ALL=C comm -12 "$tmp/overlay.list" "$tmp/moved.list")"
[[ -z "$clash" ]] || die "overlay/ holds paths that move whole from the monorepo:"$'\n'"$clash"
tar -C "$here/overlay" --exclude .DS_Store --exclude __pycache__ -cf - . | tar -C "$out" -xf -
mkdir -p "$out/tools/from-monorepo"
tar -C "$here" --exclude .DS_Store --exclude __pycache__ --exclude SOURCE.json -cf - . | tar -C "$out/tools/from-monorepo" -xf -
cat > "$out/tools/from-monorepo/SOURCE.json" <<EOF
{
 "repo": "Algorythmos-AI/trading-lab-research",
 "commit": "$sha",
 "filtered_history_tip": "$tip",
 "filtered_commits": $kept,
 "files_moved_whole": $moved
}
EOF
git -C "$out" add -A
git -C "$out" "${identity[@]}" commit -q -m "chore: the standalone repository around the extracted desk

Everything below this commit is the crypto desk's own files from
trading-lab-research at ${sha:0:12}, with their history, filtered with the
allowlist tools/from-monorepo/paths.txt.

This commit adds what is not a whole-file move (tools/from-monorepo/overlay):
trimmed copies of the shared modules the desk imports, the host layer under
its own unit prefix, the snapshot contract, the tests of all of it,
governance, CI, and the extraction tooling itself. Where each copy came from
and what was removed from it: tools/from-monorepo/copies.json."

# 5. The deliberate changes to files that moved whole, as a second commit.
shopt -s nullglob
patches=("$here"/patches/*.patch)
for p in ${patches[@]+"${patches[@]}"}; do          # the long form: bash 3.2 with `set -u` and no patch
  git -C "$out" apply --index "$p" \
    || die "patches/$(basename "$p") does not apply at ${sha:0:12}: remake it against the new file (drift.py says which)"
done
if (( ${#patches[@]} )); then
  git -C "$out" "${identity[@]}" commit -q -m "chore: the deliberate changes to the moved files

tools/from-monorepo/patches, in order:
$(for p in "${patches[@]}"; do echo "  $(basename "$p")"; done)

Each is a difference from trading-lab-research that this repository means
to have. They are listed, with the reason for each, in
research/decisions/CRY-DEC-0001-standalone-repository.md."
fi

# The snapshot contract is tagged by what it is: the consumer vendors contracts/ at this tag and checks the
# hashes. The name is the schema's own sha256, so a changed schema can never hide behind an old tag.
if [[ -f "$out/contracts/crypto.schema.json" ]]; then
  if command -v sha256sum >/dev/null 2>&1; then sum=(sha256sum); else sum=(shasum -a 256); fi
  schema_sha="$("${sum[@]}" "$out/contracts/crypto.schema.json" | cut -c1-64)"
  contract_tag="contract-${schema_sha:0:12}"
  git -C "$out" "${identity[@]}" tag -a "$contract_tag" -m "The snapshot contract: schema id trading-lab/crypto-snapshot

$(cat "$out/contracts/SHA256SUMS")" HEAD
fi

# 6. Checks on the result.
git -C "$out" status --porcelain | grep -q . && die "the new repository is not clean after its commits"
# The markers are spelled in pieces so that this file does not contain them: the check below reads this
# repository's whole history, and this file is part of it.
markers="$(printf 'w%srior|SP%s-0001|knowl%s/|research/sp%s' ar EC edge ecs)"
hits="$(git -C "$out" log -p --all | grep -ciE "$markers" || true)"
if [[ "$hits" != 0 ]]; then
  git -C "$out" log -p --all | grep -niE "$markers" | cut -c1-160 | head -20 >&2
  die "the history holds $hits line(s) with a restricted marker: STOP, do not publish $out"
fi
# Nothing of the machine this ran on: a home directory or a scratch directory in a generated file (a lockfile's
# notes, a log pasted into a record) would be published with the history. Spelled in pieces, as above.
machine="$(printf '/Us%s/|/private/(tmp|var)/|/var/fold%s/' ers ers)"
hits="$(git -C "$out" log -p --all | grep -cE "$machine" || true)"
if [[ "$hits" != 0 ]]; then
  git -C "$out" log -p --all | grep -nE "$machine" | cut -c1-160 | head -20 >&2
  die "the history holds $hits line(s) with a path of a developer's machine: fix the file in overlay/ and run again"
fi
new="$(git -C "$out" rev-list --count "$tip"..HEAD)"
bad="$(git -C "$out" log --format='%an <%ae>|%cn <%ce>' "$tip"..HEAD | grep -vc '^skalaliya <skalaliya@gmail.com>|skalaliya <skalaliya@gmail.com>$' || true)"
[[ "$bad" == 0 ]] || die "a commit made here does not carry the repository's identity"
trailers="$(git -C "$out" log --format=%B "$tip"..HEAD | grep -ci '^co-authored-by:' || true)"
[[ "$trailers" == 0 ]] || die "a commit made here carries a Co-Authored-By trailer"

echo "extract: done"
echo "  repository     $out"
echo "  from           trading-lab-research ${sha}"
echo "  history        $kept filtered commits (tip ${tip:0:12}) + $new made here"
echo "  tree           $(git -C "$out" rev-parse 'HEAD^{tree}')"
echo "  contract tag   ${contract_tag:-none}"
echo "  restricted     0 marker lines in git log -p --all; 0 lines with a path of this machine"
echo "next: cd $out && make bootstrap && make ci && make bootstrap-ml && make test-ml && tools/parity.sh $mono"
