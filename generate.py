#!/usr/bin/env python3
"""Generate the CraftED production dashboard as self-contained HTML pages.

Reads the CraftED ops SQLite DB STRICTLY READ-ONLY (sqlite `mode=ro` URI) and
writes ``dashboard.html``, ``index.html``, and per-course pages next to this
script. No external assets, no CDNs — the pages load on any device/network. This
script NEVER writes to the DB or anywhere under the ops directory; it only reads.

Run:  python3 generate.py
"""

import base64
import html
import math
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
    # Same qualifying deliveries, bucketed by their delivered month (t1), so the
    # monthly chart can reuse this exact rule (see "Deliveries per month" below).
    qualifying_by_month = {}
    for s in spans:
        t0, t1 = parse_utc(s["t0"]), parse_utc(s["t1"])
        if t0 and t1:
            hrs = (t1 - t0).total_seconds() / 3600.0
            if hrs >= MIN_CYCLE_HOURS:
                qualifying.append(hrs)
                ym = f"{t1.year:04d}-{t1.month:02d}"
                qualifying_by_month[ym] = qualifying_by_month.get(ym, 0) + 1
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
    # Count only QUALIFYING deliveries — the same submitted->delivered span rule
    # (>= MIN_CYCLE_HOURS) the avg-cycle-time metric uses to exclude retroactively
    # registered pre-system videos. Those bulk rows collapse to ~0h and drop out
    # here too, so the chart shows real production cadence, not the seed import.
    # (Every other panel still counts all delivered videos; this filter is
    # display-only and confined to this chart.)
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
        (f"{yy:04d}-{mm:02d}", qualifying_by_month.get(f"{yy:04d}-{mm:02d}", 0))
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


def _tone(status):
    """Map a pipeline status to a CSS tone class (drives dot/pill/bar colour)."""
    return "s-" + STATUS_CLASS.get(status, "neutral")


_RING_C = 2 * math.pi * 90  # circumference of the r=90 donut ring


def render_ring(pct, gid):
    """A 200x200 SVG donut filled to pct, gold gradient, rounded cap."""
    offset = _RING_C * (1 - pct / 100.0)
    return (
        '<svg width="200" height="200" viewBox="0 0 200 200" aria-hidden="true">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#f0cf7a"></stop>'
        '<stop offset="1" stop-color="#e2b64e"></stop></linearGradient></defs>'
        '<circle cx="100" cy="100" r="90" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="12"></circle>'
        f'<circle cx="100" cy="100" r="90" fill="none" stroke="url(#{gid})" stroke-width="12" '
        f'stroke-linecap="round" stroke-dasharray="{_RING_C:.2f}" stroke-dashoffset="{offset:.2f}" '
        'transform="rotate(-90 100 100)"></circle></svg>'
    )


def render_kpis(items):
    """A row of four KPI tiles: [(value, label, colour_class), ...]."""
    tiles = []
    for value, label, cls in items:
        tiles.append(
            f'<div class="kpi"><div class="kpi-v {cls}">{value}</div>'
            f'<div class="kpi-l">{e(label)}</div></div>'
        )
    return f'<div class="kpis">{"".join(tiles)}</div>'


def render_pipeline_cards(d):
    """Big count cards for each pipeline state present (dashboard + course)."""
    present = [(s, d["status_counts"].get(s, 0)) for s in STATUS_ORDER
               if d["status_counts"].get(s, 0) > 0]
    total = d["total_videos"] or 1
    if not present:
        return ('<div class="card pcard"><div class="pcard-foot">'
                'No videos registered yet.</div></div>')
    cards = []
    for status, n in present:
        pct = n / total * 100
        label = status.replace("_", " ")
        cards.append(
            f'<div class="card pcard {_tone(status)}">'
            f'<div class="pcard-head"><span class="dot"></span>{e(label)}</div>'
            f'<div class="pcard-row"><div class="pcard-n">{n}</div>'
            f'<div class="pcard-pct">{pct:.0f}%</div></div>'
            f'<div class="bar6"><div class="bar6-fill" style="width:{pct:.2f}%"></div></div>'
            f'<div class="pcard-foot">of {d["total_videos"]} tracked videos</div></div>'
        )
    return f'<div class="two">{"".join(cards)}</div>'


def render_faculty(d):
    """BY FACULTY partition: each faculty groups its clickable course rows."""
    faculties = d["faculties"]
    if not faculties:
        return ('<div class="card part"><div class="part-label">By faculty</div>'
                '<p class="lead">No courses yet.</p></div>')
    groups = []
    for f in faculties:
        rows = []
        for c in f["courses"]:
            # Segments fill each row relative to the course's own total.
            dpct = (c["delivered"] / c["total"] * 100) if c["total"] else 0
            ipct = (c["inflight"] / c["total"] * 100) if c["total"] else 0
            rows.append(
                f'<a class="crow" href="courses/{e(c["slug"])}/">'
                f'<span class="crow-name"><b>{e(c["name"])}</b>'
                '<i aria-hidden="true">&#8599;</i></span>'
                '<span class="stack">'
                f'<span class="seg green" style="width:{dpct:.2f}%"></span>'
                f'<span class="seg blue" style="width:{ipct:.2f}%"></span></span>'
                f'<span class="crow-ratio"><b>{c["delivered"]}</b>'
                f'<span>/{c["total"]}</span></span></a>'
            )
        cw = "course" if len(f["courses"]) == 1 else "courses"
        groups.append(
            '<div class="fac"><div class="fac-head"><div class="fac-id">'
            f'<span class="fac-name">{e(f["name"])}</span>'
            f'<span class="fac-code">{e(f["slug"])}</span></div>'
            f'<span class="fac-sum">{len(f["courses"])} {cw} '
            f'&middot; {f["delivered"]}/{f["total"]} delivered</span></div>'
            f'<div class="fac-rows">{"".join(rows)}</div></div>'
        )
    legend = ('<div class="legend">'
              '<span class="leg"><span class="leg-dot" style="background:var(--green)"></span>delivered</span>'
              '<span class="leg"><span class="leg-dot" style="background:var(--blue)"></span>in flight</span></div>')
    return (f'<div class="card part"><div class="part-label">By faculty</div>'
            f'{"".join(groups)}{legend}</div>')


