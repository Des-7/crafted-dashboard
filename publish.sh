#!/usr/bin/env bash
# Regenerate the CraftED dashboard and publish it to GitHub Pages.
#
# One command: run generate.py -> commit ONLY if the output changed -> push.
# This is what the hourly n8n job will call. It is safe to run repeatedly:
# if nothing changed, it commits nothing and exits 0.
#
# The generator reads the ops DB strictly read-only; this script never touches
# anything under the ops directory.
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "==> generating dashboard"
"$PY" generate.py

# Stage the generated pages (and anything else that legitimately changed).
git add -A

if git diff --cached --quiet; then
  echo "==> no changes — nothing to publish"
  exit 0
fi

STAMP="$(date '+%Y-%m-%d %H:%M:%S %z')"
echo "==> committing"
git commit -q -m "dashboard: refresh ${STAMP}"

echo "==> pushing"
git push -q origin HEAD

echo "==> published"
