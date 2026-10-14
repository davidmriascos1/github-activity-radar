#!/usr/bin/env bash
# Blobless-clone every repo in a URL list, for poking at with the single-repo
# tools (radar/staging.py, radar/incumbent.py, scripts/score_report.py).
#
# Usage:  scripts/clone_repos.sh [LIST] [DEST]
#   LIST  one GitHub URL per line, #-comments allowed
#         (default: examples/repos.sample.txt)
#   DEST  where the clones go (default: data/clones — gitignored)
#
# The daily scan (radar/run.py) does NOT need this: it clones into a temp
# directory and deletes each clone as soon as it is scored.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIST="${1:-$ROOT/examples/repos.sample.txt}"
DEST="${2:-$ROOT/data/clones}"
mkdir -p "$DEST"

while IFS= read -r line; do
  url="$(printf '%s' "${line%%#*}" | tr -d '[:space:]')"
  [ -z "$url" ] && continue
  name="$(printf '%s' "$url" | sed -E 's#^.*github\.com[:/]+##; s#\.git$##; s#/+$##; s#/#-#g')"
  if [ -d "$DEST/$name/.git" ]; then echo "exists $name"; continue; fi
  rm -rf "${DEST:?}/$name"
  if GIT_TERMINAL_PROMPT=0 git clone --filter=blob:none --no-checkout "$url" "$DEST/$name" >/dev/null 2>&1; then
    echo "OK    $name"
  else
    echo "FAIL  $name  ($url)"
  fi
done < "$LIST"

echo "---"
ls -1 "$DEST"