_TYPE_TONE = {"video": "var(--gold)", "storyboard": "var(--blue)"}
_TYPE_PALETTE = ["var(--gold)", "var(--blue)", "var(--teal)", "var(--cyan)", "var(--green)"]


def render_type_mix(d):
    """BY TYPE partition: one labelled bar per content format."""
    rows = d["type_counts"]
    if not rows:
        return ('<div class="card part"><div class="part-label">By type</div>'
                '<p class="lead">No videos yet.</p></div>')
    total = sum(n for _, n in rows) or 1
    body = []
    for i, (vtype, n) in enumerate(rows):
        pct = n / total * 100
        color = _TYPE_TONE.get(vtype, _TYPE_PALETTE[i % len(_TYPE_PALETTE)])
        body.append(
            '<div class="trow"><div class="trow-head">'
            f'<span class="trow-name">{e(vtype)}</span>'
            f'<span class="trow-val"><b>{n}</b> &middot; {pct:.0f}%</span></div>'
            f'<div class="bar9"><div class="bar9-fill" '
            f'style="width:{pct:.2f}%;background:{color}"></div></div></div>'
        )
    return (f'<div class="card part"><div class="part-label">By type</div>'
            f'<div class="tmix">{"".join(body)}</div></div>')


def render_throughput(d):
    """Three mini stat cards: speed, quality loop, output."""
    if d["cycle_avg_h"] is None:
        speed_v = "n/a"
        speed_s = (f'avg cycle time &mdash; no delivery has a &ge;1h cool-down span '
                   f'yet ({d["cycle_delivered_total"]} delivered, all instant)')
    else:
        speed_v = fmt_age(d["cycle_avg_h"])
        speed_s = (f'avg cycle time &mdash; over {d["cycle_qualifying"]} of '
                   f'{d["cycle_delivered_total"]} delivered (excludes retroactive)')
    if d["review_avg"] is None:
        q_v = "n/a"
        q_s = "avg review rounds / video &mdash; none have reached review yet"
    else:
        q_v = f'{d["review_avg"]:.2f}'
        rw = "round" if d["review_max"] == 1 else "rounds"
        q_s = (f'avg review rounds / video &mdash; peak: {d["review_max"]} {rw} '
               'on a single video')
    out_s = (f'delivered / total &mdash; {d["inflight_total"]} left to finish'
             if d["inflight_total"] else 'delivered / total &mdash; all delivered')
    cards = [
        ("SPEED", speed_v, speed_s),
        ("QUALITY LOOP", q_v, q_s),
        ("OUTPUT", f'{d["delivered_total"]}<span>/{d["total_videos"]}</span>', out_s),
    ]
    items = []
    for label, val, sub in cards:
        items.append(
            f'<div class="card tcard"><div class="tcard-l">{label}</div>'
            f'<div class="tcard-v">{val}</div><div class="tcard-s">{sub}</div></div>'
        )
    return f'<div class="through">{"".join(items)}</div>'


def render_monthly(d):
    """Monthly deliveries bar chart with an honest empty state."""
    data = d["deliveries_per_month"]
    maxn = max((n for _, n in data), default=0) or 1
    cols = []
    for ym, n in data:
        _, m = ym.split("-")
        label = MONTH_ABBR[int(m)].lower()
        if n == 0:
            bar = '<div class="col-bar zero" style="height:3px"></div>'
        else:
            px = max(6, n / maxn * 110)
            bar = f'<div class="col-bar" style="height:{px:.1f}px"></div>'
        cols.append(
            f'<div class="col" title="{e(ym)}: {n} delivered">{bar}'
            f'<div class="col-x">{label}</div></div>'
        )
    total = sum(n for _, n in data)
    empty = ''
    if total == 0:
        empty = ('<div class="chart-empty"><span>No dated deliveries yet '
                 '&mdash; real production volume will appear here as videos '
                 'ship.</span></div>')
    return ('<div class="card chartcard"><div class="chart-title">Monthly deliveries</div>'
            f'<div class="chart">{"".join(cols)}{empty}</div></div>')


def render_sla_banner(d):
    """SLA watch: a calm green banner, or a red one listing at-risk courses.

    Aggregate/course-name only on the main page (no video codes) per the
    content ruling; the specific over-SLA videos live on each course page.
    """
    courses = [c for c in d["course_details"] if c["attention_count"] > 0]
    if not courses:
        return ('<div class="sla ok"><div class="sla-icon">'
                '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#3ecf8e" '
                'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
                '<path d="M20 6 9 17l-5-5"></path></svg></div>'
                '<div class="sla-body"><div class="sla-title">Every course is within SLA</div>'
                '<div class="sla-sub">No course has a video past its state threshold.</div></div>'
                '<div class="sla-count">0 courses at risk</div></div>')
    courses.sort(key=lambda c: -c["attention_count"])
    items = []
    for c in courses:
        n = c["attention_count"]
        vids = "video" if n == 1 else "videos"
        items.append(
            f'<li><a href="courses/{e(c["slug"])}/">{e(c["name"])} '
            f'<span class="code">{e(c["code"])}</span></a> &mdash; '
            f'<span class="over">{n} {vids} over SLA</span></li>'
        )
    n_at = len(courses)
    cw = "course" if n_at == 1 else "courses"
    return ('<div class="sla risk"><div class="sla-icon">'
            '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#f87171" '
            'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z">'
            '</path><path d="M12 9v4"></path><path d="M12 17h.01"></path></svg></div>'
            f'<div class="sla-body"><div class="sla-title">{n_at} {cw} need attention</div>'
            f'<ul class="sla-list">{"".join(items)}</ul></div>'
            f'<div class="sla-count">{n_at} at risk</div></div>')


def _done_panel(title, sub):
    return ('<div class="card done"><div class="done-icon">'
            '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#3ecf8e" '
            'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
            '<path d="M20 6 9 17l-5-5"></path></svg></div>'
            f'<div class="done-title">{title}</div>'
            f'<div class="done-sub">{sub}</div></div>')


