#!/usr/bin/env python3
"""
Build script for is_committed.
Reads raw_commits.tsv, anonymizes, fetches recent GitHub events,
generates a self-contained index.html in Snitch dashboard style.
"""

import csv
import json
import subprocess
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = PROJECT_DIR / "data"
RAW_TSV = DATA_DIR / "raw_commits.tsv"
SAFE_TSV = DATA_DIR / "commits.tsv"
OUTPUT_HTML = PROJECT_DIR / "index.html"


def anon_project_name(raw: str) -> str:
    """Keep public repo names, hash private ones."""
    if raw.startswith("github/m0n0x41d/") or raw.startswith("m0n0x41d/"):
        return raw.split("/")[-1]
    if raw.startswith("local/"):
        return raw.split("/", 1)[-1]
    # Private — short hash, no real name
    repo = raw.split("/")[-1]
    h = hashlib.md5(repo.encode()).hexdigest()[:6]
    return f"prv-{h}"


def commit_source(raw_project: str) -> str:
    """Classify commit source for chart series."""
    if raw_project.startswith("github/m0n0x41d/") or raw_project.startswith("m0n0x41d/"):
        return "github"
    return "private"


def anonymize_raw_to_safe():
    """Convert raw_commits.tsv → commits.tsv (safe to commit, no real names/sha)."""
    if not RAW_TSV.exists():
        return False
    with open(RAW_TSV, newline="") as fin, open(SAFE_TSV, "w", newline="") as fout:
        reader = csv.DictReader(fin, delimiter="\t")
        writer = csv.writer(fout, delimiter="\t")
        writer.writerow(["date", "project", "source"])
        for row in reader:
            try:
                dt = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
            except ValueError:
                continue
            writer.writerow([
                dt.strftime("%Y-%m-%dT%H:%M:%S"),
                anon_project_name(row["project"]),
                commit_source(row["project"]),
            ])
    print(f"  Anonymized -> {SAFE_TSV}")
    return True


