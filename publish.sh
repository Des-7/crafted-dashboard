#!/usr/bin/env bash
# Regenerate the CraftED dashboard and publish it to GitHub Pages.
#
# One command: run generate.py -> commit ONLY if the data changed -> push.
# This is what the hourly n8n job will call, so it must be quiet when idle:
# the generation timestamp bumps on every run, so we compare the generated page
# to the committed one with that timestamp normalised out. If nothing but the
# clock moved, we restore the committed copies and exit without a commit — no
# history noise from 24 identical refreshes a day.
#
# The generator reads the ops DB strictly read-only; this script never touches
# anything under the ops directory.
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "==> generating dashboard"
"$PY" generate.py

# Normalise the volatile generation timestamp so it doesn't count as a change.
norm() {
  sed -E 's#Generated <b>[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}</b>#Generated <b>TS</b>#'
}

if git rev-parse --verify -q HEAD >/dev/null && git cat-file -e HEAD:dashboard.html 2>/dev/null; then
  if diff -q <(git show HEAD:dashboard.html | norm) <(norm < dashboard.html) >/dev/null; then
    # Only the timestamp moved — discard the regenerated pages, stay clean.
    git checkout -- dashboard.html index.html 2>/dev/null || true
    echo "==> no data change — nothing to publish"
    exit 0
  fi
fi

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