def render_course_video_table(course):
    """Per-video progress table for one course (video codes allowed here).

    A fully-delivered course collapses to a celebratory aggregate panel instead
    of a long, redundant all-delivered table (per the design handoff).
    """
    if course["total_videos"] and course["inflight_total"] == 0:
        vw = "video" if course["delivered_total"] == 1 else "videos"
        return _done_panel(
            f'All {course["delivered_total"]} {vw} delivered',
            'This course is fully delivered &mdash; no videos remain in the '
            'production pipeline.')
    if not course["videos"]:
        return _done_panel(
            'No videos registered yet',
            'This page will update automatically when videos are added.')
    rows = []
    for v in course["videos"]:
        tone = _tone(v["status"])
        sla_txt = f'{v["sla_h"]:g}h' if v["sla_h"] is not None else "&mdash;"
        if v["progress"] is None:
            prog = '<div class="v-prog off">Off pipeline</div>'
        else:
            prog = (f'<div class="v-prog {tone}"><div class="bar5">'
                    f'<span style="width:{v["progress"]:.2f}%"></span></div>'
                    f'<span class="pct">{v["progress"]:.0f}%</span></div>')
        rows.append(
            '<div class="vrow vt-grid">'
            f'<div><div class="v-title">{e(v["title"])}</div>'
            f'<div class="v-code" title="{e(v["code"])}">{e(v["code"])}</div></div>'
            f'<div class="v-type">{e(v["vtype"])}</div>'
            f'<div><span class="pill {tone}">{e(v["status"].replace("_", " "))}</span></div>'
            f'{prog}'
            f'<div class="v-tis r">{e(fmt_age(v["age_h"]))}</div>'
            f'<div class="v-sla r">{sla_txt}</div>'
            f'<div class="v-rev r">{v["review_rounds"]}</div></div>'
        )
    head = ('<div class="vt-head vt-grid"><div>Video</div><div>Type</div><div>Status</div>'
            '<div>Pipeline progress</div><div class="r">Time in state</div>'
            '<div class="r">SLA</div><div class="r">Reviews</div></div>')
    return f'<div class="card vtable">{head}{"".join(rows)}</div>'


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

