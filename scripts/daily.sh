#!/usr/bin/env bash
# The daily run: fetch the last 10 days, and push the data if anything is new.
# Pushing triggers the pages workflow, which republishes the page.
#
# Run this from a machine the Central Bank's site answers. GitHub's servers get
# "403 Forbidden", so it cannot run as a GitHub Actions job.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."

git pull --quiet --ff-only

# Exits non-zero if a check fails, which stops here before anything is committed.
uv run lkr-rates update

git add data
if git diff --cached --quiet -- data/clean; then
  echo "No new rates today."
  git reset --quiet
else
  git commit --quiet -m "data: rates up to $(date -u +%F)"
  git push --quiet
  echo "Pushed new rates."
fi
