# CraftED — Production Dashboard

A mobile-friendly production monitor for the CraftED video pipeline.
It is a **static, read-only mirror** of the CraftED operations database: a Python
script queries the DB and writes one self-contained HTML file (inline CSS/JS, no
external CDNs — it loads on any device or network), which is published to GitHub
Pages.

**Public URL:** https://des-7.github.io/crafted-dashboard/

The page carries an auto-refresh meta tag (30 min) and shows its generation time
in Amman time. Open it on a phone — the layout is responsive.

## What it shows

The **overview page** (`index.html` / `dashboard.html`) is intentionally
aggregate-only — course names and numbers, no per-video codes (Ahmed's
2026-07-20 content ruling). Per-video detail lives on the course pages.

- **Pipeline** — count of videos per status as colour-coded cards.
- **SLA watch** — per-course *counts* of videos past their state SLA (not the
  videos themselves). Each course links to its page for the specifics; a calm
  "every course is within SLA" line when there are none.
- **Performance** — average cycle time (submitted → delivered), a review-rounds
  metric (average plus the peak rounds on a single video), delivered/total, and a
  12-month deliveries bar chart (pure CSS/SVG, no chart library).
- **Portfolio** — per-course delivered-vs-total, videos per type, videos per course.
- **Course drill-down** — each course name links to a generated page carrying the
  per-video detail: video code, status, pipeline progress, time in state, SLA, and
  review rounds, with SLA-exceeded rows flagged.

### A note on the cycle-time metric

The DB was seeded with ~78 SecureCoding videos registered retroactively in one
batch, so their submitted → delivered spans collapse to essentially zero. To
avoid fabricating a "we deliver in minutes" claim, the average **excludes any
delivery whose span is under one hour** and the card is labelled accordingly.
Until real end-to-end deliveries accumulate, this metric reads `n/a`.

## Files

| File            | Purpose                                                        |
| --------------- | -------------------------------------------------------------- |
| `generate.py`   | Reads the ops DB (read-only) and writes `dashboard.html` + `index.html` + course pages. |
| `publish.sh`    | One command: regenerate → commit (only if changed) → push.     |
| `dashboard.html`| Generated page (the named deliverable). Committed.             |
| `index.html`    | Identical copy so the bare Pages URL serves the page. Committed.|
| `courses/`      | Generated course pages, one URL-safe directory per course.     |
| `.gitignore`    | Keeps the DB and any ops-dir artefacts out of the repo.        |
| `README.md`     | This file.                                                     |

## Read-only guarantee

`generate.py` opens the database through a `file:…?mode=ro&immutable=1` SQLite
URI. In this mode SQLite rejects every write at the driver level (verified:
`CREATE`/`INSERT` both raise *"attempt to write a readonly database"*), and it
will not create a DB if the path is missing. The script's only filesystem writes
are `dashboard.html`, `index.html`, and the per-course pages under `courses/`
**inside this project directory** — nothing is ever written under the ops
directory, and the DB is never copied into this repo.

The DB path is read at runtime (default `/Volumes/Des/crafted-ops/crafted.db`,
overridable via the `CRAFTED_OPS_DB` environment variable). The database file is
never committed — see `.gitignore`.

## Usage

Generate locally:

```bash
python3 generate.py           # writes dashboard.html + index.html
```

Publish (regenerate, commit if changed, push):

```bash
./publish.sh
```

## Hourly refresh (n8n)

`publish.sh` is designed to be the single command an **n8n** job runs on an hourly
schedule: it regenerates the pages from the current DB state, commits only when the
output actually changed, and pushes. GitHub Pages then serves the refreshed pages
within a minute or two.

"Actually changed" is enforced by normalising the two bits that move on every run
regardless of data — the generation timestamp and the server-rendered "time in
state" age cells on the course pages — before comparing against the committed
copies. If only the clock and ages moved, the regenerated pages are discarded and
nothing is committed, so running hourly keeps the git history clean. A genuine
change (status, counts, an item crossing its SLA, a course added or removed) still
differs after normalisation and is published.

> **Status (2026-07-21): the hourly n8n job (workflow W4) is INACTIVE.** By
> Ahmed's explicit decision it stays off — along with the rest of n8n — until
> go-live. Nothing in this repo activates it; `publish.sh` is currently run
> manually. Do not enable the schedule before go-live.

## GitHub Pages

The repository is published from the default branch's root, where `index.html`
lives. After enabling Pages (Settings → Pages → Source: *Deploy from a branch* →
branch `main`, folder `/root`), the dashboard is served at the public URL above.