CSS = """
:root{
  color-scheme:dark;
  --canvas:#070b12;--card:linear-gradient(180deg,#111a29 0%,#0b111c 100%);
  --line:rgba(255,255,255,.07);--hair:rgba(255,255,255,.06);--hair-soft:rgba(255,255,255,.04);
  --inset:rgba(255,255,255,.03);--track:rgba(255,255,255,.06);
  --gold:#e2b64e;--gold-light:#f0cf7a;--gold-soft:rgba(226,182,78,.12);
  --green:#3ecf8e;--green-soft:rgba(62,207,142,.13);
  --blue:#6b8cff;--blue-soft:rgba(107,140,255,.14);
  --red:#f87171;--red-soft:rgba(248,113,113,.13);
  --yellow:#f5c66a;--yellow-soft:rgba(245,198,106,.14);
  --teal:#59d9cf;--teal-soft:rgba(89,217,207,.14);
  --cyan:#5fd3ea;--cyan-soft:rgba(95,211,234,.14);
  --amber:#f0a65a;--amber-soft:rgba(240,166,90,.14);
  --neutral:#8a93a3;--neutral-soft:rgba(138,147,163,.12);
  --t-bright:#f6f8fc;--t-strong:#f2f5fa;--t-head:#eef2f8;--t-body:#e8ecf3;
  --t-muted:#8a93a3;--t-soft:#c3cad6;--t-dim:#7d8698;--t-faint:#5a6373;--t-faintest:#4d5766;
  --fd:'Space Grotesk',system-ui,-apple-system,sans-serif;
  --fm:'IBM Plex Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
  --fb:'IBM Plex Sans',system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box}html{margin:0;padding:0;scroll-behavior:smooth}
body{margin:0;min-height:100vh;background:radial-gradient(1200px 520px at 72% -12%,rgba(226,182,78,.07),transparent 60%),radial-gradient(900px 500px at 8% 4%,rgba(62,207,142,.05),transparent 55%),var(--canvas);color:var(--t-body);font-family:var(--fb);font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
a{color:var(--gold);text-decoration:none}a:hover{color:var(--gold-light)}
.wrap{max-width:1200px;margin:0 auto;padding:26px 28px 40px}
@keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.view{animation:fadeUp .4s ease both}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.view{animation:none}}

/* Top bar */
.topbar{display:flex;justify-content:space-between;align-items:center;gap:20px;padding:6px 2px 26px}
.brand{display:flex;align-items:center;gap:13px;min-width:0;color:inherit;text-decoration:none}
.brand-logo{display:block;width:42px;height:42px;flex:none}
.wordmark{font-family:var(--fd);font-weight:700;font-size:22px;letter-spacing:-.01em;color:var(--t-strong)}.wordmark span{color:var(--gold)}
.topmeta{display:flex;align-items:center;gap:22px}
.live{display:inline-flex;align-items:center;gap:8px;padding:6px 13px;border:1px solid rgba(62,207,142,.3);border-radius:99px;background:rgba(62,207,142,.08)}
.live-dot{width:7px;height:7px;border-radius:99px;background:var(--green);box-shadow:0 0 8px var(--green)}
.live-txt{font-family:var(--fm);font-size:11px;letter-spacing:.06em;color:var(--green)}
.metaline{font-family:var(--fm);font-size:12px;color:var(--t-faint);white-space:nowrap}.metaline b{font-weight:500;color:var(--t-soft)}

/* Cards / sections */
.card{background:var(--card);border:1px solid var(--line);border-radius:16px}
.section{margin-top:44px}
.section-head{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin-bottom:18px}
.eyebrow{font-family:var(--fm);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);margin-bottom:10px}
.h1{margin:0;font-family:var(--fd);font-weight:700;font-size:34px;letter-spacing:-.02em;color:var(--t-bright);line-height:1.04}
.h2{margin:0;font-family:var(--fd);font-weight:600;font-size:26px;letter-spacing:-.01em;color:var(--t-head)}
.lead{margin:9px 0 0;font-size:13.5px;line-height:1.5;color:var(--t-muted);max-width:520px;text-wrap:pretty}
.aside{font-family:var(--fm);font-size:11px;color:var(--t-faint);white-space:nowrap}

/* Hero */
.hero{position:relative;overflow:hidden;display:grid;grid-template-columns:1fr 300px;gap:40px;align-items:center;padding:32px 36px;border-radius:20px}
.hero-glow{position:absolute;top:-40%;right:18%;width:340px;height:340px;background:radial-gradient(circle,rgba(226,182,78,.13),transparent 65%);pointer-events:none}
.hero-copy{position:relative}
.hero .lead{margin:9px 0 22px;font-size:14px;line-height:1.55;max-width:440px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:var(--inset);border:1px solid var(--line);border-radius:13px;padding:15px 16px}
.kpi-v{font-family:var(--fd);font-weight:600;font-size:30px;line-height:1}
.kpi-l{margin-top:7px;font-size:12px;color:var(--t-dim)}
.c-bright{color:var(--t-strong)}.c-green{color:var(--green)}.c-gold{color:var(--gold)}.c-blue{color:var(--blue)}.c-red{color:var(--red)}.c-faint{color:var(--t-faint)}
.progline{margin-top:22px}
.prog-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.prog-cap{font-family:var(--fm);font-size:11px;letter-spacing:.1em;color:var(--t-muted)}
.prog-val{font-family:var(--fm);font-size:12px;color:var(--t-soft)}
.bar8{height:8px;background:var(--track);border-radius:99px;overflow:hidden}
.bar8-fill{height:100%;border-radius:99px;background:linear-gradient(90deg,var(--gold-light),var(--gold))}
.ring-wrap{display:flex;flex-direction:column;align-items:center}
.ring{position:relative;width:200px;height:200px}
.ring svg{display:block}
.ring-c{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring-pct{font-family:var(--fd);font-weight:700;font-size:42px;line-height:1;color:var(--t-bright)}
.ring-cap{font-family:var(--fm);font-size:10px;letter-spacing:.22em;color:var(--gold);margin-top:4px}
.ring-sub{text-align:center;margin-top:14px}
.ring-title{font-family:var(--fd);font-weight:500;font-size:14px;color:var(--t-head)}
.ring-note{font-family:var(--fm);font-size:11.5px;color:var(--t-faint);margin-top:3px}

/* Pipeline cards */
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.pcard{padding:22px 24px}
.pcard-head{display:flex;align-items:center;gap:9px;font-family:var(--fm);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--t-muted)}
.dot{width:8px;height:8px;border-radius:99px;background:var(--c,var(--neutral))}
.pcard-row{display:flex;justify-content:space-between;align-items:flex-end;margin:16px 0 14px}
.pcard-n{font-family:var(--fd);font-weight:600;font-size:46px;line-height:.9;color:var(--t-bright)}
.pcard-pct{font-family:var(--fd);font-weight:600;font-size:20px;color:var(--c,var(--neutral))}
.bar6{height:6px;background:var(--track);border-radius:99px;overflow:hidden}
.bar6-fill{height:100%;border-radius:99px;background:var(--c,var(--neutral))}
.pcard-foot{margin-top:12px;font-family:var(--fm);font-size:11px;color:var(--t-faint)}

/* Courses */
.courses{display:grid;grid-template-columns:1.7fr 1fr;gap:16px;align-items:start}
.part{padding:22px 24px}
.part-label{font-family:var(--fm);font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--gold);margin-bottom:18px}
.fac+.fac{margin-top:24px}
.fac-head{display:flex;justify-content:space-between;align-items:baseline;gap:14px;margin-bottom:16px}
.fac-id{display:flex;align-items:center;gap:10px;min-width:0}
.fac-name{font-family:var(--fd);font-weight:600;font-size:15px;color:var(--t-head)}
.fac-code{font-family:var(--fm);font-size:10.5px;letter-spacing:.06em;color:var(--gold);background:var(--gold-soft);padding:3px 8px;border-radius:6px;white-space:nowrap}
.fac-sum{font-family:var(--fm);font-size:11.5px;color:var(--t-faint);white-space:nowrap}
.fac-rows{display:flex;flex-direction:column;gap:14px}
.crow{display:grid;grid-template-columns:180px 1fr auto;gap:18px;align-items:center;cursor:pointer;padding:5px 8px;margin:-5px -8px;border-radius:9px;transition:background .15s;color:inherit;text-decoration:none}
.crow:hover{background:rgba(255,255,255,.045)}
.crow-name{display:flex;align-items:center;gap:6px;min-width:0}
.crow-name b{font-weight:400;font-size:13.5px;color:#d6dbe4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.crow-name i{font-style:normal;font-size:12px;color:var(--t-faint);flex:none}
.stack{height:9px;background:var(--track);border-radius:99px;overflow:hidden;display:flex}
.stack .seg{height:100%}.seg.green{background:var(--green)}.seg.blue{background:var(--blue)}
.crow-ratio{font-family:var(--fm);font-size:12.5px;white-space:nowrap;text-align:right;min-width:44px}.crow-ratio b{color:var(--t-strong);font-weight:500}.crow-ratio span{color:var(--t-faint)}
.legend{display:flex;align-items:center;gap:18px;margin-top:20px;padding-top:16px;border-top:1px solid var(--hair)}
.leg{display:flex;align-items:center;gap:7px;font-family:var(--fm);font-size:11px;color:var(--t-muted)}
.leg-dot{width:8px;height:8px;border-radius:99px}
.tmix{display:flex;flex-direction:column;gap:20px}
.trow-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:9px}
.trow-name{font-family:var(--fd);font-weight:500;font-size:14px;color:var(--t-body);text-transform:capitalize}
.trow-val{font-family:var(--fm);font-size:12px;color:var(--t-soft)}.trow-val b{color:var(--t-strong);font-weight:400}
.bar9{height:9px;background:var(--track);border-radius:99px;overflow:hidden}
.bar9-fill{height:100%;border-radius:99px;background:var(--c,var(--gold))}

/* Delivery + throughput */
.delivery{display:grid;grid-template-columns:2fr 1fr;gap:16px}
.chartcard{padding:22px 24px}
.chart-title{font-family:var(--fd);font-weight:500;font-size:15px;color:var(--t-head);margin-bottom:6px}
.chart{position:relative;height:150px;display:flex;align-items:flex-end;gap:10px;margin-top:20px}
.col{flex:1;display:flex;flex-direction:column;align-items:center;gap:9px;height:100%;justify-content:flex-end;min-width:0}
.col-bar{width:100%;border-radius:3px;background:linear-gradient(180deg,var(--gold-light),var(--gold))}
.col-bar.zero{background:rgba(226,182,78,.16)}
.col-x{font-family:var(--fm);font-size:9.5px;color:var(--t-faintest)}
.chart-empty{position:absolute;inset:0 0 22px 0;display:flex;align-items:center;justify-content:center;pointer-events:none}
.chart-empty span{font-family:var(--fm);font-size:11px;color:var(--t-faint);text-align:center;max-width:360px;line-height:1.5;background:rgba(7,11,18,.6);padding:6px 14px;border-radius:8px}
.through{display:flex;flex-direction:column;gap:12px}
.tcard{padding:15px 18px;border-radius:14px}
.tcard-l{font-family:var(--fm);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--t-muted)}
.tcard-v{font-family:var(--fd);font-weight:600;font-size:26px;line-height:1;margin:8px 0 6px;color:var(--t-bright)}.tcard-v span{color:var(--t-faint)}
.tcard-s{font-size:11.5px;line-height:1.45;color:var(--t-faint)}

/* SLA banner */
.sla{display:flex;gap:18px;align-items:center;padding:22px 26px;border-radius:16px;border:1px solid var(--green-soft);background:linear-gradient(90deg,rgba(62,207,142,.11),rgba(62,207,142,.02))}
.sla.risk{border-color:rgba(248,113,113,.3);background:linear-gradient(90deg,rgba(248,113,113,.11),rgba(248,113,113,.02))}
.sla-icon{flex:none;width:44px;height:44px;border-radius:99px;background:rgba(62,207,142,.16);display:flex;align-items:center;justify-content:center}
.sla.risk .sla-icon{background:rgba(248,113,113,.16)}
.sla-body{flex:1;min-width:0}
.sla-title{font-family:var(--fd);font-weight:600;font-size:17px;color:var(--t-head)}
.sla-sub{margin-top:3px;font-size:13px;color:var(--t-muted)}
.sla-count{font-family:var(--fm);font-size:12px;color:var(--green);white-space:nowrap}.sla.risk .sla-count{color:var(--red)}
.sla-list{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:7px}
.sla-list a{color:var(--t-body)}.sla-list a:hover{color:var(--gold)}.sla-list .over{color:var(--red);font-family:var(--fm);font-size:12px}
.sla-list .code{font-family:var(--fm);font-size:11px;color:var(--t-faintest)}

/* Course detail */
.back{display:inline-flex;align-items:center;gap:7px;cursor:pointer;font-size:13px;color:var(--t-muted);margin-bottom:16px;transition:color .15s}
.back:hover{color:var(--gold)}.back span{font-size:15px}
.course-code{font-family:var(--fm);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--gold);margin-bottom:10px}
.hero.course h1{font-size:40px}
.vtable{overflow:hidden}
.vt-grid{display:grid;grid-template-columns:2.2fr .9fr 1fr 1.5fr .9fr .7fr .8fr;gap:16px;align-items:center}
.vt-head{padding:14px 24px;border-bottom:1px solid var(--hair);font-family:var(--fm);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--t-faint)}
.vt-head .r{text-align:right}
.vrow{padding:18px 24px;border-bottom:1px solid var(--hair-soft)}.vrow:last-child{border-bottom:0}
.v-title{font-family:var(--fd);font-weight:500;font-size:14px;color:var(--t-strong)}
.v-code{margin-top:4px;font-family:var(--fm);font-size:10px;color:var(--t-faintest);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.v-type{font-size:12.5px;color:var(--t-muted);text-transform:capitalize}
.pill{display:inline-flex;font-family:var(--fm);font-size:10.5px;letter-spacing:.04em;padding:4px 10px;border-radius:99px;text-transform:capitalize;color:var(--c,var(--neutral));background:var(--cs,var(--neutral-soft))}
.v-prog{display:flex;align-items:center;gap:10px}
.v-prog .bar5{flex:1;height:5px;background:var(--track);border-radius:99px;overflow:hidden}
.v-prog .bar5 span{display:block;height:100%;border-radius:99px;background:var(--c,var(--neutral))}
.v-prog .pct{font-family:var(--fm);font-size:11px;color:var(--t-muted);width:34px;text-align:right}
.v-prog.off{color:var(--t-faint);font-family:var(--fm);font-size:11px}
.r{text-align:right}
.v-tis{font-family:var(--fm);font-size:12px;color:var(--t-soft)}
.v-sla{font-family:var(--fm);font-size:12px;color:var(--t-muted)}
.v-rev{font-family:var(--fm);font-size:12px;color:var(--t-soft)}
.done{padding:40px;display:flex;flex-direction:column;align-items:center;text-align:center;gap:14px;border-color:rgba(62,207,142,.22)}
.done-icon{width:52px;height:52px;border-radius:99px;background:rgba(62,207,142,.16);display:flex;align-items:center;justify-content:center}
.done-title{font-family:var(--fd);font-weight:600;font-size:20px;color:var(--t-head)}
.done-sub{font-size:13.5px;color:var(--t-muted);max-width:380px;line-height:1.5}

/* Footer */
.footer{margin-top:52px;padding-top:22px;border-top:1px solid var(--hair);display:flex;justify-content:space-between;align-items:center;gap:20px}
.foot-l{font-family:var(--fm);font-size:11.5px;color:var(--t-faintest)}.foot-l span{color:var(--t-muted)}
.foot-r{display:flex;align-items:center;gap:20px;font-family:var(--fm);font-size:11.5px;color:var(--t-faint)}
.foot-badge{display:flex;align-items:center;gap:7px}.foot-badge::before{content:"";width:6px;height:6px;border-radius:99px;background:var(--gold)}

/* Status tones (dot / pill / bar via --c/--cs) */
.s-green{--c:var(--green);--cs:var(--green-soft)}
.s-blue{--c:var(--blue);--cs:var(--blue-soft)}
.s-gold{--c:var(--gold);--cs:var(--gold-soft)}
.s-red{--c:var(--red);--cs:var(--red-soft)}
.s-yellow{--c:var(--yellow);--cs:var(--yellow-soft)}
.s-teal{--c:var(--teal);--cs:var(--teal-soft)}
.s-cyan{--c:var(--cyan);--cs:var(--cyan-soft)}
.s-amber{--c:var(--amber);--cs:var(--amber-soft)}
.s-neutral{--c:var(--neutral);--cs:var(--neutral-soft)}

/* Responsive */
@media(max-width:900px){
  .hero,.hero.course{grid-template-columns:1fr;gap:28px}
  .ring-wrap{order:-1}
  .courses{grid-template-columns:1fr}
  .delivery{grid-template-columns:1fr}
}
@media(max-width:680px){
  .wrap{padding:20px 16px 36px}
  .topbar{flex-wrap:wrap;gap:14px}
  .topmeta{gap:14px}
  .kpis{grid-template-columns:1fr 1fr}
  .two{grid-template-columns:1fr}
  .h1,.hero.course h1{font-size:28px}
  .h2{font-size:22px}
  .section-head{flex-direction:column;align-items:flex-start;gap:6px}
  .vt-head{display:none}
  .vt-grid{grid-template-columns:1fr 1fr;gap:10px}
  .vrow{padding:16px 18px}
  .v-prog{grid-column:1/-1}
  .footer{flex-direction:column;align-items:flex-start;gap:12px}
}
"""

