#!/usr/bin/env bash
# Regenerate the CraftED dashboard and publish it to GitHub Pages.
#
# One command: run generate.py -> commit ONLY if the data changed -> push.
# This is what the hourly n8n job will call, so it must be quiet when idle.
#
# Two volatile bits change on every run without any real data moving:
#   1. the generation timestamp in each page header, and
#   2. the server-rendered "time in state" age cells on the course pages
#      (they tick up every hour for as long as a video sits in a state).
# We normalise both out before comparing the freshly generated pages to the
# committed ones. If only the clock and the ages moved, we restore the committed
# copies and exit without a commit — no history noise from 24 refreshes a day.
# A genuine change (status, counts, an item crossing its SLA, a course added or
# removed) still differs after normalisation and is published normally.
#
# The generator reads the ops DB strictly read-only; this script never touches
# anything under the ops directory.
set -euo pipefail

cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "==> generating dashboard"
"$PY" generate.py

# Normalise the volatile bits so they don't count as a change:
#   - the "Updated <b>YYYY-MM-DD HH:MM</b>" generation timestamp, and
#   - "time in state" age cells (e.g. 45m / 3.8h / 3.8d / —) on course pages.
# Both are pure functions of the wall clock, not of the underlying data.
norm() {
  sed -E \
    -e 's#Updated <b>[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}</b>#Updated <b>TS</b>#g' \
    -e 's#<td class="num">([0-9]+(\.[0-9]+)?[mhd]|—)</td>#<td class="num">AGE</td>#g'
}

# The complete set of generated pages: the two root copies plus every course
# page named in the manifest.
generated_pages() {
  printf '%s\n' dashboard.html index.html
  if [ -f courses/.generated-pages ]; then
    while IFS= read -r rel; do
      [ -n "$rel" ] && printf 'courses/%s\n' "$rel"
    done < courses/.generated-pages
  fi
}

# Return success (0) if anything beyond the volatile bits changed vs HEAD.
data_changed() {
  # A change in the SET of courses (a page added or removed) is always real;
  # the manifest captures it exactly, so compare it verbatim.
  git cat-file -e HEAD:courses/.generated-pages 2>/dev/null || return 0
  diff -q <(git show HEAD:courses/.generated-pages) courses/.generated-pages >/dev/null || return 0
  # Then every page's content, with the clock and age cells normalised away.
  local f
  while IFS= read -r f; do
    git cat-file -e "HEAD:$f" 2>/dev/null || return 0
    diff -q <(git show "HEAD:$f" | norm) <(norm < "$f") >/dev/null || return 0
  done < <(generated_pages)
  return 1
}

if git rev-parse --verify -q HEAD >/dev/null; then
  if ! data_changed; then
    # Only the clock/ages moved — discard the regenerated pages, stay clean.
    git checkout -- dashboard.html index.html courses 2>/dev/null || true
    echo "==> no data change — nothing to publish"
    exit 0
  fi
fi

# Explicit staging of the known outputs ONLY — never `git add -A`. .gitignore is
# a second line of defence, not the first. README.md is staged too so a genuine
# doc edit rides along, but only when it actually changed (add is a no-op if not).
git add dashboard.html index.html courses
if [ -f README.md ]; then git add README.md; fi

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
