#!/usr/bin/env python3
"""
Commitscape SVG renderer: turns an analysis JSON (from the commitscape CLI or
commitscape/analyse.py) into poster-style SVGs. Runs fully offline, stdlib only.

Designs:
  week-circles  Working hours: every commit on a 7-day clock, radius = time within the range
  user-bubbles  Committers: beeswarm of each top committer's commits over time
  trunk         Time and influence: nested contours of commit share per contributor per era
  cross         Ribbons from directories to the authors who changed them
  core          Directory ring with chords between files that change together

Commit colour: additions/deletions ratio > 1.25 "more added", < 0.8 "more deleted", else "about the same".
Commit size:   sqrt(lines added + deleted).

Usage:
  python3 -m commitscape.render analysis.json --out viz/ [--designs week-circles,core] [--title "My project"]
"""
import argparse
import html
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .filegroups import PRESETS, Classifier, breakdown, detect_preset, load_profile
from .identities import load_aliases, resolve_users

POS, NEU, NEG = "#8DD3C7", "#EADEC4", "#BC6F6F"
SANS = "Montserrat,Gotham,Proxima Nova,Helvetica,sans-serif"
MONO = "Ubuntu Mono,Menlo,monospace"
A_W, A_H = 1063.8297872340427, 1500  # 1:1.41 poster



# --------------------------------------------------------------------------- data


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_date(d):
    return f"{d.day} {d.strftime('%b %Y')}"


class Data:
    def __init__(self, a, name=None, include_bots=False, aliases=None, exclude=None, file_groups=None):
        """`aliases` maps original author names to final names (merging identities);
        `exclude` lists original names to leave out (see commitscape/identities.py).
        `file_groups` is a file-groups profile (see commitscape/filegroups.py); files in its excluded
        groups are left out of every design. Without one, a preset is picked from the file list."""
        self.raw = a
        self.files = a["files"]
        self.users, user_map, self.bots = resolve_users(a["users"], aliases, exclude, include_bots)
        meta = a.get("meta") or {}
        self.name = name or meta.get("title") or meta.get("name") or "repository"
        self.commits = []
        profile = file_groups or PRESETS[detect_preset(self.files)]
        self.classifier = Classifier(profile)
        self.raw_commits_kept = []  # analysis-shaped commits after exclusions, for group_shares
        for c in a["commits"]:
            if c["user"] not in user_map:  # excluded or a bot
                continue
            c = dict(c, files=[f for f in c["files"] if not self.classifier.excluded(self.files[f["File"]])])
            self.raw_commits_kept.append(c)
            adds = sum(f["Additions"] for f in c["files"])
            dels = sum(f["Deletions"] for f in c["files"])
            self.commits.append({
                "t": datetime.fromisoformat(c["time"]), "user": user_map[c["user"]], "files": c["files"],
                "adds": adds, "dels": dels, "lines": adds + dels, "kind": kind(adds, dels),
            })
        self.commits.sort(key=lambda c: c["t"])
        # The chosen date range (if any) defines the time axis; otherwise the commits do.
        tz = self.commits[0]["t"].tzinfo if self.commits else timezone.utc
        if meta.get("from"):
            self.first = datetime.fromisoformat(meta["from"]).replace(tzinfo=tz)
        else:
            self.first = self.commits[0]["t"] if self.commits else datetime.now(timezone.utc)
        if meta.get("to"):
            self.last = datetime.fromisoformat(meta["to"]).replace(hour=23, minute=59, second=59, tzinfo=tz)
        else:
            self.last = self.commits[-1]["t"] if self.commits else datetime.now(timezone.utc)
        self.span = max((self.last - self.first).total_seconds(), 1)
        self.user_commits = Counter(c["user"] for c in self.commits)
        self.files_touched = len({f["File"] for c in self.commits for f in c["files"]})
        self.lines_added = sum(c["adds"] for c in self.commits)
        self.lines_removed = sum(c["dels"] for c in self.commits)
        self.active_days = len({c["t"].date() for c in self.commits})
        self.languages = group_shares(self)

    def tnorm(self, t):
        return (t - self.first).total_seconds() / self.span

    def top_users(self, n):
        return [u for u, _ in self.user_commits.most_common(n)]

    @property
    def date_range(self):
        return f"{fmt_date(self.first)} - {fmt_date(self.last)}"


def group_shares(d, top=4):
    """Top file groups by lines changed within the range, as [(name, share)], the rest as "Other".
    Excluded groups are already gone from the commits."""
    groups, _, _ = breakdown(d.raw_commits_kept, d.files, d.classifier)
    total = sum(g["lines"] for g in groups.values())
    if not total:
        return []
    ranked = sorted(groups.items(), key=lambda kv: -kv[1]["lines"])
    shares = [(name, g["lines"] / total) for name, g in ranked[:top] if g["lines"]]
    rest = 1 - sum(sh for _, sh in shares)
    if rest > 0.005:
        shares.append(("Other", rest))
    return shares


def fmt_big(n):
    """1,234 / 98,765 / 758k / 1.2M: exact below 100,000, abbreviated above."""
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    return f"{round(n / 1000)}k" if n >= 100_000 else f"{n:,}"


def kind(adds, dels):
    if dels == 0:
        return "pos" if adds else "neu"
    r = adds / dels
    return "pos" if r > 1.25 else "neg" if r < 0.8 else "neu"


def bubble_r(lines, scale=0.33, rmax=30):
    return min(1.2 + scale * math.sqrt(lines), rmax)


def polar(cx, cy, r, a):
    """Angle `a` in radians, clockwise from 12 o'clock."""
    return cx + r * math.sin(a), cy - r * math.cos(a)