# Fonts embedded as base64 woff2 so every page stays self-contained (no CDN /
# no external request — the same rule as the logo). Source files live in
# assets/fonts/ (OFL, from Google Fonts): Space Grotesk + IBM Plex Sans are
# variable (one file spans their weights); IBM Plex Mono ships one file per
# weight. They are read and inlined at build time, not referenced at runtime.
_FONT_FILES = [
    ("Space Grotesk", "400 700", "spacegrotesk.woff2"),
    ("IBM Plex Sans", "400 500", "ibmplexsans.woff2"),
    ("IBM Plex Mono", "400", "ibmplexmono400.woff2"),
    ("IBM Plex Mono", "500", "ibmplexmono500.woff2"),
    ("IBM Plex Mono", "600", "ibmplexmono600.woff2"),
]


def _build_font_face_css():
    rules = []
    for family, weight, fname in _FONT_FILES:
        path = os.path.join(_HERE, "assets", "fonts", fname)
        if not os.path.exists(path):
            raise SystemExit(f"error: embedded font missing at {path}")
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        rules.append(
            f"@font-face{{font-family:'{family}';font-style:normal;"
            f"font-weight:{weight};font-display:swap;"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2')}}"
        )
    return "".join(rules)


FONT_FACE_CSS = _build_font_face_css()



