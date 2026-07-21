#!/usr/bin/env python3
"""Generate the D.Learn production dashboard as self-contained HTML pages.

Reads the CraftED ops SQLite DB STRICTLY READ-ONLY (sqlite `mode=ro` URI) and
writes ``dashboard.html``, ``index.html``, and per-course pages next to this
script. No external assets, no CDNs — the pages load on any device/network. This
script NEVER writes to the DB or anywhere under the ops directory; it only reads.

Run:  python3 generate.py
"""

import html
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Absolute path to the ops DB. Read at runtime; the DB is NEVER copied here.
DB_PATH = os.environ.get("CRAFTED_OPS_DB", "/Volumes/Des/crafted-ops/crafted.db")

# Where generated pages are written (inside this project directory). The root
# overview is emitted twice: dashboard.html (the named deliverable) and
# index.html (so the bare GitHub Pages URL serves it). Course pages live below
# courses/<slug>/index.html.
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_HERE, "dashboard.html")
INDEX_PATH = os.path.join(_HERE, "index.html")
COURSES_DIR = os.path.join(_HERE, "courses")
COURSES_MANIFEST = os.path.join(COURSES_DIR, ".generated-pages")

# Public repo / Pages URL for the footer.
REPO_URL = "https://github.com/Des-7/crafted-dashboard"

# Amman (Asia/Amman) is a fixed UTC+3 offset (Jordan dropped DST in 2022).
# DB timestamps are written by SQLite `datetime('now')`, i.e. naive UTC.
AMMAN = timezone(timedelta(hours=3), name="Amman")

# Minimum submitted -> delivered span (hours) for a delivery to count toward the
# cycle-time average. Retroactively-registered videos collapse to ~0h and would
# otherwise fabricate a "we deliver in minutes" claim, so they are excluded.
MIN_CYCLE_HOURS = 1.0

# Status -> semantic colour class. Mirrors the team sheet: failures red,
# awaiting_review yellow, delivered green, holds amber, work-in-flight blue.
STATUS_CLASS = {
    "submitted": "neutral",
    "validated": "blue",
    "parsing": "blue",
    "awaiting_review": "yellow",
    "approved": "teal",
    "rendering": "blue",
    "rendered": "cyan",
    "delivered": "green",
    "failed": "red",
    "on_hold": "amber",
}

# Display order for status cards (pipeline order, then the off-pipeline states).
STATUS_ORDER = [
    "submitted", "validated", "parsing", "awaiting_review", "approved",
    "rendering", "rendered", "delivered", "failed", "on_hold",
]

