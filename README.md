# CraftED — Production Dashboard

A single-page, mobile-friendly production monitor for the CraftED video pipeline.
It is a **static, read-only mirror** of the CraftED operations database: a Python
script queries the DB and writes one self-contained HTML file (inline CSS/JS, no
external CDNs — it loads on any device or network), which is published to GitHub
Pages.

**Public URL:** https://des-7.github.io/crafted-dashboard/

The page carries an auto-refresh meta tag (30 min) and shows its generation time
in Amman time. Open it on a phone — the layout is responsive.

## What it shows

- **Live pulse** — count of videos per status as colour-coded cards, plus a table
  of every non-delivered video (code, course, type, status, age in current state).
  Rows turn red when a video's age exceeds that state's SLA.
- **Attention needed** — the explicit list of SLA-exceeded videos (same logic as
  the ops CLI's `stale check`). A calm "all on schedule" line when there are none.
- **History & throughput** — per-course delivered-vs-in-flight totals; average
  cycle time (submitted → delivered), a review-rounds metric (average and the max
  with its video code), and a 12-month deliveries bar chart (pure CSS/SVG, no
  chart library).
- **Breakdown** — videos per type and per course.

### A note on the cycle-time metric

The DB was seeded with ~78 SecureCoding videos registered retroactively in one
batch, so their submitted → delivered spans collapse to essentially zero. To
avoid fabricating a "we deliver in minutes" claim, the average **excludes any
delivery whose span is under one hour** and the card is labelled accordingly.
Until real end-to-end deliveries accumulate, this metric reads `n/a`.

## Files

| File            | Purpose                                                        |
| --------------- | -------------------------------------------------------------- |
| `generate.py`   | Reads the ops DB (read-only) and writes `dashboard.html` + `index.html`. |
| `publish.sh`    | One command: regenerate → commit (only if changed) → push.     |
| `dashboard.html`| Generated page (the named deliverable). Committed.             |
| `index.html`    | Identical copy so the bare Pages URL serves the page. Committed.|
| `.gitignore`    | Keeps the DB and any ops-dir artefacts out of the repo.        |
| `README.md`     | This file.                                                     |

## Read-only guarantee

`generate.py` opens the database through a `file:…?mode=ro&immutable=1` SQLite
URI. In this mode SQLite rejects every write at the driver level (verified:
`CREATE`/`INSERT` both raise *"attempt to write a readonly database"*), and it
will not create a DB if the path is missing. The script's only filesystem writes
are `dashboard.html` and `index.html` **inside this project directory** — nothing
is ever written under the ops directory, and the DB is never copied into this
repo.

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

## Hourly automation (n8n)

`publish.sh` is designed to be the single command an **n8n** job runs on an hourly
schedule: it regenerates the page from the current DB state, commits only when the
output actually changed, and pushes. GitHub Pages then serves the refreshed page
within a minute or two. Because it commits nothing when nothing changed, running
it hourly keeps the git history clean.

## GitHub Pages

The repository is published from the default branch's root, where `index.html`
lives. After enabling Pages (Settings → Pages → Source: *Deploy from a branch* →
branch `main`, folder `/root`), the dashboard is served at the public URL above.