# The CraftED brand MARK, icon-only (gold tile + teal play/ring), inlined so the
# pages stay self-contained — no external request, crisp on any DPI. Derived from
# assets/Crafted Logo.svg: the tile <rect> plus the teal play/ring paths, with the
# Illustrator <style> classes flattened to presentation-attribute fills
# (fill="#e2b64e" gold / "#103c40" teal) so nothing collides with the page CSS,
# and the viewBox tightened to the tile's bounds. The "CraftED" wordmark is set
# beside it as live text in Space Grotesk (see render_header), not baked into the
# SVG. Sized via the .brand-logo CSS rule.
LOGO_MARK = (
    '<svg class="brand-logo" xmlns="http://www.w3.org/2000/svg" viewBox="155.41 292.85 461.32 428.01" aria-hidden="true">'
    '<rect fill="#e2b64e" x="161.41" y="298.85" width="449.32" height="416.01" rx="74.26" ry="74.26"/>'
    '<path fill="#103c40" d="M405.43,644.82l-.51-3.57c14.38-2.04,28.23-6.42,41.18-13l1.64,3.22c-13.3,6.76-27.54,11.25-42.31,13.36ZM365.47,644.51c-13.97-2.22-27.5-6.63-40.21-13.12-.63-.32-1.25-.64-1.86-.97l1.69-3.19c.6.32,1.21.63,1.82.95,12.37,6.32,25.54,10.62,39.13,12.78l-.57,3.56ZM480.29,608.26l-2.51-2.6c10.37-10.03,18.99-21.69,25.62-34.67l3.26,1.55-.05.1c-6.8,13.32-15.66,25.3-26.31,35.61ZM291.23,606.68c-10.53-10.53-19.2-22.68-25.76-36.12l3.24-1.58c6.39,13.08,14.82,24.91,25.07,35.16l-2.55,2.55ZM519.36,534.68l-3.56-.63c1.31-7.44,1.97-15.04,1.97-22.61,0-6.86-.55-13.78-1.62-20.57l3.57-.57c1.11,6.98,1.67,14.09,1.67,21.14,0,7.77-.68,15.59-2.03,23.23ZM253.43,532.45c-1.12-7.01-1.68-14.15-1.68-21.22,0-7.74.68-15.53,2.01-23.15l3.56.62c-1.3,7.41-1.96,14.99-1.96,22.52,0,6.89.55,13.84,1.64,20.65l-3.57.57ZM504.46,453.77c-6.38-13.08-14.8-24.92-25.05-35.17l2.55-2.55c10.53,10.54,19.19,22.7,25.74,36.14l-3.25,1.58ZM269.66,451.82l-3.21-1.65.07-.13c6.83-13.37,15.7-25.37,26.39-35.69l2.51,2.6c-10.4,10.04-19.04,21.73-25.68,34.72l-.08.16ZM448.13,395.49c-.63-.33-1.26-.66-1.89-.98-12.35-6.31-25.49-10.6-39.05-12.76l.57-3.57c13.94,2.22,27.44,6.63,40.12,13.11.65.33,1.3.67,1.94,1.01l-1.69,3.19ZM327.1,394.39l-1.63-3.22c13.31-6.75,27.55-11.24,42.32-13.33l.51,3.57c-14.38,2.04-28.24,6.4-41.19,12.97Z"/>'
    '<path fill="#103c40" d="M371.69,644.35c-.02-8.31,6.71-15.05,15.02-15.06h0c8.31-.02,15.05,6.71,15.06,15.02h0c.02,8.31-6.71,15.06-15.02,15.07h-.02c-8.3,0-15.03-6.73-15.04-15.03ZM299.72,631.23c-6.73-4.88-8.22-14.29-3.34-21.01h0c4.88-6.73,14.28-8.22,21.01-3.34h0c6.72,4.88,8.22,14.28,3.34,21.01h0c-2.94,4.06-7.53,6.21-12.19,6.21h0c-3.06,0-6.15-.93-8.82-2.87ZM452.69,627.73c-4.9-6.71-3.42-16.12,3.29-21.02h0c6.71-4.9,16.13-3.42,21.02,3.29h0c4.9,6.72,3.42,16.13-3.29,21.02h0c-2.68,1.96-5.78,2.89-8.85,2.89h0c-4.65,0-9.23-2.14-12.17-6.18ZM245.82,557.32c-2.58-7.89,1.73-16.39,9.63-18.97h0c7.9-2.58,16.4,1.74,18.98,9.63h0c2.57,7.9-1.74,16.4-9.64,18.98h0c-1.55.5-3.12.74-4.67.74h0c-6.33,0-12.23-4.03-14.3-10.38ZM508.48,566.67c-7.91-2.56-12.24-11.04-9.68-18.95h0c2.56-7.9,11.05-12.24,18.95-9.67h0c7.9,2.56,12.24,11.04,9.68,18.95h0c-2.06,6.36-7.97,10.41-14.31,10.41h0c-1.54,0-3.1-.24-4.64-.74ZM255.36,484.79c-7.91-2.55-12.25-11.03-9.7-18.94h0c2.55-7.9,11.03-12.24,18.94-9.69h0c7.9,2.55,12.25,11.03,9.7,18.93h0c-2.06,6.38-7.96,10.43-14.32,10.43h0c-1.53,0-3.09-.23-4.62-.73ZM498.76,474.83c-2.57-7.9,1.75-16.39,9.66-18.96h0c7.9-2.57,16.39,1.75,18.95,9.66h0c2.57,7.9-1.75,16.38-9.65,18.95h0c-1.55.51-3.11.75-4.65.75h0c-6.34,0-12.24-4.04-14.31-10.4ZM296.02,412.79c-4.9-6.71-3.44-16.12,3.27-21.03h0c6.71-4.9,16.12-3.44,21.02,3.27h0c4.91,6.71,3.45,16.12-3.26,21.02h0c-2.68,1.96-5.79,2.91-8.87,2.91h0c-4.64-.01-9.21-2.14-12.16-6.17ZM455.87,415.89h0c-6.72-4.89-8.21-14.3-3.32-21.02h0c4.89-6.72,14.3-8.2,21.02-3.32h0c6.72,4.89,8.2,14.3,3.31,21.02h0c-2.94,4.05-7.52,6.2-12.17,6.2h0c-3.07,0-6.17-.94-8.84-2.88ZM371.21,378.39c-.02-8.31,6.7-15.06,15-15.08h.36c8.31,0,15.05,6.73,15.05,15.04h0c0,8.31-6.74,15.04-15.05,15.04h-.06c-.07.01-.15.01-.22.01h-.04c-8.29,0-15.03-6.72-15.04-15.01Z"/>'
    '<path fill="#103c40" d="M358.89,572.85c-.79.08-2.35.09-3.13,0-7.62-.9-10.5-8.52-10.69-15.26.18-35.37-.39-70.78.29-106.12,2.48-13.82,11.98-14.34,22.83-8.55,29.97,15.97,58.61,34.63,88.48,50.8,10.35,5.95,12.16,16.16,1.93,23.54-30.46,16.65-59.79,35.68-90.42,52-2.75,1.47-6.17,3.27-9.3,3.61v-.02h.01Z"/>'
    '</svg>'
)


