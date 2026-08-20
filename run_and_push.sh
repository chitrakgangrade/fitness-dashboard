#!/usr/bin/env bash
# Regenerates the fitness dashboard and pushes it to GitHub Pages if anything changed.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

# Keep this checkout in sync with origin/main before generating. Without
# this, a push to main from anywhere else (e.g. a merged PR) leaves this
# clone behind, and `git push` below silently fails as a rejected
# non-fast-forward under set -e -- breaking the daily refresh with no
# visible error until someone notices the site is stale and manually pulls.
git fetch origin main
git merge --ff-only origin/main

/usr/bin/python3 generate_dashboard.py

if [[ -n "$(git status --porcelain docs/)" ]]; then
  git add docs/
  git commit -m "Daily dashboard refresh: $(date '+%Y-%m-%d %H:%M')"
  git push origin main
  echo "Pushed dashboard update."
else
  echo "No changes to dashboard output, skipping commit."
fi