PIPELINE_STAGES = [
    "submitted", "validated", "parsing", "awaiting_review", "approved",
    "rendering", "rendered", "delivered",
]


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def parse_utc(ts):
    """Parse a DB timestamp ('YYYY-MM-DD HH:MM:SS') as an aware UTC datetime."""
    if ts is None:
        return None
    ts = ts.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fmt_age(hours):
    """Human-friendly age from a float hour count."""
    if hours is None:
        return "—"
    if hours < 1:
        m = int(round(hours * 60))
        return f"{m}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def slugify(value):
    """Create a stable, URL-safe directory name from a course code."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug or "course"


def stage_progress(status):
    """Return a display-only percentage for a normal pipeline state."""
    if status not in PIPELINE_STAGES:
        return None
    return PIPELINE_STAGES.index(status) / (len(PIPELINE_STAGES) - 1) * 100


# --------------------------------------------------------------------------- #
# Data access (READ-ONLY)
# --------------------------------------------------------------------------- #

def connect_ro(path):
    """Open the DB strictly read-only via a file: URI with mode=ro.

    mode=ro fails if the file is missing (it will NOT create one) and blocks
    every write at the SQLite layer, so this process cannot mutate the ops DB.
    """
    if not os.path.exists(path):
        raise SystemExit(f"error: DB not found at {path}")
    uri = f"file:{path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect(conn, now_utc):
    """Run every query and return a plain dict of computed data for rendering."""
    d = {}

    # -- SLA thresholds -----------------------------------------------------
    d["sla"] = {r["state"]: r["max_hours"]
                for r in conn.execute("SELECT state, max_hours FROM state_sla")}

    # -- Status pulse -------------------------------------------------------
    d["status_counts"] = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) n FROM videos GROUP BY status")
    }
    d["total_videos"] = sum(d["status_counts"].values())
    d["delivered_total"] = d["status_counts"].get("delivered", 0)
    d["inflight_total"] = d["total_videos"] - d["delivered_total"]

    # -- Type / course breakdown -------------------------------------------
    d["type_counts"] = [
        (r["video_type"], r["n"])
        for r in conn.execute(
            "SELECT video_type, COUNT(*) n FROM videos "
            "GROUP BY video_type ORDER BY n DESC")
    ]
    # projects IS the faculty layer: each course maps to a faculty via
    # courses.project_id. p.slug is the ops faculty slug (case-sensitive,
    # lowercase — displayed verbatim); p.name is the faculty display name.
    d["course_counts"] = [
        dict(id=r["id"], project_id=r["project_id"],
             faculty_slug=r["faculty_slug"], faculty_name=r["faculty_name"],
             code=r["code"], name=r["name"], total=r["total"],
             delivered=r["delivered"], inflight=r["total"] - r["delivered"])
        for r in conn.execute(
            "SELECT c.id, c.project_id, p.slug AS faculty_slug, "
            "  p.name AS faculty_name, c.code, c.name, COUNT(v.id) total, "
            "  COALESCE(SUM(CASE WHEN v.status='delivered' THEN 1 ELSE 0 END), 0) delivered "
            "FROM courses c "
            "JOIN projects p ON p.id = c.project_id "
            "LEFT JOIN videos v ON v.course_id = c.id "
            "GROUP BY c.id ORDER BY total DESC")
    ]
    used_slugs = set()
    for course in d["course_counts"]:
        base = slugify(course["code"])
        slug = base if base not in used_slugs else f'{base}-{course["id"]}'
        course["slug"] = slug
        used_slugs.add(slug)

    # -- Faculty grouping (projects = faculties) ---------------------------
    # Group the courses under their faculty for the overview roster, preserving
    # the total-desc order within each faculty. Faculties themselves are ordered
    # by video volume (busiest first), ties broken by slug for a stable page.
    faculties_by_id, faculty_order = {}, []
    for c in d["course_counts"]:
        fid = c["project_id"]
        if fid not in faculties_by_id:
            faculties_by_id[fid] = dict(
                id=fid, slug=c["faculty_slug"], name=c["faculty_name"],
                courses=[], total=0, delivered=0, inflight=0)
            faculty_order.append(fid)
        f = faculties_by_id[fid]
        f["courses"].append(c)
        f["total"] += c["total"]
        f["delivered"] += c["delivered"]
        f["inflight"] += c["inflight"]
    faculties = [faculties_by_id[i] for i in faculty_order]
    faculties.sort(key=lambda f: (-f["total"], f["slug"]))
    d["faculties"] = faculties

    # -- Non-delivered videos + stale detection ----------------------------
    non_delivered, stale = [], []
    rows = conn.execute(
        "SELECT v.code, v.video_type, v.status, v.state_entered_at, "
        "       c.code AS course_code "
        "FROM videos v JOIN courses c ON c.id = v.course_id "
        "WHERE v.status != 'delivered' "
        "ORDER BY v.state_entered_at ASC"
    ).fetchall()
    for r in rows:
        entered = parse_utc(r["state_entered_at"])
        age_h = ((now_utc - entered).total_seconds() / 3600.0
                 if entered else None)
        sla_h = d["sla"].get(r["status"])
        over = (sla_h is not None and age_h is not None and age_h > sla_h)
        item = dict(code=r["code"], course=r["course_code"],
                    vtype=r["video_type"], status=r["status"],
                    age_h=age_h, sla_h=sla_h, over=over)
        non_delivered.append(item)
        if over:
            stale.append(item)
    non_delivered.sort(key=lambda x: (x["age_h"] is None, -(x["age_h"] or 0)))
    stale.sort(key=lambda x: -(x["age_h"] or 0))
    d["non_delivered"] = non_delivered
    d["stale"] = stale

    # -- Cycle time (submitted -> delivered), retroactive rows excluded -----
    spans = conn.execute(
        "WITH sub AS (SELECT video_id, MIN(created_at) t0 "
        "             FROM state_transitions WHERE to_state='submitted' "
        "             GROUP BY video_id), "
        "     del AS (SELECT video_id, MAX(created_at) t1 "
        "             FROM state_transitions WHERE to_state='delivered' "
        "             GROUP BY video_id) "
        "SELECT s.t0, dl.t1 FROM sub s JOIN del dl ON dl.video_id = s.video_id"
    ).fetchall()
    qualifying = []
    for s in spans:
        t0, t1 = parse_utc(s["t0"]), parse_utc(s["t1"])
        if t0 and t1:
            hrs = (t1 - t0).total_seconds() / 3600.0
            if hrs >= MIN_CYCLE_HOURS:
                qualifying.append(hrs)
    d["cycle_delivered_total"] = len(spans)
    d["cycle_qualifying"] = len(qualifying)
    d["cycle_avg_h"] = (sum(qualifying) / len(qualifying)) if qualifying else None

    # -- Review rounds (transitions INTO awaiting_review per video) ---------
    rounds = conn.execute(
        "SELECT st.video_id, v.code, c.code AS course_code, COUNT(*) n "
        "FROM state_transitions st "
        "JOIN videos v ON v.id = st.video_id "
        "JOIN courses c ON c.id = v.course_id "
        "WHERE st.to_state = 'awaiting_review' "
        "GROUP BY st.video_id ORDER BY n DESC"
    ).fetchall()
    d["review_video_count"] = len(rounds)
    if rounds:
        total_rounds = sum(r["n"] for r in rounds)
        d["review_avg"] = total_rounds / len(rounds)
        top = rounds[0]
        d["review_max"] = top["n"]
        d["review_max_code"] = top["code"]
        d["review_max_course"] = top["course_code"]
    else:
        d["review_avg"] = None
        d["review_max"] = None
        d["review_max_code"] = None
        d["review_max_course"] = None

    # -- Deliveries per month (last 12 calendar months) ---------------------
    by_month = {
        r["ym"]: r["n"]
        for r in conn.execute(
            "SELECT strftime('%Y-%m', created_at) ym, COUNT(*) n "
            "FROM state_transitions WHERE to_state='delivered' GROUP BY ym")
    }
    months = []
    y, m = now_utc.year, now_utc.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    months.reverse()
    d["deliveries_per_month"] = [
        (f"{yy:04d}-{mm:02d}", by_month.get(f"{yy:04d}-{mm:02d}", 0))
        for (yy, mm) in months
    ]

    # -- Per-course drill-down pages --------------------------------------
    course_details = {c["id"]: dict(c, status_counts={}, videos=[])
                      for c in d["course_counts"]}
    video_rows = conn.execute(
        "SELECT v.id, v.course_id, v.code, v.title, v.video_type, v.status, "
        "       v.state_entered_at, "
        "       (SELECT COUNT(*) FROM state_transitions st "
        "        WHERE st.video_id=v.id) transition_count, "
        "       (SELECT COUNT(*) FROM state_transitions st "
        "        WHERE st.video_id=v.id AND st.to_state='awaiting_review') review_rounds "
        "FROM videos v ORDER BY v.course_id, v.title COLLATE NOCASE"
    ).fetchall()
    for r in video_rows:
        course = course_details.get(r["course_id"])
        if course is None:
            continue
        entered = parse_utc(r["state_entered_at"])
        age_h = ((now_utc - entered).total_seconds() / 3600.0
                 if entered else None)
        sla_h = d["sla"].get(r["status"])
        over = (sla_h is not None and age_h is not None and age_h > sla_h)
        course["status_counts"][r["status"]] = (
            course["status_counts"].get(r["status"], 0) + 1)
        course["videos"].append(dict(
            id=r["id"], code=r["code"], title=r["title"],
            vtype=r["video_type"], status=r["status"], age_h=age_h,
            sla_h=sla_h, over=over, progress=stage_progress(r["status"]),
            review_rounds=r["review_rounds"],
            transition_count=r["transition_count"],
        ))
    for course in course_details.values():
        course["total_videos"] = course["total"]
        course["delivered_total"] = course["delivered"]
        course["inflight_total"] = course["inflight"]
        course["attention_count"] = sum(1 for v in course["videos"] if v["over"])
    d["course_details"] = [course_details[c["id"]]
                           for c in d["course_counts"]]

    return d


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #

def e(s):
    return html.escape(str(s), quote=True)


MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def render_status_cards(d):
    present = [(s, d["status_counts"].get(s, 0)) for s in STATUS_ORDER
               if d["status_counts"].get(s, 0) > 0]
    if not present:
        return '<p class="muted">No videos registered yet.</p>'
    total = d["total_videos"] or 1
    cards = []
    for status, n in present:
        cls = STATUS_CLASS.get(status, "neutral")
        label = status.replace("_", " ")
        share = n / total * 100
        cards.append(
            f'<article class="card {e(cls)}" role="listitem">'
            '<div class="card-head">'
            f'<span class="status-dot" aria-hidden="true"></span>{e(label)}'
            '</div><div class="card-value-row">'
            f'<div class="card-n">{n}</div><div class="card-share">{share:.0f}%</div>'
            '</div><div class="card-meter" aria-hidden="true">'
            f'<span style="width:{share:.2f}%"></span></div>'
            f'<div class="card-l">of {d["total_videos"]} tracked videos</div></article>'
        )
    return f'<div class="cards" role="list">{"".join(cards)}</div>'


def render_course_attention(d):
    """Per-course SLA-exceeded counts for the main page.

    Aggregate only: how many videos in each course have exceeded their state
    SLA — no video codes. Each course links to its own page, where the specific
    over-SLA videos (with codes and ages) are listed. This keeps the main page
    to faculty/course names and numbers per Ahmed's 2026-07-20 content ruling.
    """
    courses = [c for c in d["course_details"] if c["attention_count"] > 0]
    if not courses:
        return ('<div class="state-message success compact"><span class="state-icon" aria-hidden="true">✓</span>'
                '<div><strong>Every course is within SLA</strong>'
                '<p>No course has a video past its state threshold.</p></div></div>')
    courses.sort(key=lambda c: -c["attention_count"])
    items = []
    for c in courses:
        n = c["attention_count"]
        vids = "video" if n == 1 else "videos"
        items.append(
            '<li>'
            f'<a class="course-link" href="courses/{e(c["slug"])}/">'
            f'{e(c["name"])} <span class="muted mono">{e(c["code"])}</span>'
            '<span aria-hidden="true">↗</span></a> — '
            f'<span class="over-by">{n} {vids} over SLA</span>'
            '</li>'
        )
    return f'<ul class="stale-list">{"".join(items)}</ul>'


def render_course_totals(d):
    faculties = d["faculties"]
    if not faculties:
        return '<p class="muted">No courses yet.</p>'
    # Scale every bar against the busiest course across all faculties so lengths
    # stay comparable between groups.
    maxtot = max((c["total"] for f in faculties for c in f["courses"]),
                 default=1) or 1
    groups = []
    for f in faculties:
        bars = []
        for c in f["courses"]:
            dpct = (c["delivered"] / maxtot * 100) if maxtot else 0
            ipct = (c["inflight"] / maxtot * 100) if maxtot else 0
            bars.append(
                '<div class="bar-row">'
                f'<div class="bar-label mono"><a class="course-link" href="courses/{e(c["slug"])}/">'
                f'{e(c["code"])}<span aria-hidden="true">↗</span></a></div>'
                '<div class="bar-track">'
                f'<div class="bar-seg green" style="width:{dpct:.2f}%" '
                f'title="delivered {c["delivered"]}"></div>'
                f'<div class="bar-seg blue" style="width:{ipct:.2f}%" '
                f'title="in flight {c["inflight"]}"></div>'
                '</div>'
                f'<div class="bar-val">{c["delivered"]}<span class="muted">'
                f'/{c["total"]}</span></div>'
                '</div>'
            )
        course_word = "course" if len(f["courses"]) == 1 else "courses"
        # Faculty slug shown verbatim (lowercase, as stored in the ops DB).
        groups.append(
            '<div class="faculty-group">'
            '<div class="faculty-head">'
            f'<span class="faculty-name">{e(f["name"])}</span>'
            f'<span class="faculty-slug mono">{e(f["slug"])}</span>'
            f'<span class="faculty-agg muted">{len(f["courses"])} {course_word}'
            f' · {f["delivered"]}/{f["total"]} delivered</span>'
            '</div>'
            f'<div class="bars">{"".join(bars)}</div>'
            '</div>'
        )
    legend = ('<div class="legend">'
              '<span><i class="sw green"></i>delivered</span>'
              '<span><i class="sw blue"></i>in flight</span></div>')
    return f'{"".join(groups)}{legend}'


def render_metrics(d):
    # Cycle time
    if d["cycle_avg_h"] is None:
        cyc_val = "n/a"
        cyc_sub = (f'no delivery has a &ge;1h submitted&rarr;delivered span yet '
                   f'({d["cycle_delivered_total"]} delivered, all retroactive)')
    else:
        cyc_val = fmt_age(d["cycle_avg_h"])
        cyc_sub = (f'over {d["cycle_qualifying"]} of {d["cycle_delivered_total"]} '
                   f'delivered (excludes same-day retroactive registrations)')
    # Review rounds
    if d["review_avg"] is None:
        rev_val = "n/a"
        rev_sub = "no video has reached review yet"
    else:
        rev_val = f'{d["review_avg"]:.2f}'
        rounds_word = "round" if d["review_max"] == 1 else "rounds"
        rev_sub = f'peak {d["review_max"]} {rounds_word} on a single video'
    return (
        '<div class="metrics">'
        '<article class="metric"><div class="metric-top"><span>Speed</span><i class="metric-mark"></i></div>'
        f'<div class="metric-v">{cyc_val}</div>'
        '<div class="metric-k">avg cycle time</div>'
        f'<div class="metric-s">{cyc_sub}</div>'
        '</article><article class="metric"><div class="metric-top"><span>Quality loop</span><i class="metric-mark"></i></div>'
        f'<div class="metric-v">{rev_val}</div>'
        '<div class="metric-k">avg review rounds / video</div>'
        f'<div class="metric-s">{rev_sub}</div>'
        '</article><article class="metric"><div class="metric-top"><span>Output</span><i class="metric-mark"></i></div>'
        f'<div class="metric-v">{d["delivered_total"]}<span class="muted">'
        f'/{d["total_videos"]}</span></div>'
        '<div class="metric-k">delivered / total</div>'
        f'<div class="metric-s">{d["inflight_total"]} in flight</div>'
        '</article>'
        '</div>'
    )


def render_deliveries_chart(d):
    data = d["deliveries_per_month"]
    maxn = max((n for _, n in data), default=0)
    if maxn == 0:
        # keep a baseline so empty bars still render a readable axis
        maxn = 1
    bars = []
    for ym, n in data:
        y, m = ym.split("-")
        label = MONTH_ABBR[int(m)]
        h = (n / maxn * 100) if maxn else 0
        # minimum visible sliver for zero so the month still reads on the axis
        style_h = max(h, 1.5)
        val = f'<span class="cbar-n">{n}</span>' if n else ''
        bars.append(
            '<div class="cbar-col" '
            f'title="{e(ym)}: {n} delivered">'
            f'{val}'
            f'<div class="cbar" style="height:{style_h:.2f}%"'
            f'{" data-zero=1" if n == 0 else ""}></div>'
            f'<div class="cbar-x">{label}</div>'
            f'<div class="cbar-y muted">{e(y[2:])}</div>'
            '</div>'
        )
    total = sum(n for _, n in data)
    return (
        f'<div class="chart" role="img" '
        f'aria-label="Deliveries per month, last 12 months, {total} total">'
        f'{"".join(bars)}</div>'
    )


def render_type_breakdown(d):
    rows = d["type_counts"]
    if not rows:
        return '<p class="muted">No videos yet.</p>'
    total = sum(n for _, n in rows) or 1
    body = []
    for vtype, n in rows:
        pct = n / total * 100
        body.append(
            '<div class="bar-row">'
            f'<div class="bar-label">{e(vtype)}</div>'
            '<div class="bar-track">'
            f'<div class="bar-seg teal" style="width:{pct:.2f}%"></div></div>'
            f'<div class="bar-val">{n}<span class="muted"> '
            f'{pct:.0f}%</span></div>'
            '</div>'
        )
    return f'<div class="bars">{"".join(body)}</div>'


def render_course_breakdown(d):
    rows = sorted(d["course_counts"], key=lambda c: -c["total"])
    if not rows:
        return '<p class="muted">No courses yet.</p>'
    total = sum(c["total"] for c in rows) or 1
    body = []
    for c in rows:
        pct = c["total"] / total * 100
        body.append(
            '<div class="bar-row">'
            f'<div class="bar-label mono"><a class="course-link" href="courses/{e(c["slug"])}/">'
            f'{e(c["code"])}<span aria-hidden="true">↗</span></a></div>'
            '<div class="bar-track">'
            f'<div class="bar-seg cyan" style="width:{pct:.2f}%"></div></div>'
            f'<div class="bar-val">{c["total"]}<span class="muted"> '
            f'{pct:.0f}%</span></div>'
            '</div>'
        )
    return f'<div class="bars">{"".join(body)}</div>'


def render_course_video_table(course):
    """Render the public, non-sensitive video progress table for one course."""
    if not course["videos"]:
        return ('<div class="state-message compact"><span class="state-icon" aria-hidden="true">—</span>'
                '<div><strong>No videos registered yet</strong>'
                '<p>This page will update automatically when videos are added.</p></div></div>')
    rows = []
    for video in course["videos"]:
        row_cls = ' class="over"' if video["over"] else ""
        badge_cls = STATUS_CLASS.get(video["status"], "neutral")
        sla_txt = f'{video["sla_h"]:g}h' if video["sla_h"] is not None else "—"
        if video["progress"] is None:
            progress = '<span class="video-progress-na">Off pipeline</span>'
        else:
            progress = ('<div class="video-progress-row" '
                        f'aria-label="{video["progress"]:.0f}% through pipeline">'
                        '<div class="video-progress-track" aria-hidden="true">'
                        f'<span class="video-progress-fill" style="width:{video["progress"]:.2f}%"></span>'
                        '</div>'
                        f'<span class="video-progress-value">{video["progress"]:.0f}%</span></div>')
        rows.append(
            f'<tr{row_cls}><td class="video-identity">'
            f'<div class="video-title">{e(video["title"])}</div>'
            f'<div class="video-code mono" title="{e(video["code"])}">{e(video["code"])}</div></td>'
            f'<td>{e(video["vtype"])}</td><td><span class="pill {e(badge_cls)}">'
            f'{e(video["status"].replace("_", " "))}</span></td>'
            f'<td class="progress-cell">{progress}</td><td class="num">{e(fmt_age(video["age_h"]))}</td>'
            f'<td class="num muted">{sla_txt}</td><td class="num">{video["review_rounds"]}</td></tr>')
    return ('<div class="table-wrap"><table class="course-video-table">'
            '<thead><tr><th>Video</th><th>Type</th><th>Status</th><th>Pipeline progress</th>'
            '<th class="num">Time in state</th><th class="num">SLA</th><th class="num">Reviews</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

CSS = """
:root{
  color-scheme:dark;--canvas:#071018;--surface:#0e1b27;--surface-raised:#122231;
  --surface-soft:#0b1722;--line:rgba(154,181,207,.15);--line-strong:rgba(154,181,207,.24);
  --text:#f4f8fb;--muted:#91a5b8;--faint:#647b90;--brand:#EF4249;--brand-strong:#d8323a;
  --brand-soft:rgba(239,66,73,.12);--green:#46d69a;--green-soft:rgba(70,214,154,.11);
  --red:#ff6b77;--red-soft:rgba(255,107,119,.12);--yellow:#f5c66a;
  --yellow-soft:rgba(245,198,106,.12);--amber:#f0a65a;--amber-soft:rgba(240,166,90,.12);
  --blue:#72aaff;--blue-soft:rgba(114,170,255,.12);--teal:#59d9cf;
  --teal-soft:rgba(89,217,207,.12);--cyan:#5fd3ea;--cyan-soft:rgba(95,211,234,.12);
  --neutral:#a1afbd;--neutral-soft:rgba(161,175,189,.10);--shadow:0 28px 80px rgba(0,0,0,.26)
}
*{box-sizing:border-box}html{margin:0;padding:0;scroll-behavior:smooth}
body{margin:0;min-height:100vh;background:radial-gradient(circle at 12% -5%,rgba(239,66,73,.13),transparent 30rem),radial-gradient(circle at 88% 0,rgba(114,170,255,.10),transparent 28rem),var(--canvas);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.22;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);background-size:48px 48px;mask-image:linear-gradient(to bottom,#000,transparent 72%)}
.wrap{position:relative;z-index:1;max-width:1240px;margin:0 auto;padding:28px 24px 72px}
.site-header{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:24px}
.brand-lockup{display:flex;align-items:center;gap:13px;min-width:0;color:inherit;text-decoration:none}
.brand-mark{display:grid;place-items:center;width:48px;height:42px;border-radius:13px;background:linear-gradient(145deg,var(--brand),var(--brand-strong));color:#fff;font-size:11px;font-weight:900;letter-spacing:.04em;box-shadow:0 12px 28px rgba(239,66,73,.24)}
.brand-kicker,.section-kicker,.metric-top{text-transform:uppercase;letter-spacing:.13em;font-size:10.5px;font-weight:800}
.brand-kicker{color:var(--brand);margin-bottom:1px}.brand-title{font-size:17px;font-weight:720;letter-spacing:-.02em;white-space:nowrap}
.header-meta{display:flex;align-items:center;justify-content:flex-end;gap:12px;color:var(--muted);font-size:12px}.header-meta b{color:var(--text)}
.live-chip{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border:1px solid rgba(70,214,154,.22);border-radius:999px;background:var(--green-soft);color:#9cf0ca;font-weight:700}.live-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px rgba(70,214,154,.10)}
.hero{position:relative;display:grid;grid-template-columns:minmax(0,1.45fr) minmax(260px,.55fr);gap:28px;overflow:hidden;padding:34px;border:1px solid var(--line-strong);border-radius:24px;background:linear-gradient(135deg,rgba(18,34,49,.98),rgba(10,23,34,.97));box-shadow:var(--shadow)}
.hero::after{content:"";position:absolute;width:330px;height:330px;border-radius:50%;right:-140px;top:-185px;background:rgba(239,66,73,.14);filter:blur(8px);pointer-events:none}
.hero-copy{position:relative;z-index:1;display:flex;align-items:center}.summary-grid{display:grid;width:100%;grid-template-columns:repeat(4,minmax(90px,1fr));gap:10px}
.summary-item{padding:14px 15px;border:1px solid var(--line);border-radius:14px;background:rgba(5,14,21,.33)}.summary-value{font-size:25px;font-weight:780;line-height:1;font-variant-numeric:tabular-nums;letter-spacing:-.035em}.summary-label{margin-top:6px;color:var(--muted);font-size:11.5px}.summary-item.alert .summary-value{color:var(--red)}
.health-card{position:relative;z-index:1;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:250px;padding:24px;border:1px solid var(--line);border-radius:19px;background:rgba(4,13,20,.38)}
.health-ring{--completion:0%;position:relative;display:grid;place-items:center;width:154px;height:154px;border-radius:50%;background:conic-gradient(var(--brand) var(--completion),rgba(255,255,255,.07) 0);box-shadow:0 0 42px rgba(239,66,73,.12)}.health-ring::before{content:"";position:absolute;width:126px;height:126px;border-radius:50%;background:var(--surface-soft)}
.health-inner{position:relative;z-index:1;text-align:center}.health-value{display:block;font-size:36px;font-weight:820;line-height:1;letter-spacing:-.05em}.health-unit{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.12em;font-weight:750}.health-title{margin-top:16px;font-size:13px;font-weight:700}.health-sub{margin-top:3px;color:var(--muted);font-size:11.5px;text-align:center}
.dashboard-section{margin-top:48px}.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:16px}.section-kicker{color:var(--brand);margin-bottom:4px}.section-title{margin:0;font-size:22px;line-height:1.2;letter-spacing:-.025em}.section-copy{max-width:470px;margin:5px 0 0;color:var(--muted);font-size:13px}.section-aside{color:var(--faint);font-size:11.5px;white-space:nowrap}
.muted{color:var(--muted)}.mono{font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace;font-size:12px;word-break:break-word}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card{--tone:var(--neutral);--tone-soft:var(--neutral-soft);min-width:0;padding:17px 18px 16px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(155deg,var(--surface-raised),var(--surface));transition:transform .2s ease,border-color .2s ease}.card:hover{transform:translateY(-2px);border-color:var(--line-strong)}
.card.green{--tone:var(--green);--tone-soft:var(--green-soft)}.card.red{--tone:var(--red);--tone-soft:var(--red-soft)}.card.yellow{--tone:var(--yellow);--tone-soft:var(--yellow-soft)}.card.amber{--tone:var(--amber);--tone-soft:var(--amber-soft)}.card.blue{--tone:var(--blue);--tone-soft:var(--blue-soft)}.card.teal{--tone:var(--teal);--tone-soft:var(--teal-soft)}.card.cyan{--tone:var(--cyan);--tone-soft:var(--cyan-soft)}
.card-head{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:.08em}.status-dot{width:7px;height:7px;border-radius:50%;background:var(--tone);box-shadow:0 0 0 4px var(--tone-soft)}.card-value-row{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-top:20px}.card-n{font-size:36px;font-weight:820;line-height:1;letter-spacing:-.045em}.card-share{color:var(--tone);font-size:12px;font-weight:750}.card-meter{height:4px;margin-top:14px;overflow:hidden;border-radius:99px;background:rgba(255,255,255,.06)}.card-meter span{display:block;height:100%;border-radius:inherit;background:var(--tone)}.card-l{margin-top:8px;color:var(--faint);font-size:11px}
.state-message{display:flex;align-items:center;gap:14px;padding:19px 20px;border:1px solid rgba(70,214,154,.18);border-radius:16px;background:linear-gradient(90deg,var(--green-soft),rgba(70,214,154,.03))}.state-message.compact{padding:16px 18px}.state-icon{display:grid;place-items:center;flex:0 0 auto;width:34px;height:34px;border-radius:11px;background:rgba(70,214,154,.14);color:#9cf0ca;font-weight:900}.state-message strong{font-size:13px}.state-message p{margin:2px 0 0;color:var(--muted);font-size:12px}.queue-heading{margin-top:18px}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:16px;background:var(--surface)}table{width:100%;border-collapse:collapse;font-size:13px}thead th{padding:11px 14px;text-align:left;white-space:nowrap;color:var(--faint);background:var(--surface-raised);border-bottom:1px solid var(--line);font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.08em}tbody td{padding:12px 14px;border-bottom:1px solid var(--line)}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:rgba(255,255,255,.018)}td.num,th.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}tr.over{background:var(--red-soft)}tr.over td:first-child{box-shadow:inset 3px 0 0 var(--red)}
.pill{display:inline-flex;padding:3px 8px;border-radius:999px;border:1px solid transparent;font-size:10.5px;font-weight:750;white-space:nowrap;text-transform:capitalize}.pill.green{background:var(--green-soft);color:#91ecc2}.pill.red{background:var(--red-soft);color:#ff9ca4}.pill.yellow{background:var(--yellow-soft);color:#f8d891}.pill.amber{background:var(--amber-soft);color:#f5c18b}.pill.blue{background:var(--blue-soft);color:#a7c8ff}.pill.teal{background:var(--teal-soft);color:#95ece6}.pill.cyan{background:var(--cyan-soft);color:#9ee9f6}.pill.neutral{background:var(--neutral-soft);color:#c5cdd5}
.stale-list{display:grid;gap:10px;list-style:none;margin:0;padding:0}.stale-list li{padding:14px 16px;border:1px solid rgba(255,107,119,.20);border-radius:14px;background:var(--red-soft)}.over-by{color:#ff9ca4;font-weight:750}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}.metric{min-width:0;padding:19px;border:1px solid var(--line);border-radius:16px;background:linear-gradient(155deg,var(--surface-raised),var(--surface))}.metric-top{display:flex;align-items:center;justify-content:space-between;color:var(--faint)}.metric-mark{width:18px;height:3px;border-radius:9px;background:var(--brand)}.metric-v{margin-top:25px;font-size:35px;font-weight:820;line-height:1;letter-spacing:-.045em}.metric-k{margin-top:8px;font-size:12.5px;font-weight:720}.metric-s{min-height:35px;margin-top:4px;color:var(--muted);font-size:11.5px}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.panel{min-width:0;padding:20px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(155deg,var(--surface-raised),var(--surface))}.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}.panel h3{margin:0;font-size:14px}.panel-note{color:var(--faint);font-size:10.5px}
.bars{display:flex;flex-direction:column;gap:12px}.bar-row{display:grid;grid-template-columns:minmax(90px,130px) 1fr auto;align-items:center;gap:11px}.bar-label{min-width:0;overflow:hidden;color:var(--muted);font-size:11.5px;text-overflow:ellipsis;white-space:nowrap}.course-link{display:inline-flex;align-items:center;gap:5px;max-width:100%;color:var(--muted);text-decoration:none}.course-link span{color:var(--brand);font-size:9px}.course-link:hover{color:var(--text)}
.faculty-group{margin-top:20px}.faculty-group:first-child{margin-top:0}.faculty-head{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:11px;padding-bottom:8px;border-bottom:1px solid var(--line)}.faculty-name{font-size:13px;font-weight:750;color:var(--text)}.faculty-slug{color:var(--brand);font-size:10px;font-weight:800;letter-spacing:.06em}.faculty-agg{margin-left:auto;font-size:11px}
.bar-track{display:flex;overflow:hidden;height:9px;border-radius:99px;background:rgba(255,255,255,.06)}.bar-seg{height:100%}.bar-seg.green{background:linear-gradient(90deg,#2fbd85,var(--green))}.bar-seg.blue{background:var(--blue)}.bar-seg.teal{background:linear-gradient(90deg,#35bdb3,var(--teal))}.bar-seg.cyan{background:linear-gradient(90deg,var(--blue),var(--cyan))}.bar-val{min-width:54px;text-align:right;font-size:11.5px;font-weight:700}.legend{display:flex;gap:14px;margin-top:16px;color:var(--muted);font-size:10.5px}.legend .sw{display:inline-block;width:7px;height:7px;margin-right:5px;border-radius:50%}.sw.green{background:var(--green)}.sw.blue{background:var(--blue)}
.chart{display:flex;align-items:flex-end;gap:7px;height:165px;padding-top:20px;overflow-x:auto}.cbar-col{display:flex;flex:1 1 0;min-width:25px;height:100%;flex-direction:column;align-items:center;justify-content:flex-end}.cbar{width:64%;max-width:32px;min-height:2px;border-radius:5px 5px 2px 2px;background:linear-gradient(180deg,var(--brand),var(--brand-strong))}.cbar[data-zero]{background:rgba(255,255,255,.07)}.cbar-n{margin-bottom:4px;color:#ff9ca4;font-size:10.5px;font-weight:750}.cbar-x{margin-top:7px;color:var(--muted);font-size:10px}.cbar-y{font-size:9px}
.site-footer{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-top:48px;padding-top:20px;border-top:1px solid var(--line);color:var(--faint);font-size:11px}.site-footer a{color:var(--muted);text-decoration:none}.site-footer a:hover{color:var(--brand)}.footer-badge{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}.footer-badge::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--brand)}
.course-hero .hero-copy{display:block}.back-link{display:inline-flex;align-items:center;gap:7px;margin-bottom:22px;color:var(--muted);font-size:11.5px;font-weight:700;text-decoration:none}.back-link:hover{color:var(--brand)}.course-code{color:var(--brand);font-family:"SFMono-Regular",Consolas,monospace;font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.course-name{margin:7px 0 0;font-size:clamp(31px,5vw,48px);line-height:1.04;letter-spacing:-.045em}.course-description{max-width:620px;margin:12px 0 24px;color:var(--muted);font-size:13px}.course-video-table td{vertical-align:middle}.video-identity{min-width:235px}.video-title{color:var(--text);font-size:12.5px;font-weight:700}.video-code{max-width:360px;margin-top:3px;overflow:hidden;color:var(--faint);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.progress-cell{min-width:145px}.video-progress-row{display:flex;align-items:center;gap:9px}.video-progress-track{width:92px;height:6px;overflow:hidden;border-radius:99px;background:rgba(255,255,255,.07)}.video-progress-fill{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--brand-strong),var(--brand))}.video-progress-value{min-width:31px;color:var(--muted);font-size:10.5px;text-align:right}.video-progress-na{color:var(--faint);font-size:10.5px}
@media(max-width:900px){.hero{grid-template-columns:1fr}.health-card{min-height:210px}.metrics{grid-template-columns:1fr 1fr}.metric:last-child{grid-column:1/-1}}
@media(max-width:700px){.wrap{padding:20px 15px 52px}.site-header{align-items:flex-start}.header-meta{align-items:flex-end;flex-direction:column;gap:6px;text-align:right}.hero{padding:24px;border-radius:20px}.summary-grid{grid-template-columns:1fr 1fr}.grid2,.metrics{grid-template-columns:1fr}.metric:last-child{grid-column:auto}.section-heading{align-items:flex-start;flex-direction:column;gap:5px}.section-aside{white-space:normal}.bar-row{grid-template-columns:84px 1fr auto}.site-footer{align-items:flex-start;flex-direction:column}}
@media(max-width:430px){.brand-title{font-size:15px}.header-time{display:none}.health-ring{width:140px;height:140px}.health-ring::before{width:114px;height:114px}.summary-item{padding:12px}.summary-value{font-size:22px}.card{padding:16px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.card{transition:none}}
"""


def render(d, gen_amman):
    ts = gen_amman.strftime("%Y-%m-%d %H:%M")
    completion = ((d["delivered_total"] / d["total_videos"] * 100)
                  if d["total_videos"] else 0)
    attention_count = len(d["stale"])
    parts = [f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="1800"><meta name="theme-color" content="#071018">
<meta name="description" content="D.Learn video production overview">
<title>D.Learn Production Control</title><style>{CSS}</style></head><body><div class="wrap">
<header class="site-header"><div class="brand-lockup"><div class="brand-mark" aria-hidden="true">DLC</div>
<div><div class="brand-kicker">D.Learn</div><div class="brand-title">Production Control</div></div></div>
<div class="header-meta"><span class="live-chip"><span class="live-dot" aria-hidden="true"></span>Live mirror</span>
<span class="header-time">Updated <b>{e(ts)}</b> · Amman</span></div></header><main>
<section class="hero" aria-label="Production overview"><div class="hero-copy">
<div class="summary-grid" aria-label="Production summary">
<div class="summary-item"><div class="summary-value">{d["total_videos"]}</div><div class="summary-label">Total videos</div></div>
<div class="summary-item"><div class="summary-value">{d["delivered_total"]}</div><div class="summary-label">Delivered</div></div>
<div class="summary-item"><div class="summary-value">{d["inflight_total"]}</div><div class="summary-label">In progress</div></div>
<div class="summary-item alert"><div class="summary-value">{attention_count}</div><div class="summary-label">Need attention</div></div>
</div></div><aside class="health-card" aria-label="{completion:.0f}% of tracked videos delivered">
<div class="health-ring" style="--completion:{completion:.2f}%"><div class="health-inner">
<span class="health-value">{completion:.0f}%</span><span class="health-unit">complete</span></div></div>
<div class="health-title">Overall delivery progress</div>
<div class="health-sub">{d["delivered_total"]} of {d["total_videos"]} tracked videos delivered</div></aside></section>
"""]
    parts.append("""<section class="dashboard-section" aria-labelledby="pipeline-title">
<div class="section-heading"><div><div class="section-kicker">01 · Pipeline</div>
<h2 class="section-title" id="pipeline-title">Where the work stands</h2>
<p class="section-copy">A live distribution of every tracked video across the production workflow.</p>
</div><div class="section-aside">Refreshes automatically every 30 minutes</div></div>""")
    parts.append(render_status_cards(d))
    parts.append("</section>")
    parts.append("""<section class="dashboard-section" aria-labelledby="attention-title">
<div class="section-heading"><div><div class="section-kicker">02 · Attention</div>
<h2 class="section-title" id="attention-title">SLA watch</h2>
<p class="section-copy">Courses with one or more videos past their state SLA. Open a course to see the specific videos.</p>
</div><div class="section-aside">Priority signal, not total volume</div></div>""")
    parts.append(render_course_attention(d))
    parts.append("</section>")
    parts.append("""<section class="dashboard-section" aria-labelledby="performance-title">
<div class="section-heading"><div><div class="section-kicker">03 · Performance</div>
<h2 class="section-title" id="performance-title">Throughput &amp; delivery</h2>
<p class="section-copy">Cycle speed, review effort and output volume in one compact view.</p>
</div><div class="section-aside">Rolling operational history</div></div>""")
    parts.append(render_metrics(d))
    parts.append('<div class="grid2">')
    parts.append('<article class="panel"><div class="panel-heading"><h3>Delivery by faculty &amp; course</h3><span class="panel-note">Delivered / total</span></div>' + render_course_totals(d) + '</article>')
    parts.append('<article class="panel"><div class="panel-heading"><h3>Monthly deliveries</h3><span class="panel-note">Last 12 months</span></div>' + render_deliveries_chart(d) + '</article></div></section>')
    parts.append("""<section class="dashboard-section" aria-labelledby="portfolio-title">
<div class="section-heading"><div><div class="section-kicker">04 · Portfolio</div>
<h2 class="section-title" id="portfolio-title">Content mix</h2>
<p class="section-copy">How the current video library is distributed by format and course.</p></div></div><div class="grid2">""")
    parts.append('<article class="panel"><div class="panel-heading"><h3>By video type</h3><span class="panel-note">Share of library</span></div>' + render_type_breakdown(d) + '</article>')
    parts.append('<article class="panel"><div class="panel-heading"><h3>By course</h3><span class="panel-note">Share of library</span></div>' + render_course_breakdown(d) + '</article></div></section>')
    parts.append(f"""</main><footer class="site-footer">
<span>Data from D.Learn operations · generated by <span class="mono">generate.py</span></span>
<span class="footer-badge">Read-only mirror · <a href="{e(REPO_URL)}">View repository</a></span>
</footer></div></body></html>""")
    return "".join(parts)


def render_course_page(course, gen_amman):
    ts = gen_amman.strftime("%Y-%m-%d %H:%M")
    completion = ((course["delivered_total"] / course["total_videos"] * 100)
                  if course["total_videos"] else 0)
    video_word = "video" if course["total_videos"] == 1 else "videos"
    page_title = f'{course["name"]} · D.Learn Production Control'
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="1800"><meta name="theme-color" content="#071018">
<meta name="description" content="Production progress for {e(course["name"])}">
<title>{e(page_title)}</title><style>{CSS}</style></head><body><div class="wrap">
<header class="site-header"><a class="brand-lockup" href="../../" aria-label="Back to production overview">
<div class="brand-mark" aria-hidden="true">DLC</div><div><div class="brand-kicker">D.Learn</div>
<div class="brand-title">Production Control</div></div></a>
<div class="header-meta"><span class="live-chip"><span class="live-dot" aria-hidden="true"></span>Live mirror</span>
<span class="header-time">Updated <b>{e(ts)}</b> · Amman</span></div></header><main>
<section class="hero course-hero" aria-labelledby="course-title"><div class="hero-copy">
<a class="back-link" href="../../"><span aria-hidden="true">←</span> All courses</a>
<div class="course-code">{e(course["code"])}</div><h1 class="course-name" id="course-title">{e(course["name"])}</h1>
<p class="course-description">A detailed view of every video in this course, its current workflow state and delivery progress.</p>
<div class="summary-grid" aria-label="Course production summary">
<div class="summary-item"><div class="summary-value">{course["total_videos"]}</div><div class="summary-label">Total videos</div></div>
<div class="summary-item"><div class="summary-value">{course["delivered_total"]}</div><div class="summary-label">Delivered</div></div>
<div class="summary-item"><div class="summary-value">{course["inflight_total"]}</div><div class="summary-label">In progress</div></div>
<div class="summary-item alert"><div class="summary-value">{course["attention_count"]}</div><div class="summary-label">Need attention</div></div>
</div></div><aside class="health-card" aria-label="{completion:.0f}% of course videos delivered">
<div class="health-ring" style="--completion:{completion:.2f}%"><div class="health-inner">
<span class="health-value">{completion:.0f}%</span><span class="health-unit">complete</span></div></div>
<div class="health-title">Course delivery progress</div>
<div class="health-sub">{course["delivered_total"]} of {course["total_videos"]} {video_word} delivered</div></aside></section>
<section class="dashboard-section" aria-labelledby="course-pipeline-title">
<div class="section-heading"><div><div class="section-kicker">01 · Course pipeline</div>
<h2 class="section-title" id="course-pipeline-title">Status distribution</h2>
<p class="section-copy">The current workflow state of every video in {e(course["name"])}.</p>
</div><div class="section-aside">{course["total_videos"]} {video_word} tracked</div></div>
{render_status_cards(course)}</section>
<section class="dashboard-section" aria-labelledby="video-progress-title">
<div class="section-heading"><div><div class="section-kicker">02 · Video detail</div>
<h2 class="section-title" id="video-progress-title">Video progress</h2>
<p class="section-copy">Titles, current states, pipeline completion and review rounds.</p>
</div><div class="section-aside">Ordered by video title</div></div>{render_course_video_table(course)}</section>
</main><footer class="site-footer"><span>Data from D.Learn operations · generated by <span class="mono">generate.py</span></span>
<span class="footer-badge">Read-only mirror · <a href="{e(REPO_URL)}">View repository</a></span>
</footer></div></body></html>"""


def write_course_pages(data, gen_amman):
    os.makedirs(COURSES_DIR, exist_ok=True)
    previous = set()
    if os.path.exists(COURSES_MANIFEST):
        with open(COURSES_MANIFEST, "r", encoding="utf-8") as fh:
            previous = {line.strip() for line in fh if line.strip()}
    current = {f'{course["slug"]}/index.html' for course in data["course_details"]}
    for relative in sorted(previous - current):
        if not re.fullmatch(r"[a-z0-9-]+/index\.html", relative):
            continue
        old_path = os.path.join(COURSES_DIR, relative)
        if os.path.isfile(old_path):
            os.remove(old_path)
        try:
            os.rmdir(os.path.dirname(old_path))
        except OSError:
            pass
    for course in data["course_details"]:
        course_dir = os.path.join(COURSES_DIR, course["slug"])
        os.makedirs(course_dir, exist_ok=True)
        with open(os.path.join(course_dir, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(render_course_page(course, gen_amman))
    with open(COURSES_MANIFEST, "w", encoding="utf-8") as fh:
        for relative in sorted(current):
            fh.write(relative + "\n")
    return len(current)


def resolve_now_utc():
    """Current UTC time, overridable via CRAFTED_NOW for reproducible tests.

    Production leaves CRAFTED_NOW unset and uses the wall clock. Tests set it to
    an ISO/DB timestamp to render at a fixed instant (e.g. to prove that a page
    generated an hour later is treated as unchanged once the clock and age cells
    are normalised out).
    """
    override = os.environ.get("CRAFTED_NOW")
    if override:
        dt = parse_utc(override)
        if dt is None:
            raise SystemExit(f"error: bad CRAFTED_NOW {override!r} "
                             "(expected 'YYYY-MM-DD HH:MM:SS')")
        return dt
    return datetime.now(timezone.utc)


def main():
    now_utc = resolve_now_utc()
    gen_amman = now_utc.astimezone(AMMAN)
    conn = connect_ro(DB_PATH)
    try:
        data = collect(conn, now_utc)
    finally:
        conn.close()
    page = render(data, gen_amman)
    for path in (OUT_PATH, INDEX_PATH):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
    course_page_count = write_course_pages(data, gen_amman)
    size_kb = len(page.encode("utf-8")) / 1024
    print(f"wrote dashboard.html + index.html ({size_kb:.1f} KB each) "
          f"+ {course_page_count} course pages")
    print(f"  videos={data['total_videos']} delivered={data['delivered_total']} "
          f"in-flight={data['inflight_total']} stale={len(data['stale'])}")
    if size_kb > 200:
        print("WARNING: output exceeds 200KB target", file=sys.stderr)


if __name__ == "__main__":
    main()