def render_header(ts, brand_href=None):
    """Top bar: brand (mark + Space Grotesk wordmark), live badge, timestamp."""
    inner = (f'{LOGO_MARK}<span class="wordmark">Craft<span>ED</span></span>')
    if brand_href:
        brand = (f'<a class="brand" href="{e(brand_href)}" '
                 'aria-label="CraftED — back to production overview">'
                 f'{inner}</a>')
    else:
        brand = f'<div class="brand" aria-label="CraftED">{inner}</div>'
    return (
        f'<div class="topbar">{brand}<div class="topmeta">'
        '<span class="live"><span class="live-dot" aria-hidden="true"></span>'
        '<span class="live-txt">Live mirror</span></span>'
        f'<span class="metaline">Updated <b>{e(ts)}</b></span>'
        '<span class="metaline">Amman</span></div></div>'
    )


def render_footer():
    return (
        '<div class="footer"><div class="foot-l">Data from CraftED operations '
        '&middot; generated by <span>generate.py</span></div>'
        '<div class="foot-r"><span class="foot-badge">Read-only mirror</span>'
        f'<a href="{e(REPO_URL)}">View repository</a></div></div>'
    )


def _doc_head(title, desc):
    return (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="refresh" content="1800">'
        '<meta name="theme-color" content="#070b12">'
        f'<meta name="description" content="{e(desc)}">'
        f'<title>{e(title)}</title>'
        f'<style>{FONT_FACE_CSS}{CSS}</style></head><body><div class="wrap">'
    )


def _hero_kpis(total, delivered, inflight, attention):
    return render_kpis([
        (total, "Total videos", "c-bright"),
        (delivered, "Delivered", "c-green"),
        (inflight, "In progress", "c-gold"),
        (attention, "Need attention", "c-red" if attention else "c-faint"),
    ])


