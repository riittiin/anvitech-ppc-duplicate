#!/usr/bin/env bash
#
# Pull the latest source of the live app into this duplicate.
#
# A plain copy-paste would overwrite the mirror feature, because it touches
# files the original also owns (engine/storage.py, api/main.py, web/*,
# tests/conftest.py, render.yaml). So the import is kept on its own branch:
#
#   vendor  — nothing but pristine copies of the original, no local changes ever
#   main    — vendor + the mirror feature
#
# Re-syncing replaces the vendor tree with the original's current tree, then
# merges vendor into main. Git then only asks about places where the original
# changed the same lines the mirror touched; everything else merges silently.
#
# Usage:
#   tools/resync-from-original.sh                    # uses ~/Desktop/Anvitech Rebuilt
#   ORIGINAL_REPO=/path/to/repo tools/resync-from-original.sh
#
set -euo pipefail

ORIGINAL="${ORIGINAL_REPO:-$HOME/Desktop/Anvitech Rebuilt}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

die() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

[ -d "$ORIGINAL/.git" ] || die "no git repo at: $ORIGINAL
Set ORIGINAL_REPO to the right path."

[ -z "$(git status --porcelain)" ] \
  || die "this repo has uncommitted changes. Commit or stash them first."

# The original is the source of truth, so it must be at a committed state —
# syncing a half-finished debugging session is exactly the mistake this guards.
if [ -n "$(git -C "$ORIGINAL" status --porcelain)" ]; then
  printf '\nThe original repo has uncommitted work in progress:\n\n' >&2
  git -C "$ORIGINAL" status --short >&2
  die "commit that work first, then re-run this."
fi

REV="$(git -C "$ORIGINAL" rev-parse --short HEAD)"
SUBJECT="$(git -C "$ORIGINAL" log -1 --pretty=%s)"
START_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "Original is at $REV — $SUBJECT"
echo

git checkout --quiet vendor

# Clear the tracked tree, then lay down the original's current tree. Using
# `git archive` means only COMMITTED, tracked files come across: no __pycache__,
# no .venv, and nothing .gitignore excludes (which is every real-data workbook).
git ls-files -z | xargs -0 rm -f
git -C "$ORIGINAL" archive HEAD | tar -x -C "$HERE"

# Real production data, and an output artifact rather than source. Tracked in
# the original's public repo; deliberately not carried into this one.
rm -f "$HERE"/delay-justification-*.xlsx

git add -A
if git diff --cached --quiet; then
  echo "vendor already matches $REV — nothing to sync."
  git checkout --quiet "$START_BRANCH"
  exit 0
fi

echo "Changes coming from the original:"
git diff --cached --stat | tail -30
echo

git commit --quiet -m "vendor: sync with the original at $REV

$SUBJECT"

git checkout --quiet main
echo "Merging into main..."
if ! git merge --no-edit vendor -m "Merge the original @ $REV into the duplicate"; then
  cat >&2 <<'EOF'

The merge stopped on conflicts. That means the original changed the same lines
the mirror feature touches. Resolve them, keeping BOTH sides' intent:

  git status                 # see the conflicted files
  ...resolve...
  git add -A && git commit

Then run the tests before trusting it:

  .venv/bin/python -m pytest -q
EOF
  exit 1
fi

cat <<EOF

Merged cleanly. Now verify before deploying:

  .venv/bin/python -m pytest -q

Expect the inherited suite plus the mirror tests
(tests/test_overlay_store.py, test_overlay_wiring.py, test_mirror_api.py).
EOF