def relax(circles, iters=60, pull=0.08, pad=0.6):
    """Pushes overlapping circles apart while pulling each back toward its anchor.
    circles: list of dicts with x, y, r, ax, ay (anchor). Mutates in place."""
    if not circles:
        return
    cell = max(c["r"] for c in circles) * 2 + pad
    for _ in range(iters):
        grid = defaultdict(list)
        for i, c in enumerate(circles):
            grid[(int(c["x"] // cell), int(c["y"] // cell))].append(i)
        for (gx, gy), idxs in grid.items():
            near = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((gx + dx, gy + dy), ())]
            for i in idxs:
                a = circles[i]
                for j in near:
                    if j <= i:
                        continue
                    b = circles[j]
                    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
                    d = math.hypot(dx, dy) or 1e-6
                    overlap = a["r"] + b["r"] + pad - d
                    if overlap > 0:
                        ux, uy = dx / d, dy / d
                        wa = b["r"] / (a["r"] + b["r"])
                        a["x"] -= ux * overlap * wa
                        a["y"] -= uy * overlap * wa
                        b["x"] += ux * overlap * (1 - wa)
                        b["y"] += uy * overlap * (1 - wa)
        for c in circles:
            c["x"] += (c["ax"] - c["x"]) * pull
            c["y"] += (c["ay"] - c["y"]) * pull


# --------------------------------------------------------------------------- poster frame (space theme)

SPACE_DEFS = f"""<defs>
<radialGradient id="dataPositive"><stop offset="40%" stop-opacity="0" stop-color="{POS}"/><stop offset="100%" stop-opacity="1" stop-color="{POS}"/></radialGradient>
<radialGradient id="dataNeutral"><stop offset="40%" stop-opacity="0" stop-color="{NEU}"/><stop offset="100%" stop-opacity="1" stop-color="{NEU}"/></radialGradient>
<radialGradient id="dataNegative"><stop offset="40%" stop-opacity="0" stop-color="{NEG}"/><stop offset="100%" stop-opacity="1" stop-color="{NEG}"/></radialGradient>
<linearGradient id="dataLinear"><stop offset="0%" stop-opacity="0" stop-color="#ffffff"/><stop offset="100%" stop-opacity="1" stop-color="#ffffff"/></linearGradient>
<radialGradient id="background"><stop offset="0%" stop-color="#0E3E5D"/><stop offset="100%" stop-color="#011522"/></radialGradient>
<filter id="innershadow" x="-50%" y="-50%" width="200%" height="200%">
 <feGaussianBlur in="SourceAlpha" stdDeviation="3" result="blur"/><feOffset dy="10" dx="10"/>
 <feComposite in2="SourceAlpha" operator="arithmetic" k2="-1" k3="1" result="shadowDiff"/>
 <feFlood flood-color="#000000" flood-opacity="0.75"/><feComposite in2="shadowDiff" operator="in"/>
 <feComposite in2="SourceGraphic" operator="over"/>
</filter>
</defs>"""
FILL = {"pos": "url(#dataPositive)", "neu": "url(#dataNeutral)", "neg": "url(#dataNegative)"}


def title_svg(name, x, y, size, family, color="#fff"):
    if "/" in name:
        owner, repo = name.split("/", 1)
        return (f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" fill="{color}">'
                f'{esc(owner)}/<tspan font-weight="bold">{esc(repo)}</tspan></text>')
    return f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" font-weight="bold" fill="{color}">{esc(name)}</text>'


# Categorical slots validated for the dark poster surface (distinct for colour-blind readers too:
# worst adjacent CVD ΔE 8.4, all >= 3:1 contrast). Groups beyond four fold into a neutral "Other".
LANG_COLORS = ["#3987E5", "#D95926", "#199E70", "#C98500"]
OTHER_COLOR = "#7D8A96"
BAR_GAP = 2  # surface gap between stacked segments


def poster_header(d):
    """Title, range, key numbers and the language breakdown, laid out in two columns
    (left: x 50-410, right: x 450-770) so nothing crosses the divider at x 430."""
    out = [title_svg(d.name, 50, 70, 30, SANS),
           f'<text x="50" y="101" font-size="11" font-weight="bold" fill="#fff">{esc(d.date_range)}</text>',
           f'<text x="50" y="119" font-size="10" fill="#fff" fill-opacity="0.85">{len(d.commits):,} commits · '
           f'{len(d.user_commits):,} contributors · {d.files_touched:,} files</text>',
           f'<text x="50" y="135" font-size="10" fill="#fff" fill-opacity="0.85">'
           f'<tspan fill="{POS}">+{fmt_big(d.lines_added)}</tspan> / <tspan fill="{NEG}">\u2212{fmt_big(d.lines_removed)}</tspan>'
           f' lines · {d.active_days:,} active days</text>']
    if not d.languages:
        return "".join(out)
    x0, width = 450, 320
    out += ['<rect x="430" y="90" width="1" height="58" fill="#fff" fill-opacity="0.35"/>',
            f'<text x="{x0}" y="101" font-size="9" fill="#fff" fill-opacity="0.7">Share of lines changed</text>',
            f'<clipPath id="langbar"><rect x="{x0}" y="108" width="{width}" height="8" rx="4"/></clipPath>']
    bar, legend, x = [], [], x0
    lx, ly = x0, 133
    for i, (name, share) in enumerate(d.languages):
        color = OTHER_COLOR if name == "Other" else LANG_COLORS[i % len(LANG_COLORS)]
        w = width * share
        last = i == len(d.languages) - 1
        seg = max(w - (0 if last else BAR_GAP), 1)
        bar.append(f'<rect x="{x:.2f}" y="108" width="{seg:.2f}" height="8" fill="{color}">'
                   f'<title>{esc(name)} {share * 100:.1f}%</title></rect>')
        x += w
        short = name if len(name) <= 18 else name[:17].rstrip() + "…"
        label = f"{short} {share * 100:.0f}%" if share >= 0.01 else f"{short} <1%"
        gap_after = 12
        item_w = 14 + len(label) * 5.1 + gap_after  # swatch + text (~5.1px/char at 9.5px) + gap
        # Wrap to a second row only if the item itself (without its trailing gap) would pass the
        # right edge; the header legend at x=800 leaves 20px of slack after the bar.
        if lx + item_w - gap_after > x0 + width + 20 and lx > x0:
            lx, ly = x0, ly + 15
        legend.append(f'<rect x="{lx}" y="{ly - 8}" width="9" height="9" rx="2" fill="{color}"/>'
                      f'<text x="{lx + 14}" y="{ly}" font-size="9.5" fill="#fff"><title>{esc(name)}</title>{esc(label)}</text>')
        lx += item_w
    out.append(f'<g clip-path="url(#langbar)">{"".join(bar)}</g>')
    out += legend
    return "".join(out)


def space_poster(d, body, legend):
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0,0,{A_W},{A_H}" font-family="{SANS}">',
           SPACE_DEFS,
           f'<rect fill="url(#background)" width="{A_W}" height="{A_H}"/>',
           poster_header(d),
           f'<g transform="translate(800,45)">{legend}</g>',
           body,
           "</svg>"]
    return "\n".join(out)


def bubble_legend(d):
    return (f'<text x="0" y="0" font-size="13" font-weight="bold" fill="#fff">{len(d.commits):,} commits</text>'
            '<rect x="0" y="14" width="200" height="1" fill="#fff" fill-opacity="0.6"/>'
            f'<circle cx="5" cy="35" r="2" fill="{FILL["neu"]}"/><circle cx="17" cy="35" r="5" fill="{FILL["neu"]}"/>'
            f'<circle cx="37" cy="35" r="10" fill="{FILL["neu"]}"/>'
            '<text x="55" y="32" font-size="9" fill="#fff">lines of code</text>'
            f'<circle cx="8" cy="62" r="7" fill="{FILL["pos"]}"/><text x="25" y="65" font-size="9" fill="#fff">more lines added</text>'
            f'<circle cx="8" cy="82" r="7" fill="{FILL["neu"]}"/><text x="25" y="85" font-size="9" fill="#fff">about the same</text>'
            f'<circle cx="8" cy="102" r="7" fill="{FILL["neg"]}"/><text x="25" y="105" font-size="9" fill="#fff">more lines deleted</text>')


def circle_svg(c):
    return (f'<circle cx="{c["x"]:.1f}" cy="{c["y"]:.1f}" r="{c["r"]:.2f}" fill="{FILL[c["kind"]]}">'
            f'<title>{esc(c["title"])}</title></circle>')


# --------------------------------------------------------------------------- week-circles


def render_week_circles(d):
    cx, cy, R, R0 = A_W / 2, 790, 400, 30
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    sector = 2 * math.pi / 7
    circles = []
    for c in d.commits:
        t = c["t"]
        frac = (t.hour + t.minute / 60) / 24
        a = (t.weekday() + 0.04 + 0.92 * frac) * sector
        r = R0 + (R - R0) * math.sqrt(d.tnorm(t))
        x, y = polar(cx, cy, r, a)
        circles.append({"x": x, "y": y, "ax": x, "ay": y, "r": bubble_r(c["lines"], 0.17, 16), "kind": c["kind"],
                        "title": f'{t:%a %d %b %Y %H:%M} · +{c["adds"]} -{c["dels"]} · {d.users[c["user"]]["name"]}'})
    relax(circles, iters=40)

    counts = Counter(c["t"].weekday() for c in d.commits)
    total = max(len(d.commits), 1)
    busiest = counts.most_common(1)[0][0] if counts else 0
    body = []
    for i in range(7):  # alternating sector wedges
        a0, a1 = i * sector, (i + 1) * sector
        p0, p1 = polar(cx, cy, R + 15, a0), polar(cx, cy, R + 15, a1)
        body.append(f'<path d="M{cx},{cy}L{p0[0]:.1f},{p0[1]:.1f}A{R + 15},{R + 15},0,0,1,{p1[0]:.1f},{p1[1]:.1f}Z" '
                    f'fill="#fff" fill-opacity="{0.035 if i % 2 == 0 else 0.015}"/>')
        am = (i + 0.5) * sector
        lx, ly = polar(cx, cy, R + 60, am)
        deg = math.degrees(am)
        rot = deg - 180 if 90 < deg < 270 else deg
        pct = counts[i] * 100 / total
        extra = " of all commits" if i == busiest else ""
        body.append(f'<text xml:space="preserve" x="{lx:.1f}" y="{ly:.1f}" font-size="13" fill="#fff" text-anchor="middle" '
                    f'dominant-baseline="middle" transform="rotate({rot:.1f},{lx:.1f},{ly:.1f})">'
                    f'<tspan font-weight="bold">{days[i]}</tspan>'
                    f'<tspan fill-opacity="0.7"> {pct:.0f}%{extra}</tspan></text>')
    body += [circle_svg(c) for c in circles]
    return space_poster(d, "\n".join(body), bubble_legend(d))


# --------------------------------------------------------------------------- user-bubbles


def beeswarm(items, cy, max_offset=46):
    """items: circles with x, r sorted by x. Assigns y close to cy without overlap."""
    placed = []
    for c in sorted(items, key=lambda c: -c["r"]):
        best = None
        for step in range(0, int(max_offset * 4)):
            off = (step // 2 + (step % 2)) * 0.5 * (1 if step % 2 else -1)
            y = cy + off
            if all(math.hypot(c["x"] - p["x"], y - p["y"]) >= c["r"] + p["r"] + 0.4
                   for p in placed if abs(c["x"] - p["x"]) < c["r"] + p["r"] + 1):
                best = y
                break
        c["y"] = best if best is not None else cy
        placed.append(c)


def axis_ticks(d):
    """Year ticks for long ranges, month ticks (Jan, Feb, ...) for ranges under ~2 years.
    Every boundary inside the range is labelled, including the first one when the range starts
    exactly on it (e.g. a range from 1 Jan gets a "Jan" tick at its left edge)."""
    start = d.first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if (d.last - d.first).days > 730:
        years = [start.replace(year=y, month=1) for y in range(d.first.year, d.last.year + 1)]
        return [(t, str(t.year)) for t in years if d.first <= t <= d.last]
    ticks, t = [], start
    while t <= d.last:
        if t >= d.first:
            ticks.append((t, t.strftime("%b %Y") if t.month == 1 or not ticks else t.strftime("%b")))
        t = t.replace(year=t.year + (t.month == 12), month=t.month % 12 + 1)
    return ticks


def render_user_bubbles(d):
    top = d.top_users(10)
    rows = [(d.users[u]["name"], {u}) for u in top]
    others = set(d.user_commits) - set(top)
    if others:
        rows.append((f"{len(others)} others", others))

    # Horizontal: same 50px side margins as the header. The label column is as wide as the longest
    # name (bold 12px ≈ 7px/char, capped), then a gap, then the time axis; bubbles can overhang the
    # axis ends by their radius, so the axis is inset by the largest radius.
    margin, rmax, gap = 50, 16, 24
    label_w = min(max((len(label) for label, _ in rows), default=8) * 7 + 4, 220)
    label_x = margin + label_w
    x0, x1 = label_x + gap + rmax, A_W - margin - rmax

    # Vertical: spread the rows over the space between the header and the bottom margin, centred,
    # instead of a fixed pitch that leaves the bottom of the poster empty for small teams.
    area_top, area_bottom, axis_h = 220, A_H - 70, 40
    n = max(len(rows), 1)
    step = min(130, (area_bottom - area_top - axis_h) / n)
    block = n * step + axis_h
    y_first = area_top + (area_bottom - area_top - block) / 2 + step / 2

    body = []
    for i, (label, members) in enumerate(rows):
        ry = y_first + i * step
        items = [{"x": x0 + (x1 - x0) * d.tnorm(c["t"]), "r": bubble_r(c["lines"], 0.22, rmax), "kind": c["kind"],
                  "title": f'{c["t"]:%d %b %Y} · +{c["adds"]} -{c["dels"]}'}
                 for c in d.commits if c["user"] in members]
        beeswarm(items, ry, max_offset=step * 0.46)
        count = sum(d.user_commits[u] for u in members)
        short = label if len(label) <= 30 else label[:29].rstrip() + "…"
        body.append(f'<text x="{label_x}" y="{ry - 2:.1f}" font-size="12" font-weight="bold" fill="#fff" text-anchor="end">'
                    f'<title>{esc(label)}</title>{esc(short)}</text>'
                    f'<text x="{label_x}" y="{ry + 10:.1f}" font-size="9" fill="#fff" fill-opacity="0.6" text-anchor="end">'
                    f'{count:,} commits</text>')
        body += [circle_svg(c) for c in items]
    # time axis, just below the last row
    ay = y_first + (n - 1) * step + step / 2 + 10
    body.append(f'<rect x="{x0 - rmax}" y="{ay:.1f}" width="{x1 - x0 + 2 * rmax}" height="1" fill="#fff" fill-opacity="0.3"/>')
    for t, tick in axis_ticks(d):
        x = x0 + (x1 - x0) * d.tnorm(t)
        body.append(f'<text x="{x:.1f}" y="{ay + 14:.1f}" font-size="9" fill="#fff" fill-opacity="0.6" text-anchor="middle">{tick}</text>')
    return space_poster(d, "\n".join(body), bubble_legend(d))


# --------------------------------------------------------------------------- trunk


def lerp_color(stops, t):
    t = min(max(t, 0), 1) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    f = t - i
    a, b = stops[i], stops[i + 1]
    return "#" + "".join(f"{round(int(a[k:k + 2], 16) * (1 - f) + int(b[k:k + 2], 16) * f):02x}" for k in (1, 3, 5))


def closed_spline(pts):
    n = len(pts)
    out = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(n):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        out.append(f"C{c1[0]:.1f},{c1[1]:.1f},{c2[0]:.1f},{c2[1]:.1f},{p2[0]:.1f},{p2[1]:.1f}")
    return "".join(out) + "Z"


def render_trunk(d, layers=7):
    cx, cy, R0, R = A_W / 2, 790, 70, 360
    top = d.top_users(10)
    first_seen = {}
    for c in d.commits:
        first_seen.setdefault(c["user"], c["t"])
    top.sort(key=lambda u: first_seen[u])  # earliest contributor at 12 o'clock
    n = len(top)
    cum = [[0] * n for _ in range(layers)]
    idx = {u: i for i, u in enumerate(top)}
    for c in d.commits:
        if c["user"] in idx:
            k = min(int(d.tnorm(c["t"]) * layers), layers - 1)
            for kk in range(k, layers):
                cum[kk][idx[c["user"]]] += 1
    vmax = max(max(cum[-1], default=0), 1)
    # Earliest (inner) sand -> latest (outer) slate blue; the last step stays light enough to read
    # against the dark background.
    stops = ["#EADEC4", "#B4BDB9", "#7F95A6", "#557590", "#3A5A78"]
    body = []
    angles = [i * 2 * math.pi / max(n, 1) for i in range(n)]
    # A closed outline needs several points: with only 1-2 contributors, add in-between points
    # (pulled toward the centre) so each contributor still gets a smooth lobe.
    fillers = 0 if n >= 3 else (5 if n == 1 else 2)
    for k in reversed(range(layers)) if n else ():  # newest (largest, darkest) first
        radii = [R0 + (R - R0) * math.sqrt(cum[k][i] / vmax) for i in range(n)]
        pts = []
        for i in range(n):
            pts.append(polar(cx, cy, radii[i], angles[i]))
            nxt = (i + 1) % n
            gap = (angles[nxt] - angles[i]) % (2 * math.pi) or 2 * math.pi
            valley = R0 + (min(radii[i], radii[nxt]) - R0) * 0.45
            for f in range(1, fillers + 1):
                pts.append(polar(cx, cy, valley, angles[i] + gap * f / (fillers + 1)))
        body.append(f'<path d="{closed_spline(pts)}" fill="{lerp_color(stops, k / (layers - 1))}" '
                    f'filter="url(#innershadow)"/>')
    if not n:
        body.append(f'<text x="{cx}" y="{cy}" font-size="14" fill="#fff" fill-opacity="0.7" text-anchor="middle">'
                    'No contributors in this date range</text>')
    for i, u in enumerate(top):
        r = R0 + (R - R0) * math.sqrt(cum[-1][i] / vmax) + 55
        x, y = polar(cx, cy, r, angles[i])
        anchor = "start" if x > cx + 20 else "end" if x < cx - 20 else "middle"
        body.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="12" font-weight="bold" fill="#fff" text-anchor="{anchor}">'
                    f'{esc(d.users[u]["name"])}</text>'
                    f'<text x="{x:.1f}" y="{y + 14:.1f}" font-size="10" fill="#fff" fill-opacity="0.7" text-anchor="{anchor}">'
                    f'{d.user_commits[u]:,} commits</text>')
    # Legend: one step per era across the full width, 2px gaps, a faint outline so dark steps show.
    gap, width = 2, 200
    step_w = (width - gap * (layers - 1)) / layers
    era = (d.last - d.first) / layers
    sw = "".join(
        f'<rect x="{i * (step_w + gap):.2f}" y="24" width="{step_w:.2f}" height="12" rx="2" '
        f'fill="{lerp_color(stops, i / (layers - 1))}" stroke="#fff" stroke-opacity="0.3" stroke-width="0.75">'
        f'<title>{fmt_date(d.first + era * i)} – {fmt_date(d.first + era * (i + 1))}</title></rect>'
        for i in range(layers))
    legend = (f'<text x="0" y="0" font-size="13" font-weight="bold" fill="#fff">Time</text>'
              f'<rect x="0" y="12" width="{width}" height="1" fill="#fff" fill-opacity="0.6"/>{sw}'
              f'<text x="0" y="51" font-size="9.5" fill="#fff">{fmt_date(d.first)}</text>'
              f'<text x="{width}" y="51" font-size="9.5" fill="#fff" text-anchor="end">{fmt_date(d.last)}</text>'
              f'<text x="0" y="67" font-size="8.5" fill="#fff" fill-opacity="0.7">'
              f'Inner layers = earliest · outer = most recent</text>')
    return space_poster(d, "\n".join(body), legend)


# --------------------------------------------------------------------------- shared tree grouping (cross, core)


# Folders that only describe *where code lives*, not *what it is about*. They're skipped when naming a
# file's module so a Maven path like src/main/java/com/acme/app/billing/... becomes "billing".
STRUCTURAL = {"src", "source", "sources", "main", "java", "kotlin", "scala", "groovy", "resources", "webapp",
              "web-inf", "app", "lib", "libs", "pkg", "internal", "cmd", "jsons", "json", "static", "public",
              "screens", "screen", "components", "comps", "features", "feature", "modules", "module", "pages",
              "views", "containers", "packages", "apps", "test", "tests", "androidtest", "debug", "release",
              "__tests__", "__mocks__", "assets", "images", "img", "icons", "fonts"}
LAYERS = {"service", "services", "model", "models", "controller", "controllers", "dao", "daos", "dto", "dtos",
          "util", "utils", "helper", "helpers", "config", "configuration", "resource", "resources", "impl", "api",
          "v1", "v2", "v3", "constants", "constant", "enums", "enum", "request", "response", "requests",
          "responses", "pojo", "pojos", "entity", "entities", "bean", "beans", "repository", "repositories",
          "mapper", "mappers", "common", "core", "servermodel", "clientmodel", "dbmodel", "daomodel", "vo",
          "summary", "quote", "postquote", "prequote", "data", "domain", "handlers", "routes"}
TLDS = {"com", "org", "net", "io", "in", "co", "dev", "uk", "de", "fr", "app"}


def module_names(paths, weight):
    """Names each path's module: the first folder that says what the code is about.

    Structural folders (src/main/java, resources/jsons, screens/...) and a reverse-domain package
    prefix (com/<company>/...) are dropped; then any prefix most files share (e.g. "mobile" in
    com/acme/mobile/...) is dropped too; then layer names (services, model, controller...) are
    skipped. Files with nothing left are named after their top folder, or "(root)"."""
    trimmed = {}
    for p in paths:
        segs = p.split("/")[:-1]
        segs = [x for x in segs if x.lower() not in STRUCTURAL]
        if segs and segs[0].lower() in TLDS and len(segs) > 1:
            segs = segs[2:]                               # com/<company>
        trimmed[p] = segs
    # Strip prefixes shared by most of the weight (e.g. the app's base package), one level at a time.
    total = sum(weight.get(p, 1) for p in paths) or 1
    for _ in range(3):
        firsts = Counter()
        for p, segs in trimmed.items():
            if len(segs) > 1:
                firsts[segs[0]] += weight.get(p, 1)
        if not firsts:
            break
        top, w = firsts.most_common(1)[0]
        if w / total < 0.5:
            break
        for p, segs in trimmed.items():
            if len(segs) > 1 and segs[0] == top:
                trimmed[p] = segs[1:]
    meaningful = {p: [x for x in segs if x.lower() not in LAYERS] for p, segs in trimmed.items()}
    names = {}
    for p, segs in trimmed.items():
        if meaningful[p]:
            names[p] = meaningful[p][0]
        elif segs:
            names[p] = segs[0]
        else:
            top = p.split("/")[0] if "/" in p else None
            names[p] = top if top and top.lower() not in STRUCTURAL else "(root)"
    # An umbrella module holding a large share (e.g. "product" over term/health/motor) is split one
    # level deeper, so the designs show the real areas rather than one giant block.
    share = Counter()
    for p, n in names.items():
        share[n] += weight.get(p, 1)
    for umbrella, w in share.items():
        if w / total < 0.3:
            continue
        for p, n in names.items():
            if n == umbrella and len(meaningful[p]) > 1:
                names[p] = meaningful[p][1]
    return names


def path_groups(d, min_share=0.004, max_children=14):
    """Returns [(level1, weight, [(level2, weight, [file_idx...])...])...] ordered by weight.

    Level 1 is the file group (as in the poster headers: Services, Models, TS, Tests...), level 2 the
    module or feature folder (see module_names). Weight = number of commits touching the files."""
    touches = Counter()
    for c in d.commits:
        for f in c["files"]:
            touches[f["File"]] += 1
    total = sum(touches.values()) or 1
    paths = {fi: d.files[fi] for fi in touches}
    modules = module_names(list(paths.values()), {paths[fi]: w for fi, w in touches.items()})
    tree = defaultdict(lambda: defaultdict(list))
    for fi, path in paths.items():
        tree[d.classifier.group(path)][modules[path]].append(fi)
    groups, other = [], []
    for l1, kids in tree.items():
        w1 = sum(touches[f] for fs in kids.values() for f in fs)
        if w1 / total < min_share:
            other += [f for fs in kids.values() for f in fs]
            continue
        children = sorted(((l2, sum(touches[f] for f in fs), fs) for l2, fs in kids.items()), key=lambda x: -x[1])
        if len(children) > max_children:
            rest = [f for _, _, fs in children[max_children:] for f in fs]
            children = children[:max_children] + [("other", sum(touches[f] for f in rest), rest)]
        groups.append((l1, w1, children))
    groups.sort(key=lambda g: -g[1])
    if other:
        groups.append(("Other", sum(touches[f] for f in other), [("other", sum(touches[f] for f in other), other)]))
    return groups, touches


def footer_mono(d, y, color="#fff"):
    return (title_svg(d.name, 30, y, 14, MONO, color) +
            f'<text x="30" y="{y + 20}" font-family="{MONO}" font-size="10" fill="{color}">'
            f'{esc(d.date_range)} · {len(d.commits):,} commits · {len(d.user_commits):,} contributors</text>')


# --------------------------------------------------------------------------- cross


def render_cross(d, max_authors=100):
    W, H, y0, y1 = 1000, 1500, 175, 1325
    groups, _ = path_groups(d, max_children=24)  # the labelled column has room for more modules
    # authors: top N by commits, listed alphabetically, plus "others"
    top = d.top_users(max_authors)
    bold = set(d.top_users(20))
    authors = sorted(top, key=lambda u: d.users[u]["name"].lower())
    rest = set(d.user_commits) - set(top)
    ay_step = (y1 - y0) / max(len(authors) + (1 if rest else 0), 1)
    ay = {u: y0 + (i + 0.5) * ay_step for i, u in enumerate(authors)}
    others_y = y0 + (len(authors) + 0.5) * ay_step
    file_seg = {}
    for gi, (_, _, children) in enumerate(groups):
        for ci, (_, _, fs) in enumerate(children):
            for f in fs:
                file_seg[f] = (gi, ci)
    seg_auth = defaultdict(Counter)
    for c in d.commits:
        for f in c["files"]:
            seg_auth[file_seg[f["File"]]][c["user"] if c["user"] in ay else -1] += 1

    gap = 4
    total = sum(g[1] for g in groups) or 1
    scale = (y1 - y0 - gap * (len(groups) - 1)) / total
    X1, X2, X3, XA = 14, 30, 150, 590.4  # group column, module column (labelled), ribbon start, authors
    body, ribbons, y = [], [], y0
    max_w = max((w for sa in seg_auth.values() for w in sa.values()), default=1)
    for gi, (l1, w1, children) in enumerate(groups):
        h1 = w1 * scale
        body.append(f'<rect x="{X1}" y="{y:.2f}" width="{X2 - X1}" height="{h1:.2f}" fill="#fff" stroke="#054367" stroke-width="0.6"/>')
        label = next((t for t in (f"{l1} ({fmt_k(w1)} commits)", l1) if len(t) * 5.2 + 8 < h1), None)
        if label:
            tx, ty = (X1 + X2) / 2 + 3, y + h1 / 2
            body.append(f'<text x="{tx}" y="{ty:.1f}" font-family="{MONO}" font-size="9" fill="#054367" text-anchor="middle" '
                        f'transform="rotate(-90,{tx},{ty:.1f})">{esc(label)}</text>')
        cy_ = y
        for ci, (l2, w2, _) in enumerate(children):
            h2 = w2 * scale
            body.append(f'<rect x="{X2}" y="{cy_:.2f}" width="{X3 - X2}" height="{h2:.2f}" fill="#fff" fill-opacity="0.85" '
                        f'stroke="#054367" stroke-width="0.4"><title>{esc(l1)} › {esc(l2)} ({w2} commits)</title></rect>')
            if h2 >= 8:  # label the module when the box is tall enough for a line of text
                fs = min(9, h2 - 1)
                text = f"{l2} ({fmt_k(w2)})"
                max_chars = int((X3 - X2 - 6) / (fs * 0.6))
                if len(text) > max_chars:
                    text = text[:max_chars - 1] + "…"
                body.append(f'<text x="{X2 + 4}" y="{cy_ + h2 / 2 + fs * 0.35:.2f}" font-family="{MONO}" '
                            f'font-size="{fs:.1f}" fill="#054367">{esc(text)}</text>')
            sa = seg_auth[(gi, ci)]
            order = sorted(sa, key=lambda u: ay.get(u, others_y))
            tot = sum(sa.values()) or 1
            sy = cy_
            for u in order:
                hh = h2 * sa[u] / tot
                ty_ = ay.get(u, others_y)
                mx = (X3 + XA) / 2
                op = 0.08 + 0.35 * math.sqrt(sa[u] / max_w)
                ribbons.append(f'<path d="M{XA},{ty_:.1f}C{mx},{ty_:.1f},{mx},{sy:.2f},{X3},{sy:.2f}L{X3},{sy + hh:.2f}'
                               f'C{mx},{sy + hh:.2f},{mx},{ty_:.1f},{XA},{ty_:.1f}Z" fill="#fff" fill-opacity="{op:.3f}"/>')
                sy += hh
            cy_ += h2
        y += h1 + gap
    for u in authors:
        b = ' font-weight="bold"' if u in bold else ' fill-opacity="0.75"'
        body.append(f'<text x="{XA + 3}" y="{ay[u] + 3:.1f}" font-family="{MONO}" font-size="{min(9, ay_step * 0.95):.1f}" '
                    f'fill="#fff"{b}>·{esc(d.users[u]["name"])}</text>')
    if rest:
        body.append(f'<text x="{XA + 3}" y="{others_y + 3:.1f}" font-family="{MONO}" font-size="9" fill="#fff" '
                    f'fill-opacity="0.75">·...and {len(rest)} others</text>')
    return "\n".join([f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0,0,{W},{H}">',
                      f'<rect fill="#054367" width="{W}" height="{H}"/>', *ribbons, *body,
                      footer_mono(d, 1400), "</svg>"])


# --------------------------------------------------------------------------- core


def arc_path(r, a0, a1):
    p0, p1 = polar(0, 0, r, a0), polar(0, 0, r, a1)
    large = 1 if a1 - a0 > math.pi else 0
    return f"M{p0[0]:.2f},{p0[1]:.2f}A{r},{r},0,{large},1,{p1[0]:.2f},{p1[1]:.2f}"


def band(r0, r1, a0, a1):
    p0, p1 = polar(0, 0, r1, a0), polar(0, 0, r1, a1)
    q1, q0 = polar(0, 0, r0, a1), polar(0, 0, r0, a0)
    large = 1 if a1 - a0 > math.pi else 0
    return (f"M{p0[0]:.2f},{p0[1]:.2f}A{r1},{r1},0,{large},1,{p1[0]:.2f},{p1[1]:.2f}"
            f"L{q1[0]:.2f},{q1[1]:.2f}A{r0},{r0},0,{large},0,{q0[0]:.2f},{q0[1]:.2f}Z")


def label_on_arc(uid, r, a0, a1, text, size, color):
    mid = (a0 + a1) / 2 % (2 * math.pi)
    if math.pi / 2 < mid < 3 * math.pi / 2:  # bottom half: run the path backwards so text stays upright
        p0, p1 = polar(0, 0, r + size * 0.7, a1), polar(0, 0, r + size * 0.7, a0)
        d_ = f"M{p0[0]:.2f},{p0[1]:.2f}A{r + size * 0.7},{r + size * 0.7},0,0,0,{p1[0]:.2f},{p1[1]:.2f}"
    else:
        d_ = arc_path(r, a0, a1)
    return (f'<path id="{uid}" d="{d_}" fill="none"/><text font-family="{MONO}" font-size="{size}" fill="{color}">'
            f'<textPath xlink:href="#{uid}" href="#{uid}" startOffset="50%" text-anchor="middle">{esc(text)}</textPath></text>')


def render_core(d, max_pairs=3000, max_commit_files=12):
    S, cx, cy = 1500, 750, 730
    Rc, Rm, R2, R1, Rout = 467, 505, 505, 530, 590
    groups, touches = path_groups(d)
    gap = 0.006
    total = sum(g[1] for g in groups) or 1
    avail = 2 * math.pi - gap * len(groups)
    file_arc, body = {}, []
    a = 0.0
    max_touch = max(touches.values(), default=1)
    for gi, (l1, w1, children) in enumerate(groups):
        a1_ = a + avail * w1 / total
        body.append(f'<path d="{band(R1, Rout, a, a1_)}" fill="#fff" stroke="#000" stroke-width="1"/>')
        if a1_ - a > 0.05:
            body.append(label_on_arc(f"g{gi}", (R1 + Rout) / 2 - 4, a, a1_, f"{l1} ({fmt_k(w1)} commits)" if a1_ - a > 0.2 else l1,
                                     10, "#000"))
        ca = a
        for ci, (l2, w2, fs) in enumerate(children):
            ca1 = ca + avail * w2 / total
            body.append(f'<path d="{band(R2, R1, ca, ca1)}" fill="#fff" fill-opacity="0.92" stroke="#000" stroke-width="0.5"/>')
            if ca1 - ca > 0.06 and l2 != l1:
                body.append(label_on_arc(f"g{gi}c{ci}", (R2 + R1) / 2 - 3, ca, ca1, l2, 7, "#000"))
            fa = ca
            for f in sorted(fs, key=lambda f: d.files[f]):
                fa1 = fa + avail * touches[f] / total
                file_arc[f] = (fa, fa1)
                shade = int(255 * (1 - math.sqrt(touches[f] / max_touch)) * 0.85 + 30)
                body.append(f'<path class="map" d="{band(Rc, Rm, fa, fa1)}" fill="rgb({shade},{shade},{shade})">'
                            f'<title>{esc(d.files[f])} ({touches[f]} commits)</title></path>')
                fa = fa1
            ca = ca1
        body.append(f'<path d="{band(Rc, Rout + 6, a1_, a1_ + gap)}" fill="#000"/>')
        a = a1_ + gap

    pairs = Counter()
    for c in d.commits:
        fs = sorted({f["File"] for f in c["files"]})
        if 2 <= len(fs) <= max_commit_files:
            for i in range(len(fs)):
                for j in range(i + 1, len(fs)):
                    pairs[(fs[i], fs[j])] += 1
    top_pairs = pairs.most_common(max_pairs)
    # split each file's arc among its chord ends, ordered by partner angle to reduce crossings
    ends = defaultdict(list)
    for (f1, f2), w in top_pairs:
        ends[f1].append((f2, w))
        ends[f2].append((f1, w))
    slot = {}
    for f, lst in ends.items():
        a0, a1 = file_arc[f]
        tot = sum(w for _, w in lst)
        lst.sort(key=lambda e: (file_arc[e[0]][0] - a0) % (2 * math.pi))
        s = a0
        for other, w in lst:
            e = s + (a1 - a0) * w / tot
            slot[(f, other)] = (s, e)
            s = e
    max_w = top_pairs[0][1] if top_pairs else 1
    chords = []
    for (f1, f2), w in reversed(top_pairs):  # strongest drawn last (on top)
        a0, a1 = slot[(f1, f2)]
        b0, b1 = slot[(f2, f1)]
        p0, p1 = polar(0, 0, Rc, a0), polar(0, 0, Rc, a1)
        q0, q1 = polar(0, 0, Rc, b0), polar(0, 0, Rc, b1)
        op = 0.06 + 0.5 * math.sqrt(w / max_w)
        if max(a1 - a0, b1 - b0) > 0.04:  # broad ribbons would hide everything else
            op = min(op, 0.18)
        chords.append(f'<path d="M{p0[0]:.1f},{p0[1]:.1f}A{Rc},{Rc},0,0,1,{p1[0]:.1f},{p1[1]:.1f}Q0,0,{q0[0]:.1f},{q0[1]:.1f}'
                      f'A{Rc},{Rc},0,0,1,{q1[0]:.1f},{q1[1]:.1f}Q0,0,{p0[0]:.1f},{p0[1]:.1f}Z" fill="#fff" fill-opacity="{op:.3f}"/>')
    return "\n".join([f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0,0,{S},{S}">',
                      f'<rect fill="#000" width="{S}" height="{S}"/>',
                      f'<g transform="translate({cx},{cy})">', *chords, *body, "</g>",
                      footer_mono(d, 1420), "</svg>"])


# --------------------------------------------------------------------------- main

RENDERERS = {
    "week-circles": render_week_circles,
    "user-bubbles": render_user_bubbles,
    "trunk": render_trunk,
    "cross": render_cross,
    "core": render_core,
}


def write_index(out, names, d):
    cards = "\n".join(f'<figure><a href="{n}.svg"><img src="{n}.svg" alt="{n}" loading="lazy"></a>'
                      f'<figcaption>{n}</figcaption></figure>' for n in names)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(d.name)} visualizations</title><style>