def render(d, gen_amman):
    ts = gen_amman.strftime("%Y-%m-%d %H:%M")
    completion = ((d["delivered_total"] / d["total_videos"] * 100)
                  if d["total_videos"] else 0)
    attention = len(d["stale"])
    ring = render_ring(completion, "ring-dash")
    kpis = _hero_kpis(d["total_videos"], d["delivered_total"],
                      d["inflight_total"], attention)
    p = [_doc_head("CraftED Production Control",
                   "CraftED video production overview")]
    p.append(render_header(ts))
    p.append('<div class="view">')
    # Hero — delivery command center
    p.append(
        '<section class="card hero" aria-label="Production overview">'
        '<div class="hero-glow" aria-hidden="true"></div>'
        '<div class="hero-copy"><div class="eyebrow">Overview</div>'
        '<h1 class="h1">Delivery command center</h1>'
        '<p class="lead">The full production picture at a glance &mdash; how many '
        'videos are tracked, delivered, and still moving through the pipeline '
        'right now.</p>'
        f'{kpis}'
        '<div class="progline"><div class="prog-head">'
        '<div class="prog-cap">Overall delivery progress</div>'
        f'<div class="prog-val">{d["delivered_total"]} of {d["total_videos"]} delivered</div>'
        '</div><div class="bar8">'
        f'<div class="bar8-fill" style="width:{completion:.2f}%"></div></div></div></div>'
        f'<div class="ring-wrap"><div class="ring">{ring}'
        f'<div class="ring-c"><div class="ring-pct">{completion:.0f}%</div>'
        '<div class="ring-cap">COMPLETE</div></div></div>'
        '<div class="ring-sub"><div class="ring-title">Overall delivery progress</div>'
        f'<div class="ring-note">{d["delivered_total"]} of {d["total_videos"]} '
        'tracked videos delivered</div></div></div></section>'
    )
    # 01 · Pipeline
    p.append(
        '<section class="section" aria-label="Pipeline">'
        '<div class="section-head"><div><div class="eyebrow">01 &middot; Pipeline</div>'
        '<h2 class="h2">Where the work stands</h2>'
        '<p class="lead">A live distribution of every tracked video across the '
        'production workflow.</p></div>'
        '<div class="aside">Refreshes every 30 min</div></div>'
        f'{render_pipeline_cards(d)}</section>'
    )
    # 02 · Courses
    p.append(
        '<section class="section" aria-label="Courses">'
        '<div class="section-head"><div><div class="eyebrow">02 &middot; Courses</div>'
        '<h2 class="h2">Course tracker</h2>'
        '<p class="lead">Delivery split across faculties and content formats. Open '
        'any course to inspect its videos, states and review rounds.</p></div>'
        '<div class="aside">Delivered / total</div></div>'
        f'<div class="courses">{render_faculty(d)}{render_type_mix(d)}</div></section>'
    )
    # 03 · Delivery
    p.append(
        '<section class="section" aria-label="Delivery trend">'
        '<div class="section-head"><div><div class="eyebrow">03 &middot; Delivery</div>'
        '<h2 class="h2">Throughput &amp; delivery trend</h2>'
        '<p class="lead">Volume delivered over time, alongside cycle speed, review '
        'effort and output.</p></div>'
        '<div class="aside">Last 12 months</div></div>'
        f'<div class="delivery">{render_monthly(d)}{render_throughput(d)}</div></section>'
    )
    # 04 · Attention
    p.append(
        '<section class="section" aria-label="SLA watch">'
        '<div class="section-head"><div><div class="eyebrow">04 &middot; Attention</div>'
        '<h2 class="h2">SLA watch</h2></div></div>'
        f'{render_sla_banner(d)}</section>'
    )
    p.append('</div>')
    p.append(render_footer())
    p.append('</div></body></html>')
    return "".join(p)


def render_course_page(course, gen_amman):
    ts = gen_amman.strftime("%Y-%m-%d %H:%M")
    completion = ((course["delivered_total"] / course["total_videos"] * 100)
                  if course["total_videos"] else 0)
    vw = "video" if course["total_videos"] == 1 else "videos"
    ring = render_ring(completion, "ring-detail")
    kpis = _hero_kpis(course["total_videos"], course["delivered_total"],
                      course["inflight_total"], course["attention_count"])
    p = [_doc_head(f'{course["name"]} · CraftED Production Control',
                   f'Production progress for {course["name"]}')]
    p.append(render_header(ts, brand_href="../../"))
    p.append('<div class="view">')
    # Hero
    p.append(
        '<section class="card hero course" aria-label="Course overview">'
        '<div class="hero-glow" aria-hidden="true"></div>'
        '<div class="hero-copy">'
        '<a class="back" href="../../"><span aria-hidden="true">&larr;</span> All courses</a>'
        f'<div class="course-code">{e(course["code"])}</div>'
        f'<h1 class="h1">{e(course["name"])}</h1>'
        '<p class="lead">A detailed view of every video in this course &mdash; its '
        'current workflow state and delivery progress.</p>'
        f'{kpis}</div>'
        f'<div class="ring-wrap"><div class="ring">{ring}'
        f'<div class="ring-c"><div class="ring-pct">{completion:.0f}%</div>'
        '<div class="ring-cap">COMPLETE</div></div></div>'
        '<div class="ring-sub"><div class="ring-title">Course delivery progress</div>'
        f'<div class="ring-note">{course["delivered_total"]} of {course["total_videos"]} '
        f'{vw} delivered</div></div></div></section>'
    )
    # 01 · Course pipeline
    p.append(
        '<section class="section" aria-label="Course pipeline">'
        '<div class="section-head"><div><div class="eyebrow">01 &middot; Course pipeline</div>'
        '<h2 class="h2">Status distribution</h2>'
        f'<p class="lead">The current workflow state of every video in '
        f'{e(course["name"])}.</p></div>'
        f'<div class="aside">{course["total_videos"]} {vw} tracked</div></div>'
        f'{render_pipeline_cards(course)}</section>'
    )
    # 02 · Video detail
    p.append(
        '<section class="section" aria-label="Video detail">'
        '<div class="section-head"><div><div class="eyebrow">02 &middot; Video detail</div>'
        '<h2 class="h2">Video progress</h2>'
        '<p class="lead">Titles, current states, pipeline completion and review '
        'rounds.</p></div>'
        '<div class="aside">Ordered by video title</div></div>'
        f'{render_course_video_table(course)}</section>'
    )
    p.append('</div>')
    p.append(render_footer())
    p.append('</div></body></html>')
    return "".join(p)


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
    # ~147KB of that is the three embedded webfonts (constant every build); the
    # threshold leaves generous room for real page content on top of them.
    if size_kb > 260:
        print("WARNING: output exceeds 260KB target", file=sys.stderr)


if __name__ == "__main__":
    main()