def load_commits() -> list[dict]:
    """Load from safe TSV (anonymized). Falls back to raw if safe doesn't exist."""
    source_file = SAFE_TSV if SAFE_TSV.exists() else RAW_TSV
    rows = []
    with open(source_file, newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            date_str = row["date"]
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            # Safe TSV already has project/source; raw needs transformation
            if "source" in row:
                project = row["project"]
                source = row["source"]
            else:
                project = anon_project_name(row["project"])
                source = commit_source(row["project"])
            rows.append({
                "date": dt.strftime("%Y-%m-%d"),
                "hour": dt.hour,
                "weekday": dt.weekday(),
                "project": project,
                "source": source,
            })
    return rows


def fetch_github_events() -> list[dict]:
    """Fetch recent public GitHub events for terminal + timeline."""
    all_events = []
    jq_filter = '.[] | select(.type == "PushEvent" or .type == "CreateEvent" or .type == "PullRequestEvent" or .type == "IssuesEvent" or .type == "IssueCommentEvent" or .type == "WatchEvent" or .type == "ForkEvent" or .type == "DeleteEvent") | {type, repo: .repo.name, created_at, payload}'
    # Fetch multiple pages
    for page in range(1, 6):
        try:
            result = subprocess.run(
                ["gh", "api", f"/users/m0n0x41d/events?per_page=100&page={page}", "-q", jq_filter],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0 or not result.stdout.strip():
                break
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    all_events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            break
    return all_events


def format_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (terminal_entries, timeline_entries)."""
    terminal = []
    timeline = []
    for ev in events:
        ts = ev.get("created_at", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            continue

        repo = ev.get("repo", "").replace("m0n0x41d/", "")
        etype = ev.get("type", "")
        payload = ev.get("payload", {})

        if etype == "PushEvent":
            commits = payload.get("commits", [])
            for c in commits[:3]:
                msg = c.get("message", "").split("\n")[0][:80]
                sha = c.get("sha", "")[:7]
                terminal.append({"time": time_str, "status": "SUCCESS",
                                 "msg": f"Commit {sha} pushed to {repo}: {msg}"})
                if len(timeline) < 8:
                    timeline.append({"time": time_str.split(" ")[1], "label": "MAIN BRANCH PUSH",
                                     "title": msg, "sha": sha, "status": "primary"})
        elif etype == "CreateEvent":
            ref_type = payload.get("ref_type", "")
            ref = payload.get("ref", "")
            terminal.append({"time": time_str, "status": "SYSTEM",
                             "msg": f"Created {ref_type} '{ref}' in {repo}"})
        elif etype == "PullRequestEvent":
            action = payload.get("action", "")
            pr = payload.get("pull_request", {})
            title = pr.get("title", "")[:60]
            status = "WARNING" if action == "closed" else "SUCCESS"
            terminal.append({"time": time_str, "status": status,
                             "msg": f"PR {action}: {title} ({repo})"})
            if len(timeline) < 8:
                timeline.append({"time": time_str.split(" ")[1], "label": f"PR {action.upper()}",
                                 "title": title, "sha": "", "status": "secondary" if action == "closed" else "primary"})
        elif etype == "IssuesEvent":
            action = payload.get("action", "")
            issue = payload.get("issue", {})
            title = issue.get("title", "")[:60]
            terminal.append({"time": time_str, "status": "SYSTEM",
                             "msg": f"Issue {action}: {title} ({repo})"})
        elif etype == "IssueCommentEvent":
            issue = payload.get("issue", {})
            title = issue.get("title", "")[:50]
            terminal.append({"time": time_str, "status": "SYSTEM",
                             "msg": f"Commented on: {title} ({repo})"})
        elif etype == "DeleteEvent":
            ref_type = payload.get("ref_type", "")
            ref = payload.get("ref", "")
            terminal.append({"time": time_str, "status": "WARNING",
                             "msg": f"Deleted {ref_type} '{ref}' in {repo}"})
        elif etype == "ForkEvent":
            forkee = payload.get("forkee", {})
            terminal.append({"time": time_str, "status": "SYSTEM",
                             "msg": f"Forked {repo} -> {forkee.get('full_name', '')}"})

    return terminal[:30], timeline[:6]


def compute_stats(commits: list[dict]) -> dict:
    monthly_gh = defaultdict(int)
    monthly_priv = defaultdict(int)
    daily_gh = defaultdict(int)
    daily_priv = defaultdict(int)
    yearly_gh = defaultdict(int)
    yearly_priv = defaultdict(int)
    by_project = defaultdict(int)
    by_weekday = defaultdict(int)
    by_hour = defaultdict(int)
    dates_set = set()

    for c in commits:
        d = datetime.strptime(c["date"], "%Y-%m-%d")
        mk = d.strftime("%Y-%m")
        yk = d.strftime("%Y")
        is_gh = c["source"] == "github"
        if is_gh:
            monthly_gh[mk] += 1
            daily_gh[c["date"]] += 1
            yearly_gh[yk] += 1
        else:
            monthly_priv[mk] += 1
            daily_priv[c["date"]] += 1
            yearly_priv[yk] += 1
        by_project[c["project"]] += 1
        by_weekday[c["weekday"]] += 1
        by_hour[c["hour"]] += 1
        dates_set.add(c["date"])

    # Streak
    sorted_dates = sorted(dates_set)
    max_streak = current_streak = 0
    if sorted_dates:
        prev = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
        current_streak = max_streak = 1
        for ds in sorted_dates[1:]:
            curr = datetime.strptime(ds, "%Y-%m-%d")
            if (curr - prev).days == 1:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            elif (curr - prev).days > 1:
                current_streak = 1
            prev = curr

    today = datetime.now().date()
    # Start from today or yesterday (data may not include today yet)
    curr_streak = 0
    check = today if today.strftime("%Y-%m-%d") in dates_set else today - timedelta(days=1)
    while check.strftime("%Y-%m-%d") in dates_set:
        curr_streak += 1
        check -= timedelta(days=1)

    # Monthly
    all_months = sorted(set(list(monthly_gh.keys()) + list(monthly_priv.keys())))
    month_labels = []
    for m in all_months:
        try:
            month_labels.append(datetime.strptime(m, "%Y-%m").strftime("%b '%y"))
        except ValueError:
            month_labels.append(m)
    month_gh = [monthly_gh.get(m, 0) for m in all_months]
    month_priv = [monthly_priv.get(m, 0) for m in all_months]

    # Daily (all dates from first to last)
    min_date = datetime.strptime(sorted_dates[0], "%Y-%m-%d").date()
    all_days = []
    d = min_date
    while d <= today:
        all_days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    day_gh = [daily_gh.get(d, 0) for d in all_days]
    day_priv = [daily_priv.get(d, 0) for d in all_days]

    # Yearly
    all_years = sorted(set(list(yearly_gh.keys()) + list(yearly_priv.keys())))
    year_gh = [yearly_gh.get(y, 0) for y in all_years]
    year_priv = [yearly_priv.get(y, 0) for y in all_years]

    # Heatmap (last 52 weeks)
    date_counts = defaultdict(int)
    for c in commits:
        date_counts[c["date"]] += 1
    heatmap = []
    start = today - timedelta(days=364)
    hd = start
    while hd <= today:
        ds = hd.strftime("%Y-%m-%d")
        heatmap.append({"date": ds, "count": date_counts.get(ds, 0)})
        hd += timedelta(days=1)

    # Velocity
    recent_30 = sum(1 for c in commits
                    if (today - datetime.strptime(c["date"], "%Y-%m-%d").date()).days <= 30)
    velocity = round(recent_30 / 30, 1)

    top_projects = sorted(by_project.items(), key=lambda x: -x[1])[:15]
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday_data = [by_weekday.get(i, 0) for i in range(7)]
    hourly_data = [by_hour.get(h, 0) for h in range(24)]
    peak_hour = max(range(24), key=lambda h: by_hour.get(h, 0))
    peak_day = max(range(7), key=lambda dd: by_weekday.get(dd, 0))

    return {
        "total": len(commits),
        "repos": len(set(c["project"] for c in commits)),
        "active_days": len(dates_set),
        "max_streak": max_streak,
        "current_streak": curr_streak,
        "years": len(set(d[:4] for d in dates_set)),
        "velocity": velocity,
        # monthly
        "month_labels": month_labels,
        "month_gh": month_gh,
        "month_priv": month_priv,
        # daily
        "day_labels": all_days,
        "day_gh": day_gh,
        "day_priv": day_priv,
        # yearly
        "year_labels": all_years,
        "year_gh": year_gh,
        "year_priv": year_priv,
        # misc
        "top_projects": top_projects,
        "weekday_names": weekday_names,
        "weekday_data": weekday_data,
        "hourly_data": hourly_data,
        "heatmap": heatmap,
        "peak_hour": peak_hour,
        "peak_day": weekday_names[peak_day],
    }


def generate_html(stats: dict, terminal: list[dict], timeline: list[dict]) -> str:
    import re

    # ── Terminal log ──
    terminal_log_html = ""
    for entry in terminal[:25]:
        color_map = {"SUCCESS": "#007AFF", "WARNING": "#FF5F1F", "SYSTEM": "#94A3B8"}
        c = color_map.get(entry["status"], "#94A3B8")
        msg = entry["msg"]
        msg = re.sub(r'\w+Octa/[\w\-\.]+', '***', msg)
        terminal_log_html += f'''<div class="log-line"><span class="log-ts">{entry["time"]}</span> <span style="color:{c};">[{entry["status"]}]</span> <span class="log-msg">{msg}</span></div>\n'''

    # ── Weekday bars ──
    wd_max = max(stats["weekday_data"]) if stats["weekday_data"] else 1
    weekday_html = ""
    for name, val in zip(stats["weekday_names"], stats["weekday_data"]):
        pct = (val / wd_max) * 100
        weekday_html += f'''<div class="wd-row">
            <span class="wd-name">{name}</span>
            <div class="wd-track"><div class="wd-fill" style="width:{pct:.0f}%"></div></div>
            <span class="wd-val">{val:,}</span>
        </div>\n'''

    # ── Top projects ──
    proj_max = stats["top_projects"][0][1] if stats["top_projects"] else 1
    top_proj_html = ""
    for name, count in stats["top_projects"]:
        pct = (count / proj_max) * 100
        is_private = name.startswith("prv-")
        bar_cls = "proj-fill-priv" if is_private else "proj-fill"
        top_proj_html += f'''<div class="proj-row">
            <div class="proj-info"><span class="proj-name{' proj-private' if is_private else ''}">{name}</span><span class="proj-count">{count:,}</span></div>
            <div class="proj-track"><div class="{bar_cls}" style="width:{pct:.1f}%"></div></div>
        </div>\n'''

    # ── Timeline ──
    timeline_html = ""
    for i, entry in enumerate(timeline):
        color = "#007AFF" if entry["status"] == "primary" else "#FF5F1F"
        opacity = "opacity:0.5;" if i >= 4 else ""
        sha_part = f' &middot; {entry["sha"]}' if entry["sha"] else ""
        timeline_html += f'''<div class="tl-node" style="{opacity}">
            <div class="tl-dot" style="border-color:{color};"></div>
            <div class="tl-content">
                <span class="tl-label" style="color:{color};">{entry["label"]}</span>
                <p class="tl-title">{entry["title"]}</p>
                <span class="tl-meta">{entry["time"]}{sha_part}</span>
            </div>
        </div>\n'''

    heatmap_json = json.dumps(stats["heatmap"])
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>is_committed // Ivan Zakutni</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root {{
    --bg: #0B0D11;
    --surface: #13161C;
    --surface-hi: #1A1E26;
    --border: #252A34;
    --primary: #007AFF;
    --secondary: #FF5F1F;
    --tertiary: #94A3B8;
    --text: #E8EAF0;
    --text2: #A0A6B8;
    --text3: #5C6378;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Inter', -apple-system, sans-serif;
}}
*, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--text); font-family:var(--sans); font-size:14px; line-height:1.5; }}
a {{ color:var(--primary); text-decoration:none; }}

/* ── Layout ── */
.container {{ max-width:1400px; margin:0 auto; padding:32px 24px; }}
.grid {{ display:grid; gap:20px; }}
.g2 {{ grid-template-columns:1fr 1fr; }}
.g3 {{ grid-template-columns:1fr 1fr 1fr; }}
.g-main {{ grid-template-columns:2fr 1fr; }}
.g-bottom {{ grid-template-columns:5fr 7fr; }}
@media(max-width:1024px) {{ .g2,.g3,.g-main,.g-bottom {{ grid-template-columns:1fr; }} }}

/* ── Header ── */
.header {{ display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:32px; flex-wrap:wrap; gap:20px; }}
.header-left h1 {{ font-size:28px; font-weight:800; letter-spacing:-0.02em; color:var(--text); }}
.header-left .subtitle {{ font-family:var(--mono); font-size:12px; color:var(--text3); margin-top:4px; }}
.header-left .subtitle span {{ color:var(--primary); }}
.stats-row {{ display:flex; gap:32px; }}
.stat {{ text-align:right; }}
.stat-label {{ font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:0.15em; color:var(--text3); }}
.stat-val {{ font-family:var(--mono); font-size:22px; font-weight:700; }}
.stat-val.blue {{ color:var(--primary); }}
.stat-val.orange {{ color:var(--secondary); }}

/* ── Panel ── */
.panel {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; }}
.panel-head {{ padding:16px 20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; }}
.panel-title {{ font-family:var(--mono); font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:0.15em; color:var(--text2); }}
.panel-body {{ padding:20px; }}

/* ── View Tabs ── */
.tabs {{ display:flex; gap:4px; }}
.tab {{ font-family:var(--mono); font-size:10px; padding:5px 14px; border-radius:4px; cursor:pointer;
        background:transparent; border:1px solid var(--border); color:var(--text3); transition:all 0.15s; }}
.tab:hover {{ color:var(--text2); border-color:var(--text3); }}
.tab.active {{ background:var(--primary); color:#fff; border-color:var(--primary); }}

/* ── Chart Legend ── */
.legend {{ display:flex; gap:16px; font-family:var(--mono); font-size:10px; }}
.legend-item {{ display:flex; align-items:center; gap:6px; color:var(--text2); }}
.legend-dot {{ width:8px; height:8px; border-radius:2px; }}

/* ── Stat cards ── */
.stat-cards {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:16px; margin-bottom:20px; }}
@media(max-width:768px) {{ .stat-cards {{ grid-template-columns:repeat(2, 1fr); }} }}
.scard {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:20px; border-left:3px solid var(--primary); }}
.scard.orange {{ border-left-color:var(--secondary); }}
.scard-label {{ font-family:var(--mono); font-size:10px; text-transform:uppercase; letter-spacing:0.12em; color:var(--text3); margin-bottom:6px; }}
.scard-val {{ font-family:var(--mono); font-size:26px; font-weight:700; color:var(--text); }}
.scard-sub {{ font-family:var(--mono); font-size:10px; color:var(--text3); margin-top:4px; }}

/* ── Weekday ── */
.wd-row {{ display:flex; align-items:center; gap:12px; padding:6px 0; }}
.wd-name {{ font-family:var(--mono); font-size:11px; color:var(--text3); width:32px; }}
.wd-track {{ flex:1; height:8px; background:var(--surface-hi); border-radius:4px; overflow:hidden; }}
.wd-fill {{ height:100%; background:var(--primary); border-radius:4px; transition:width 0.6s; }}
.wd-val {{ font-family:var(--mono); font-size:11px; color:var(--text); width:48px; text-align:right; }}

/* ── Projects ── */
.proj-row {{ margin-bottom:10px; }}
.proj-info {{ display:flex; justify-content:space-between; margin-bottom:4px; }}
.proj-name {{ font-family:var(--mono); font-size:11px; color:var(--text2); }}
.proj-hash {{ font-family:var(--mono); font-size:9px; color:var(--text3); }}
.proj-count {{ font-family:var(--mono); font-size:11px; color:var(--text); font-weight:600; }}
.proj-track {{ height:4px; background:var(--surface-hi); border-radius:2px; overflow:hidden; }}
.proj-fill {{ height:100%; background:var(--primary); border-radius:2px; }}
.proj-fill-priv {{ height:100%; background:var(--secondary); border-radius:2px; }}

/* ── Timeline ── */
.tl-node {{ position:relative; padding-left:24px; padding-bottom:20px; }}
.tl-node:not(:last-child)::after {{ content:''; position:absolute; left:7px; top:20px; bottom:0; width:1px; background:var(--border); }}
.tl-dot {{ position:absolute; left:0; top:4px; width:16px; height:16px; border-radius:50%; background:var(--bg); border:2px solid var(--primary); }}
.tl-label {{ font-family:var(--mono); font-size:10px; text-transform:uppercase; font-weight:700; letter-spacing:0.1em; }}
.tl-title {{ font-size:13px; font-weight:600; color:var(--text); margin:4px 0 2px; }}
.tl-meta {{ font-family:var(--mono); font-size:10px; color:var(--text3); }}

/* ── Terminal ── */
.terminal {{ background:#080A0E; border:1px solid var(--border); border-radius:8px; overflow:hidden; display:flex; flex-direction:column; }}
.terminal-bar {{ display:flex; align-items:center; justify-content:space-between; padding:8px 16px; background:var(--surface-hi); border-bottom:1px solid var(--border); }}
.terminal-dots {{ display:flex; gap:6px; }}
.terminal-dots span {{ width:10px; height:10px; border-radius:50%; }}
.terminal-dots .r {{ background:#FF5F57; opacity:0.5; }}
.terminal-dots .y {{ background:#FFBD2E; opacity:0.5; }}
.terminal-dots .g {{ background:#28CA41; opacity:0.5; }}
.terminal-title {{ font-family:var(--mono); font-size:10px; color:var(--text3); }}
.terminal-body {{ padding:16px; font-family:var(--mono); font-size:11px; line-height:1.7; flex:1; }}
.log-line {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.log-ts {{ color:var(--text3); }}
.log-msg {{ color:var(--text2); }}

/* ── Heatmap ── */
/* heatmap layout */
.hm-outer {{ display:flex; align-items:stretch; gap:0; }}
.hm-center {{ flex:0 0 auto; display:flex; justify-content:center; margin:0 auto; }}
.hm-side {{ flex:1; min-width:20px;
    background:repeating-linear-gradient(0deg, transparent, transparent 11px, rgba(0,122,255,0.04) 11px, rgba(0,122,255,0.04) 12px),
               repeating-linear-gradient(90deg, transparent, transparent 11px, rgba(0,122,255,0.04) 11px, rgba(0,122,255,0.04) 12px);
    position:relative; overflow:hidden;
}}
.hm-side::after {{ content:''; position:absolute; inset:0;
    background:linear-gradient(90deg, var(--surface) 0%, transparent 30%, transparent 70%, var(--surface) 100%);
}}
.hm-side-l::after {{ background:linear-gradient(90deg, var(--surface) 0%, transparent 100%); }}
.hm-side-r::after {{ background:linear-gradient(90deg, transparent 0%, var(--surface) 100%); }}
.hm-wrap {{ display:flex; gap:3px; }}
.hm-col {{ display:flex; flex-direction:column; gap:3px; }}
.hm-cell {{ width:12px; height:12px; border-radius:2px; background:var(--surface-hi); }}
.hm-cell.l1 {{ background:rgba(0,122,255,0.15); }}
.hm-cell.l2 {{ background:rgba(0,122,255,0.35); }}
.hm-cell.l3 {{ background:rgba(0,122,255,0.55); }}
.hm-cell.l4 {{ background:rgba(0,122,255,0.85); }}

/* private repos */
.proj-private {{ color:var(--text3); }}

.footer {{ text-align:center; padding:40px 0 16px; font-family:var(--mono); font-size:10px; color:var(--text3); }}
</style>
</head>
<body>
<div class="container">

    <!-- Header -->
    <div class="header">
        <div class="header-left">
            <h1>is_committed</h1>
            <div class="subtitle"><span>Ivan Zakutni</span> &middot; Staff Systems Engineer &middot; last build {now_str}</div>
        </div>
        <div class="stats-row">
            <div class="stat">
                <div class="stat-label">Velocity (30d)</div>
                <div class="stat-val blue">{stats["velocity"]} /day</div>
            </div>
            <div class="stat">
                <div class="stat-label">Streak</div>
                <div class="stat-val orange">{stats["current_streak"]}d</div>
            </div>
        </div>
    </div>

    <!-- Stat Cards -->
    <div class="stat-cards">
        <div class="scard">
            <div class="scard-label">Total Commits</div>
            <div class="scard-val">{stats["total"]:,}</div>
            <div class="scard-sub">{stats["years"]} years tracked</div>
        </div>
        <div class="scard">
            <div class="scard-label">Repositories</div>
            <div class="scard-val">{stats["repos"]}</div>
            <div class="scard-sub">public + private</div>
        </div>
        <div class="scard orange">
            <div class="scard-label">Active Days</div>
            <div class="scard-val">{stats["active_days"]:,}</div>
            <div class="scard-sub">max streak: {stats["max_streak"]}d</div>
        </div>
        <div class="scard">
            <div class="scard-label">Peak</div>
            <div class="scard-val">{stats["peak_day"]} {stats["peak_hour"]:02d}h</div>
            <div class="scard-sub">most active time</div>
        </div>
    </div>

    <!-- Main Velocity Chart with Tabs -->
    <div class="panel" style="margin-bottom:20px;">
        <div class="panel-head">
            <div class="panel-title">Commit Velocity</div>
            <div style="display:flex;align-items:center;gap:16px;">
                <div class="legend">
                    <div class="legend-item"><div class="legend-dot" style="background:var(--primary);"></div>GitHub</div>
                    <div class="legend-item"><div class="legend-dot" style="background:var(--secondary);"></div>Private</div>
                    <div class="legend-item"><div class="legend-dot" style="background:var(--tertiary);"></div>Combined</div>
                </div>
                <div class="tabs">
                    <div class="tab active" onclick="switchView('monthly')">Monthly</div>
                    <div class="tab" onclick="switchView('daily')">Daily</div>
                    <div class="tab" onclick="switchView('yearly')">Yearly</div>
                </div>
            </div>
        </div>
        <div class="panel-body">
            <div id="chart-monthly" style="height:320px;"><canvas id="monthlyChart"></canvas></div>
            <div id="chart-daily" style="height:320px;display:none;"><canvas id="dailyChart"></canvas></div>
            <div id="chart-yearly" style="height:320px;display:none;"><canvas id="yearlyChart"></canvas></div>
        </div>
    </div>

    <!-- Heatmap -->
    <div class="panel" style="margin-bottom:20px;">
        <div class="panel-head">
            <div class="panel-title">Activity Heatmap (52 weeks)</div>
        </div>
        <div class="panel-body" style="overflow:hidden;">
            <div class="hm-outer">
                <div class="hm-side hm-side-l"></div>
                <div class="hm-center">
                    <div class="hm-wrap" id="heatmap"></div>
                </div>
                <div class="hm-side hm-side-r"></div>
            </div>
        </div>
    </div>

    <!-- Middle row: Weekday + Hourly -->
    <div class="grid g2" style="margin-bottom:20px;">
        <div class="panel">
            <div class="panel-head"><div class="panel-title">Weekly Distribution</div></div>
            <div class="panel-body">
                {weekday_html}
                <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border);">
                    <div style="font-family:var(--mono);font-size:10px;color:var(--text3);text-transform:uppercase;">Peak</div>
                    <div style="font-family:var(--mono);font-size:18px;font-weight:700;color:var(--secondary);">{stats["peak_day"]} {stats["peak_hour"]:02d}:00</div>
                </div>
            </div>
        </div>
        <div class="panel">
            <div class="panel-head"><div class="panel-title">Hourly Commit Pattern</div></div>
            <div class="panel-body"><div style="height:200px;"><canvas id="hourlyChart"></canvas></div></div>
        </div>
    </div>

    <!-- Bottom row: Top repos + Terminal -->
    <div class="grid g-bottom">
        <div class="panel">
            <div class="panel-head"><div class="panel-title">Top Repositories</div></div>
            <div class="panel-body">{top_proj_html}</div>
        </div>
        <div class="terminal">
            <div class="terminal-bar">
                <div class="terminal-dots"><span class="r"></span><span class="y"></span><span class="g"></span></div>
                <span class="terminal-title">recent_activity.log</span>
                <div style="width:40px;"></div>
            </div>
            <div class="terminal-body">
                {terminal_log_html if terminal_log_html else '<div class="log-line"><span class="log-ts">--:--:--</span> <span style="color:var(--tertiary);">[SYSTEM]</span> <span class="log-msg">Awaiting build refresh...</span></div>'}
            </div>
        </div>
    </div>

    <div class="footer">
        built with obsessive commit habits // <a href="https://github.com/m0n0x41d">@m0n0x41d</a>
    </div>
</div>

<script>
const COLORS = {{ primary:'#007AFF', secondary:'#FF5F1F', tertiary:'#94A3B8', grid:'rgba(37,42,52,0.5)' }};
Chart.defaults.color = '#5C6378';
Chart.defaults.borderColor = '#252A34';
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 11;

// ── Data ──
const DATA = {{
    monthLabels: {json.dumps(stats["month_labels"])},
    monthGH: {json.dumps(stats["month_gh"])},
    monthPriv: {json.dumps(stats["month_priv"])},
    dayLabels: {json.dumps(stats["day_labels"])},
    dayGH: {json.dumps(stats["day_gh"])},
    dayPriv: {json.dumps(stats["day_priv"])},
    yearLabels: {json.dumps(stats["year_labels"])},
    yearGH: {json.dumps(stats["year_gh"])},
    yearPriv: {json.dumps(stats["year_priv"])},
}};
const dayCombined = DATA.dayGH.map((v,i) => v + DATA.dayPriv[i]);
function rolling(arr, w) {{
    const out = [];
    for (let i = 0; i < arr.length; i++) {{
        const start = Math.max(0, i-w+1);
        let sum = 0;
        for (let j = start; j <= i; j++) sum += arr[j];
        out.push(Math.round(sum / (i - start + 1) * 10) / 10);
    }}
    return out;
}}
const TT = {{ backgroundColor:'#1A1E26', borderColor:'#252A34', borderWidth:1 }};

// ── Chart builders (recreated each switch for animation replay) ──
let activeChart = null;
const builders = {{
    monthly(canvas) {{
        return new Chart(canvas, {{
            type:'bar',
            data:{{ labels:DATA.monthLabels, datasets:[
                {{ label:'GitHub', data:DATA.monthGH, backgroundColor:COLORS.primary, borderRadius:2 }},
                {{ label:'Private', data:DATA.monthPriv, backgroundColor:COLORS.secondary, borderRadius:2 }},
            ]}},
            options:{{
                responsive:true, maintainAspectRatio:false,
                animation:{{ duration:800, easing:'easeOutQuart' }},
                plugins:{{ legend:{{ display:false }}, tooltip:TT }},
                scales:{{
                    x:{{ stacked:true, grid:{{ display:false }}, ticks:{{ maxRotation:45 }} }},
                    y:{{ stacked:true, grid:{{ color:COLORS.grid }} }},
                }},
            }},
        }});
    }},
    daily(canvas) {{
        return new Chart(canvas, {{
            type:'line',
            data:{{ labels:DATA.dayLabels, datasets:[
                {{ label:'Combined', data:rolling(dayCombined,7), borderColor:COLORS.tertiary, backgroundColor:'rgba(148,163,184,0.05)', borderWidth:1.5, pointRadius:0, fill:true, tension:0.3 }},
                {{ label:'GitHub', data:rolling(DATA.dayGH,7), borderColor:COLORS.primary, borderWidth:1.5, pointRadius:0, tension:0.3 }},
                {{ label:'Private', data:rolling(DATA.dayPriv,7), borderColor:COLORS.secondary, borderWidth:1.5, pointRadius:0, tension:0.3 }},
            ]}},
            options:{{
                responsive:true, maintainAspectRatio:false,
                animation:{{ duration:1000, easing:'easeOutQuart' }},
                plugins:{{ legend:{{ display:false }}, tooltip:{{ ...TT, mode:'index', intersect:false }} }},
                scales:{{
                    x:{{ grid:{{ display:false }}, ticks:{{ maxTicksLimit:12, maxRotation:0 }} }},
                    y:{{ grid:{{ color:COLORS.grid }} }},
                }},
                interaction:{{ mode:'nearest', axis:'x', intersect:false }},
            }},
        }});
    }},
    yearly(canvas) {{
        return new Chart(canvas, {{
            type:'bar',
            data:{{ labels:DATA.yearLabels, datasets:[
                {{ label:'GitHub', data:DATA.yearGH, backgroundColor:COLORS.primary, borderRadius:6 }},
                {{ label:'Private', data:DATA.yearPriv, backgroundColor:COLORS.secondary, borderRadius:6 }},
            ]}},
            options:{{
                responsive:true, maintainAspectRatio:false,
                animation:{{ duration:900, easing:'easeOutBack',
                    y:{{ from: (ctx) => ctx.chart.scales.y.getPixelForValue(0) }}
                }},
                plugins:{{ legend:{{ display:false }}, tooltip:TT }},
                scales:{{
                    x:{{ stacked:true, grid:{{ display:false }} }},
                    y:{{ stacked:true, grid:{{ color:COLORS.grid }} }},
                }},
            }},
        }});
    }},
}};

function switchView(view) {{
    ['monthly','daily','yearly'].forEach(v => {{
        const el = document.getElementById('chart-'+v);
        el.style.display = v===view ? 'block' : 'none';
    }});
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    event.target.classList.add('active');
    // Destroy old chart on the target canvas, recreate for animation
    const container = document.getElementById('chart-'+view);
    const oldCanvas = container.querySelector('canvas');
    if (activeChart) {{ activeChart.destroy(); activeChart = null; }}
    // Replace canvas to get a clean one
    const newCanvas = document.createElement('canvas');
    newCanvas.id = view+'Chart';
    oldCanvas.replaceWith(newCanvas);
    activeChart = builders[view](newCanvas);
}}

// Initial render
activeChart = builders.monthly(document.getElementById('monthlyChart'));

// ── Hourly ──
new Chart(document.getElementById('hourlyChart'), {{
    type:'bar',
    data:{{
        labels:{json.dumps([f"{h:02d}:00" for h in range(24)])},
        datasets:[{{
            data:{json.dumps(stats["hourly_data"])},
            backgroundColor:function(ctx){{
                const v=ctx.raw||0, m=Math.max(...{json.dumps(stats["hourly_data"])}), r=v/m;
                return r>0.7?COLORS.primary:r>0.4?'rgba(0,122,255,0.5)':'rgba(0,122,255,0.15)';
            }},
            borderRadius:2,
        }}],
    }},
    options:{{
        responsive:true, maintainAspectRatio:false,
        plugins:{{ legend:{{ display:false }} }},
        scales:{{ x:{{ grid:{{ display:false }} }}, y:{{ grid:{{ color:COLORS.grid }} }} }},
    }},
}});

// ── Heatmap ──
(function(){{
    const data={heatmap_json};
    const c=document.getElementById('heatmap');
    const weeks={{}};
    data.forEach((d,i)=>{{ const w=Math.floor(i/7); if(!weeks[w])weeks[w]=[]; weeks[w].push(d); }});
    Object.values(weeks).forEach(week=>{{
        const col=document.createElement('div'); col.className='hm-col';
        week.forEach(d=>{{
            const cell=document.createElement('div'); cell.className='hm-cell';
            cell.title=d.date+': '+d.count+' commits';
            if(d.count>=10)cell.classList.add('l4');
            else if(d.count>=5)cell.classList.add('l3');
            else if(d.count>=2)cell.classList.add('l2');
            else if(d.count>=1)cell.classList.add('l1');
            col.appendChild(cell);
        }});
        c.appendChild(col);
    }});
}})();
</script>
</body>
</html>'''


def main():
    # If raw data exists, anonymize it first
    if RAW_TSV.exists():
        print("Anonymizing raw data...")
        anonymize_raw_to_safe()

    print("Loading commits...")
    commits = load_commits()
    print(f"  {len(commits)} commits loaded")

    print("Computing stats...")
    stats = compute_stats(commits)

    print("Fetching recent GitHub events...")
    events = fetch_github_events()
    terminal, timeline = format_events(events)
    print(f"  {len(terminal)} terminal / {len(timeline)} timeline")

    print("Generating HTML...")
    html = generate_html(stats, terminal, timeline)
    OUTPUT_HTML.write_text(html)
    print(f"  -> {OUTPUT_HTML}")
    print(f"\n  {stats['total']:,} commits | {stats['repos']} repos | {stats['velocity']} /day | streak {stats['current_streak']}d")


if __name__ == "__main__":
    main()