body{{margin:0;padding:24px 16px;background:#0b1620;color:#e8eef2;font:14px system-ui,sans-serif}}
h1{{font-size:20px;margin:0 0 4px}}p{{margin:0 0 20px;opacity:.7}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px}}
figure{{margin:0}}img{{width:100%;display:block;border-radius:6px;background:#000}}figcaption{{margin-top:6px;opacity:.8}}
</style></head><body><h1>{esc(d.name)}</h1><p>{esc(d.date_range)} · {len(d.commits):,} commits · {len(d.user_commits):,} contributors</p>
<main>{cards}</main></body></html>"""
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def render_all(d, out, designs=None):
    """Writes one SVG per design plus an index.html gallery. Returns the SVG paths."""
    names = designs or list(RENDERERS)
    unknown = [n for n in names if n not in RENDERERS]
    if unknown:
        raise SystemExit(f"Unknown design(s): {', '.join(unknown)}. Available: {', '.join(RENDERERS)}")
    if not d.commits and d.bots:
        print("  Warning: every commit in this range is from a bot; the visualizations will be empty. "
              "Use --include-bots to show them.")
    os.makedirs(out, exist_ok=True)
    paths = []
    for n in names:
        svg = RENDERERS[n](d)
        path = os.path.join(out, f"{n}.svg")
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        print(f"  {path} ({len(svg) / 1024:.0f} KB)")
        paths.append(path)
    # List every design present in the folder, so a partial re-render keeps earlier ones.
    write_index(out, [n for n in RENDERERS if os.path.exists(os.path.join(out, f"{n}.svg"))], d)
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("analysis")
    ap.add_argument("--out", default="viz")
    ap.add_argument("--designs", help=f"comma-separated, default all: {','.join(RENDERERS)}")
    ap.add_argument("--title", "--name", dest="title", help="override the project title")
    ap.add_argument("--include-bots", action="store_true", help="keep dependabot/CI bot commits")
    ap.add_argument("--aliases", help='JSON file: {"aliases": {"old name": "Final Name"}, "exclude": [...]}')
    ap.add_argument("--file-groups", help=f"file-groups JSON file or preset ({', '.join(PRESETS)}); "
                                          "default: detected from the files")
    a = ap.parse_args()
    aliases, exclude = load_aliases(a.aliases) if a.aliases else ({}, set())
    profile = load_profile(a.file_groups) if a.file_groups else None
    with open(a.analysis, encoding="utf-8") as f:
        d = Data(json.load(f), a.title, a.include_bots, aliases, exclude, profile)
    designs = [n.strip() for n in a.designs.split(",") if n.strip()] if a.designs else None
    render_all(d, a.out, designs)


if __name__ == "__main__":
    main()
