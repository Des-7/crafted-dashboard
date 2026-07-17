#!/usr/bin/env python3
"""Generate the CraftED production dashboard as a single self-contained HTML file.

Reads the CraftED ops SQLite DB STRICTLY READ-ONLY (sqlite `mode=ro` URI) and
writes ``dashboard.html`` next to this script. No external assets, no CDNs — the
page loads on any device/network. This script NEVER writes to the DB or anywhere
under the ops directory; it only reads.

Run:  python3 generate.py
"""

import html
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Absolute path to the ops DB. Read at runtime; the DB is NEVER copied here.
DB_PATH = os.environ.get("CRAFTED_OPS_DB", "/Volumes/Des/crafted-ops/crafted.db")

# Where the generated page is written (this project directory). We emit two
# identical files: dashboard.html (the named deliverable) and index.html (so the
# bare GitHub Pages URL serves the page — Pages looks for index.html at the root).
_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(_HERE, "dashboard.html")
INDEX_PATH = os.path.join(_HERE, "index.html")

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
    d["course_counts"] = [
        dict(code=r["code"], name=r["name"], total=r["total"],
             delivered=r["delivered"], inflight=r["total"] - r["delivered"])
        for r in conn.execute(
            "SELECT c.code, c.name, COUNT(v.id) total, "
            "  SUM(CASE WHEN v.status='delivered' THEN 1 ELSE 0 END) delivered "
            "FROM courses c LEFT JOIN videos v ON v.course_id = c.id "
            "GROUP BY c.id ORDER BY total DESC")
    ]

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
    cards = []
    for status, n in present:
        cls = STATUS_CLASS.get(status, "neutral")
        cards.append(
            f'<div class="card {e(cls)}">'
            f'<div class="card-n">{n}</div>'
            f'<div class="card-l">{e(status.replace("_", " "))}</div>'
            f'</div>'
        )
    return f'<div class="cards">{"".join(cards)}</div>'


def render_nondelivered_table(d):
    rows = d["non_delivered"]
    if not rows:
        return ('<p class="ok-line">✓ Nothing in flight — all '
                f'{d["delivered_total"]} registered videos are delivered.</p>')
    body = []
    for r in rows:
        sla_txt = (f'{r["sla_h"]:g}h' if r["sla_h"] is not None else "—")
        cls = ' class="over"' if r["over"] else ""
        badge_cls = STATUS_CLASS.get(r["status"], "neutral")
        body.append(
            f"<tr{cls}>"
            f'<td class="mono">{e(r["code"])}</td>'
            f'<td>{e(r["course"])}</td>'
            f'<td>{e(r["vtype"])}</td>'
            f'<td><span class="pill {e(badge_cls)}">'
            f'{e(r["status"].replace("_", " "))}</span></td>'
            f'<td class="num">{e(fmt_age(r["age_h"]))}</td>'
            f'<td class="num muted">{sla_txt}</td>'
            f"</tr>"
        )
    return (
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Code</th><th>Course</th><th>Type</th>'
        '<th>Status</th><th class="num">Age in state</th>'
        '<th class="num">SLA</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
    )


def render_stale(d):
    rows = d["stale"]
    if not rows:
        return ('<p class="ok-line">✓ All on schedule — no video has '
                'exceeded its state SLA.</p>')
    items = []
    for r in rows:
        over_by = (r["age_h"] - r["sla_h"]) if r["sla_h"] is not None else None
        items.append(
            '<li>'
            f'<span class="mono">{e(r["code"])}</span> '
            f'<span class="muted">({e(r["course"])})</span> — '
            f'<span class="pill {e(STATUS_CLASS.get(r["status"], "neutral"))}">'
            f'{e(r["status"].replace("_", " "))}</span> '
            f'in state <b>{e(fmt_age(r["age_h"]))}</b>, '
            f'SLA {r["sla_h"]:g}h '
            f'<span class="over-by">(+{fmt_age(over_by)} over)</span>'
            '</li>'
        )
    return f'<ul class="stale-list">{"".join(items)}</ul>'


