#!/usr/bin/env bash
# Regenerates the fitness dashboard and pushes it to GitHub Pages if anything changed.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

/usr/bin/python3 generate_dashboard.py

if [[ -n "$(git status --porcelain docs/)" ]]; then
  git add docs/
  git commit -m "Daily dashboard refresh: $(date '+%Y-%m-%d %H:%M')"
  git push origin main
  echo "Pushed dashboard update."
else
  echo "No changes to dashboard output, skipping commit."
fi