def render_course_totals(d):
    rows = d["course_counts"]
    if not rows:
        return '<p class="muted">No courses yet.</p>'
    maxtot = max((c["total"] for c in rows), default=1) or 1
    body = []
    for c in rows:
        dpct = (c["delivered"] / maxtot * 100) if maxtot else 0
        ipct = (c["inflight"] / maxtot * 100) if maxtot else 0
        body.append(
            '<div class="bar-row">'
            f'<div class="bar-label mono">{e(c["code"])}</div>'
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
    legend = ('<div class="legend">'
              '<span><i class="sw green"></i>delivered</span>'
              '<span><i class="sw blue"></i>in flight</span></div>')
    return f'<div class="bars">{"".join(body)}</div>{legend}'


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
        rev_sub = (f'max {d["review_max"]} '
                   f'(<span class="mono">{e(d["review_max_code"])}</span>)')
    return (
        '<div class="metrics">'
        '<div class="metric">'
        f'<div class="metric-v">{cyc_val}</div>'
        '<div class="metric-k">avg cycle time</div>'
        f'<div class="metric-s">{cyc_sub}</div>'
        '</div>'
        '<div class="metric">'
        f'<div class="metric-v">{rev_val}</div>'
        '<div class="metric-k">avg review rounds / video</div>'
        f'<div class="metric-s">{rev_sub}</div>'
        '</div>'
        '<div class="metric">'
        f'<div class="metric-v">{d["delivered_total"]}<span class="muted">'
        f'/{d["total_videos"]}</span></div>'
        '<div class="metric-k">delivered / total</div>'
        f'<div class="metric-s">{d["inflight_total"]} in flight</div>'
        '</div>'
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
            f'<div class="bar-label mono">{e(c["code"])}</div>'
            '<div class="bar-track">'
            f'<div class="bar-seg cyan" style="width:{pct:.2f}%"></div></div>'
            f'<div class="bar-val">{c["total"]}<span class="muted"> '
            f'{pct:.0f}%</span></div>'
            '</div>'
        )
    return f'<div class="bars">{"".join(body)}</div>'


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

CSS = """
:root{
  --bg:#0d1117; --bg2:#161b22; --bg3:#1c2230; --line:#2a3040;
  --fg:#e6edf3; --muted:#8b949e; --accent:#58a6ff;
  --green:#2ea043; --green-d:#1a3a24;
  --red:#e5484d; --red-d:#3a1c1e;
  --yellow:#d9a441; --yellow-d:#3a301a;
  --amber:#e08c3b; --amber-d:#3a2a17;
  --blue:#388bfd; --blue-d:#16283f;
  --teal:#2bb1a8; --teal-d:#12332f;
  --cyan:#39c0d3; --cyan-d:#12313a;
  --neutral:#6e7681; --neutral-d:#22262e;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:15px; line-height:1.5; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1100px; margin:0 auto; padding:20px 16px 64px}
header{border-bottom:1px solid var(--line); padding-bottom:16px; margin-bottom:8px}
h1{font-size:22px; margin:0 0 4px; letter-spacing:.2px}
.sub{color:var(--muted); font-size:13px}
.sub b{color:var(--fg); font-weight:600}
.dot{display:inline-block; width:7px; height:7px; border-radius:50%;
  background:var(--green); margin-right:6px; vertical-align:middle}
section{margin-top:28px}
h2{font-size:13px; text-transform:uppercase; letter-spacing:.08em;
  color:var(--muted); margin:0 0 12px; font-weight:600}
h2 .sec-n{color:var(--accent); margin-right:6px}
.muted{color:var(--muted)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12.5px; word-break:break-all}

/* status cards */
.cards{display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
  gap:10px}
.card{background:var(--bg2); border:1px solid var(--line);
  border-left-width:4px; border-radius:8px; padding:12px 14px}
.card-n{font-size:26px; font-weight:700; line-height:1}
.card-l{color:var(--muted); font-size:12px; margin-top:4px; text-transform:capitalize}
.card.green{border-left-color:var(--green)} .card.green .card-n{color:#4ac26b}
.card.red{border-left-color:var(--red)} .card.red .card-n{color:#ff6a6f}
.card.yellow{border-left-color:var(--yellow)} .card.yellow .card-n{color:#e8bd63}
.card.amber{border-left-color:var(--amber)} .card.amber .card-n{color:#f0a557}
.card.blue{border-left-color:var(--blue)} .card.blue .card-n{color:#5aa5ff}
.card.teal{border-left-color:var(--teal)} .card.teal .card-n{color:#43cabf}
.card.cyan{border-left-color:var(--cyan)} .card.cyan .card-n{color:#54d3e6}
.card.neutral{border-left-color:var(--neutral)} .card.neutral .card-n{color:#a7b0ba}

/* tables */
.table-wrap{overflow-x:auto; border:1px solid var(--line); border-radius:8px}
table{border-collapse:collapse; width:100%; font-size:13.5px}
thead th{text-align:left; background:var(--bg2); color:var(--muted);
  font-weight:600; padding:9px 12px; border-bottom:1px solid var(--line);
  white-space:nowrap; position:sticky; top:0}
tbody td{padding:9px 12px; border-bottom:1px solid var(--line)}
tbody tr:last-child td{border-bottom:none}
tbody tr:nth-child(even){background:rgba(255,255,255,.015)}
td.num,th.num{text-align:right; white-space:nowrap;
  font-variant-numeric:tabular-nums}
tr.over{background:var(--red-d) !important}
tr.over td:first-child{box-shadow:inset 3px 0 0 var(--red)}

/* pills */
.pill{display:inline-block; padding:2px 8px; border-radius:999px;
  font-size:11.5px; font-weight:600; white-space:nowrap;
  border:1px solid transparent; text-transform:capitalize}
.pill.green{background:var(--green-d); color:#4ac26b; border-color:#22482e}
.pill.red{background:var(--red-d); color:#ff6a6f; border-color:#5a2529}
.pill.yellow{background:var(--yellow-d); color:#e8bd63; border-color:#4a3d20}
.pill.amber{background:var(--amber-d); color:#f0a557; border-color:#4a361d}
.pill.blue{background:var(--blue-d); color:#5aa5ff; border-color:#1d3a5c}
.pill.teal{background:var(--teal-d); color:#43cabf; border-color:#1a4640}
.pill.cyan{background:var(--cyan-d); color:#54d3e6; border-color:#1a444f}
.pill.neutral{background:var(--neutral-d); color:#a7b0ba; border-color:#333a44}

/* ok / stale */
.ok-line{background:var(--green-d); border:1px solid #22482e; color:#7ee2a0;
  padding:12px 14px; border-radius:8px; font-size:14px}
.stale-list{list-style:none; margin:0; padding:0}
.stale-list li{background:var(--red-d); border:1px solid #5a2529;
  border-radius:8px; padding:10px 12px; margin-bottom:8px}
.over-by{color:#ff8a8e; font-weight:600}

/* metrics */
.metrics{display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:12px; margin-bottom:20px}
.metric{background:var(--bg2); border:1px solid var(--line);
  border-radius:8px; padding:14px 16px}
.metric-v{font-size:28px; font-weight:700; line-height:1}
.metric-k{font-size:12.5px; color:var(--fg); margin-top:6px; font-weight:600}
.metric-s{font-size:11.5px; color:var(--muted); margin-top:3px}

/* horizontal bars */
.panel{background:var(--bg2); border:1px solid var(--line);
  border-radius:8px; padding:16px}
.grid2{display:grid; grid-template-columns:1fr 1fr; gap:16px}
.bars{display:flex; flex-direction:column; gap:9px}
.bar-row{display:grid; grid-template-columns:130px 1fr auto; align-items:center;
  gap:10px}
.bar-label{font-size:12.5px; color:var(--fg); overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap}
.bar-track{height:16px; background:var(--bg3); border-radius:4px;
  overflow:hidden; display:flex}
.bar-seg{height:100%}
.bar-seg.green{background:var(--green)} .bar-seg.blue{background:var(--blue)}
.bar-seg.teal{background:var(--teal)} .bar-seg.cyan{background:var(--cyan)}
.bar-val{font-size:12.5px; font-variant-numeric:tabular-nums; text-align:right;
  min-width:52px}
.legend{display:flex; gap:16px; margin-top:12px; font-size:12px;
  color:var(--muted)}
.legend .sw{display:inline-block; width:10px; height:10px; border-radius:2px;
  margin-right:5px; vertical-align:middle}
.sw.green{background:var(--green)} .sw.blue{background:var(--blue)}

/* month chart */
.chart{display:flex; align-items:flex-end; gap:6px; height:150px;
  padding-top:18px; overflow-x:auto}
.cbar-col{flex:1 1 0; min-width:26px; display:flex; flex-direction:column;
  align-items:center; height:100%; justify-content:flex-end; position:relative}
.cbar{width:60%; max-width:34px; background:linear-gradient(180deg,#4ac26b,#2ea043);
  border-radius:3px 3px 0 0; min-height:2px}
.cbar[data-zero]{background:var(--bg3)}
.cbar-n{font-size:11px; font-weight:600; color:#7ee2a0; margin-bottom:3px}
.cbar-x{font-size:11px; color:var(--muted); margin-top:6px}
.cbar-y{font-size:10px}

footer{margin-top:40px; padding-top:16px; border-top:1px solid var(--line);
  color:var(--muted); font-size:12px}
footer a{color:var(--accent); text-decoration:none}
footer a:hover{text-decoration:underline}

@media (max-width:640px){
  h1{font-size:19px}
  .bar-row{grid-template-columns:96px 1fr auto}
  .metric-v{font-size:24px}
  .cbar-col{min-width:22px}
}
"""


def render(d, gen_amman):
    ts = gen_amman.strftime("%Y-%m-%d %H:%M")
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>CraftED — Production Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>CraftED — Production Dashboard</h1>
  <div class="sub"><span class="dot"></span>Generated <b>{e(ts)}</b> Amman time
    · auto-refreshes every 30 min ·
    <b>{d["total_videos"]}</b> videos tracked</div>
</header>
""")

    parts.append('<section><h2><span class="sec-n">1</span>Live pulse</h2>')
    parts.append(render_status_cards(d))
    parts.append('<div style="height:16px"></div>')
    parts.append('<h2 style="margin-top:4px">Non-delivered videos</h2>')
    parts.append(render_nondelivered_table(d))
    parts.append('</section>')

    parts.append('<section><h2><span class="sec-n">2</span>Attention needed '
                 '— SLA exceeded</h2>')
    parts.append(render_stale(d))
    parts.append('</section>')

    parts.append('<section><h2><span class="sec-n">3</span>History &amp; '
                 'throughput</h2>')
    parts.append(render_metrics(d))
    parts.append('<div class="grid2">')
    parts.append('<div class="panel"><h2>Per-course totals</h2>'
                 + render_course_totals(d) + '</div>')
    parts.append('<div class="panel"><h2>Deliveries / month '
                 '(last 12)</h2>' + render_deliveries_chart(d) + '</div>')
    parts.append('</div>')
    parts.append('</section>')

    parts.append('<section><h2><span class="sec-n">4</span>Breakdown</h2>')
    parts.append('<div class="grid2">')
    parts.append('<div class="panel"><h2>Videos per type</h2>'
                 + render_type_breakdown(d) + '</div>')
    parts.append('<div class="panel"><h2>Videos per course</h2>'
                 + render_course_breakdown(d) + '</div>')
    parts.append('</div>')
    parts.append('</section>')

    parts.append(f"""
<footer>
  Data: CraftED ops DB · generated by <span class="mono">generate.py</span>
  · read-only mirror · <a href="{e(REPO_URL)}">repository</a>
</footer>
</div>
</body>
</html>
""")
    return "".join(parts)


def main():
    now_utc = datetime.now(timezone.utc)
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
    size_kb = len(page.encode("utf-8")) / 1024
    print(f"wrote dashboard.html + index.html ({size_kb:.1f} KB each)")
    print(f"  videos={data['total_videos']} delivered={data['delivered_total']} "
          f"in-flight={data['inflight_total']} stale={len(data['stale'])}")
    if size_kb > 200:
        print("WARNING: output exceeds 200KB target", file=sys.stderr)


if __name__ == "__main__":
    main()
