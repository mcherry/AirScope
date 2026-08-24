#!/usr/bin/env python3
"""
airscope-web - read-only view of what AirScope has recorded.

Runs as a separate service from the poller and opens the database read-only, so
a bug here cannot corrupt history. Renders no JavaScript at all: every chart is
server-generated SVG, which lets the Content-Security-Policy forbid scripts
outright. Callsigns are arbitrary bytes chosen by whoever is transmitting, so
everything from the feed is HTML-escaped on the way out.

    airscope-web.py --database /var/lib/airscope/airscope.db --bind 0.0.0.0:8080

Copyright (C) 2026 AirScope contributors. Licensed under the GNU General Public
License v3 or later; see LICENSE. This program comes with ABSOLUTELY NO WARRANTY.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import signal
import socketserver
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from airscope import (COMPASS, Stopped, _handle_stop, compass,  # noqa: E402
                      decode_track, duration, find_config, load_config, setting)

CSP = ("default-src 'none'; style-src 'self'; img-src 'self' https: data:; "
       "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")

CSS = """
:root {
  --bg:#05080d; --panel:#0a0f16; --edge:#16202c; --edge2:#1e2b3a;
  --ink:#c8d6e5; --dim:#5d7189; --cyan:#00e5ff; --amber:#ffb000;
  --hot:#ff4d6d; --grid:rgba(0,229,255,.06);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg); color:var(--ink); min-height:100vh;
  font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  background-image:linear-gradient(var(--grid) 1px,transparent 1px),
                   linear-gradient(90deg,var(--grid) 1px,transparent 1px);
  background-size:44px 44px;
}
a{color:var(--cyan);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:22px 18px 60px}
header{display:flex;align-items:baseline;gap:14px;border-bottom:1px solid var(--edge);
  padding-bottom:12px;margin-bottom:20px;flex-wrap:wrap}
header h1{font-size:16px;margin:0;letter-spacing:.34em;text-transform:uppercase;
  color:var(--cyan);text-shadow:0 0 18px rgba(0,229,255,.45)}
header .sub{color:var(--dim);font-size:11px;letter-spacing:.18em;text-transform:uppercase}
header nav{margin-left:auto;display:flex;gap:16px}
.grid{display:grid;gap:14px}
.cols{grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
/* Both card rows have exactly five items; auto-fit alone drops one to a
   second row once a scrollbar narrows the container. */
@media (min-width:900px){.cols{grid-template-columns:repeat(5,1fr)}}
.flex1{flex:1;min-width:330px}
.mt{margin-top:14px}
.mt-s{margin-top:6px}
.panel{background:var(--panel);border:1px solid var(--edge);border-radius:3px;padding:14px 16px;
  position:relative}
.panel::before{content:"";position:absolute;inset:0 auto auto 0;width:16px;height:1px;
  background:var(--cyan);opacity:.65}
.label{font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--dim)}
.stat{font-size:26px;line-height:1.15;margin-top:6px;color:var(--ink);
  font-variant-numeric:tabular-nums}
.stat.cy{color:var(--cyan)} .stat.am{color:var(--amber)}
.unit{font-size:11px;color:var(--dim);margin-left:5px}
h2{font-size:11px;letter-spacing:.24em;text-transform:uppercase;color:var(--dim);
  margin:26px 0 10px;border-bottom:1px solid var(--edge);padding-bottom:7px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);
  text-align:left;font-weight:400;padding:7px 9px;border-bottom:1px solid var(--edge)}
td{padding:7px 9px;border-bottom:1px solid rgba(22,32,44,.55);white-space:nowrap}
tbody tr:hover{background:rgba(0,229,255,.045)}
td.r,th.r{text-align:right}
.mil{color:var(--amber)}
.tag{display:inline-block;border:1px solid var(--edge2);border-radius:2px;
  padding:1px 6px;font-size:10px;letter-spacing:.1em;color:var(--dim)}
.tag.close{border-color:var(--hot);color:var(--hot)}
.tag.near{border-color:var(--amber);color:var(--amber)}
.hero{display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start}
.hero img{border:1px solid var(--edge);border-radius:3px;max-width:340px;height:auto}
.kv{display:grid;grid-template-columns:auto 1fr;gap:5px 16px;font-size:12px}
.kv dt{color:var(--dim);text-transform:uppercase;font-size:10px;letter-spacing:.14em;
  align-self:center}
.kv dd{margin:0;color:var(--ink)}
.empty{color:var(--dim);padding:26px 0;text-align:center;letter-spacing:.1em}
footer{margin-top:34px;padding-top:12px;border-top:1px solid var(--edge);
  color:var(--dim);font-size:10px;letter-spacing:.14em;text-transform:uppercase}
svg{display:block;max-width:100%;height:auto}
.sweep{fill:var(--cyan);opacity:.055}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}

@media (max-width:640px){
  .wrap{padding:14px 11px 40px}
  header{gap:8px;padding-bottom:9px;margin-bottom:14px}
  header h1{font-size:14px;letter-spacing:.24em}
  header .sub{display:none}
  header nav{margin-left:auto;gap:14px;font-size:12px}
  .cols{grid-template-columns:repeat(2,1fr);gap:9px}
  .panel{padding:11px 12px}
  .stat{font-size:21px}
  /* flex:1 sets flex-basis:0, so width:100% alone will not force a wrap. */
  .hero{display:block}
  .hero>*+*{margin-top:12px}
  .flex1{min-width:0;width:auto}
  .hero img{max-width:100%}
  .opt{display:none}
  th,td{padding:8px 7px}
  td{white-space:normal}
  .kv{grid-template-columns:1fr;gap:2px 0}
  .kv dt{margin-top:7px}
  h2{margin:18px 0 8px}
}
"""


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def ago(ts) -> str:
    if not ts:
        return "-"
    delta = max(0, time.time() - ts)
    if delta < 90:
        return f"{int(delta)}s ago"
    if delta < 5400:
        return f"{int(delta / 60)}m ago"
    if delta < 172800:
        return f"{int(delta / 3600)}h ago"
    return time.strftime("%d %b", time.localtime(ts))


def stamp(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def dist_tag(nm) -> str:
    if nm is None:
        return '<span class="tag">no position</span>'
    cls = "close" if nm <= 2 else "near" if nm <= 8 else ""
    return f'<span class="tag {cls}">{nm:.1f} nm</span>'


def radar_svg(points: list, size: int = 320, rings=(10, 25, 50, 100)) -> str:
    """Polar plot of closest approaches. points = [(bearing, range_nm, label)]."""
    cx = cy = size / 2
    span = max(rings)
    out = [f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
           f'role="img" aria-label="Range and bearing of recent passes">']
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{size/2-1}" class="sweep"/>')
    for ring in rings:
        r = (ring / span) * (size / 2 - 14)
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="none" '
                   f'stroke="#16202c" stroke-width="1"/>')
        out.append(f'<text x="{cx+3}" y="{cy-r+11:.1f}" fill="#3d4f63" '
                   f'font-size="9" font-family="monospace">{ring}</text>')
    for i in range(8):
        angle = math.radians(i * 45)
        x = cx + math.sin(angle) * (size / 2 - 14)
        y = cy - math.cos(angle) * (size / 2 - 14)
        out.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                   f'stroke="#111a24" stroke-width="1"/>')
        lx = cx + math.sin(angle) * (size / 2 - 4)
        ly = cy - math.cos(angle) * (size / 2 - 4)
        out.append(f'<text x="{lx:.1f}" y="{ly:.1f}" fill="#3d4f63" font-size="9" '
                   f'text-anchor="middle" dominant-baseline="middle" '
                   f'font-family="monospace">{COMPASS[i*2]}</text>')
    for bearing, rng, label in points:
        if bearing is None or rng is None:
            continue
        r = min(rng, span) / span * (size / 2 - 14)
        angle = math.radians(bearing)
        x = cx + math.sin(angle) * r
        y = cy - math.cos(angle) * r
        colour = "#ff4d6d" if rng <= 2 else "#ffb000" if rng <= 8 else "#00e5ff"
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{colour}">'
                   f'<title>{esc(label)}</title></circle>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="2" fill="#c8d6e5"/>')
    out.append("</svg>")
    return "".join(out)


def track_svg(track: list, width: int = 620, height: int = 300) -> str:
    """Ground path in a north-up frame centred on the observer."""
    pts = [p for p in track if p["lat"] and p["lon"]]
    if len(pts) < 2:
        return '<p class="empty">no positional track recorded</p>'
    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    mid = math.radians(sum(lats) / len(lats))
    xs = [(lon - min(lons)) * 60 * math.cos(mid) for lon in lons]
    ys = [(lat - min(lats)) * 60 for lat in lats]
    pad = 26

    # One scale for both axes, centred: a mostly east-west track must not be
    # stretched vertically or pinned to a corner.
    span = max(max(xs) - min(xs), max(ys) - min(ys), 0.5)
    scale = min((width - 2 * pad) / span, (height - 2 * pad) / span)
    mx, my = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2

    def sx(x):
        return width / 2 + (x - mx) * scale

    def sy(y):
        return height / 2 - (y - my) * scale

    path = " ".join(f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}"
                    for i, (x, y) in enumerate(zip(xs, ys)))
    out = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
           f'role="img" aria-label="Ground track">',
           f'<path d="{path}" fill="none" stroke="#00e5ff" stroke-width="1.6" '
           f'stroke-linejoin="round" opacity="0.9"/>',
           f'<circle cx="{sx(xs[0]):.1f}" cy="{sy(ys[0]):.1f}" r="3.5" fill="#5d7189"/>',
           f'<circle cx="{sx(xs[-1]):.1f}" cy="{sy(ys[-1]):.1f}" r="4" fill="#ffb000"/>',
           f'<text x="{pad}" y="16" fill="#3d4f63" font-size="9" '
           f'font-family="monospace">{span:.1f} NM ACROSS &#183; NORTH UP</text>',
           "</svg>"]
    return "".join(out)


def altitude_svg(track: list, width: int = 620, height: int = 110) -> str:
    alts = [p["alt"] for p in track if p["alt"]]
    if len(alts) < 2:
        return ""
    lo, hi = min(alts), max(alts)
    rng = max(hi - lo, 500)
    step = (width - 20) / max(len(alts) - 1, 1)
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{10 + i*step:.1f},{height-14-(a-lo)/rng*(height-30):.1f}"
        for i, a in enumerate(alts))
    return ("".join([
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="Altitude profile">',
        f'<path d="{path}" fill="none" stroke="#ffb000" stroke-width="1.5"/>',
        f'<text x="10" y="12" fill="#3d4f63" font-size="9" font-family="monospace">'
        f'{hi:,} FT</text>',
        f'<text x="10" y="{height-3}" fill="#3d4f63" font-size="9" '
        f'font-family="monospace">{lo:,} FT</text>',
        "</svg>"]))


def page(title: str, body: str) -> bytes:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{esc(title)} &#183; AirScope</title>
<link rel="stylesheet" href="/static/app.css">
</head><body><div class="wrap">
<header>
  <h1>AirScope</h1>
  <span class="sub">military traffic recorder</span>
  <nav><a href="/">Overview</a><a href="/aircraft">Airframes</a></nav>
</header>
{body}
<footer>read-only view &#183; generated {esc(time.strftime('%Y-%m-%d %H:%M:%S'))}</footer>
</div></body></html>""".encode("utf-8")


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------

class Views:
    def __init__(self, db_path: Path):
        self.path = db_path

    def connect(self):
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def overview(self) -> bytes:
        with self.connect() as db:
            totals = db.execute(
                "SELECT (SELECT COUNT(*) FROM aircraft) airframes,"
                " (SELECT COUNT(*) FROM visits) visits,"
                " (SELECT MIN(closest_nm) FROM visits) nearest,"
                " (SELECT COUNT(*) FROM visits WHERE started > ?) today,"
                " (SELECT COUNT(*) FROM visits WHERE ended IS NULL) live",
                (time.time() - 86400,)).fetchone()
            rows = db.execute("""
                SELECT v.*, a.registration, a.type_code, a.description, a.operator
                FROM visits v LEFT JOIN aircraft a USING (icao)
                ORDER BY v.started DESC LIMIT 60""").fetchall()

        nearest = f"{totals['nearest']:.1f}" if totals["nearest"] is not None else "-"
        cards = f"""
<div class="grid cols">
  <div class="panel"><div class="label">Airframes seen</div>
    <div class="stat cy">{totals['airframes']}</div></div>
  <div class="panel"><div class="label">Total passes</div>
    <div class="stat">{totals['visits']}</div></div>
  <div class="panel"><div class="label">Last 24 hours</div>
    <div class="stat">{totals['today']}</div></div>
  <div class="panel"><div class="label">Closest ever</div>
    <div class="stat am">{nearest}<span class="unit">nm</span></div></div>
  <div class="panel"><div class="label">In view now</div>
    <div class="stat {'cy' if totals['live'] else ''}">{totals['live']}</div></div>
</div>"""

        plot = radar_svg([(r["closest_bearing"], r["closest_nm"],
                           f"{r['type_code'] or r['icao']} {r['callsign'] or ''} "
                           f"{r['closest_nm']:.1f} nm" if r["closest_nm"] else "")
                          for r in rows])

        body = ["<h2>Recent passes</h2>",
                '<div class="hero">', f'<div class="panel">{plot}</div>',
                '<div class="flex1"><div class="scroll"><table>',
                "<thead><tr><th>When</th><th>Type</th><th>Callsign</th>"
                "<th class='opt'>Registration</th><th class='r'>Closest</th>"
                "<th class='r opt'>Alt</th></tr></thead><tbody>"]
        if not rows:
            body.append('<tr><td colspan="6" class="empty">nothing recorded yet</td></tr>')
        for r in rows:
            alt = f"{r['max_alt']:,}" if r["max_alt"] else "-"
            body.append(
                f"<tr><td><a href='/visit/{r['id']}'>{esc(ago(r['started']))}</a></td>"
                f"<td class='mil'>{esc(r['type_code'] or '?')}</td>"
                f"<td>{esc(r['callsign'] or '')}</td>"
                f"<td class='opt'><a href='/aircraft/{esc(r['icao'])}'>"
                f"{esc(r['registration'] or r['icao'])}</a></td>"
                f"<td class='r'>{dist_tag(r['closest_nm'])}</td>"
                f"<td class='r opt'>{alt}</td></tr>")
        body.append("</tbody></table></div></div></div>")
        return page("Overview", "".join([cards] + body))

    def airframes(self) -> bytes:
        with self.connect() as db:
            rows = db.execute("""
                SELECT a.*, MIN(v.closest_nm) best FROM aircraft a
                LEFT JOIN visits v USING (icao)
                GROUP BY a.icao ORDER BY a.last_seen DESC LIMIT 300""").fetchall()
        out = ["<h2>Airframes</h2><div class='scroll'><table><thead><tr>"
               "<th>Registration</th><th>Type</th>"
               "<th class='opt'>Operator</th><th class='r opt'>Passes</th>"
               "<th class='r'>Closest</th>"
               "<th class='r'>Last seen</th></tr></thead><tbody>"]
        if not rows:
            out.append('<tr><td colspan="6" class="empty">nothing recorded yet</td></tr>')
        for r in rows:
            out.append(
                f"<tr><td><a href='/aircraft/{esc(r['icao'])}'>"
                f"{esc(r['registration'] or r['icao'])}</a></td>"
                f"<td class='mil'>{esc(r['type_code'] or '?')}</td>"
                f"<td class='opt'>{esc(r['operator'] or r['description'] or '')}</td>"
                f"<td class='r opt'>{r['visit_count']}</td>"
                f"<td class='r'>{dist_tag(r['best'])}</td>"
                f"<td class='r'>{esc(ago(r['last_seen']))}</td></tr>")
        out.append("</tbody></table></div>")
        return page("Airframes", "".join(out))

    def aircraft(self, icao: str) -> bytes | None:
        with self.connect() as db:
            ac = db.execute("SELECT * FROM aircraft WHERE icao = ?", (icao,)).fetchone()
            if not ac:
                return None
            visits = db.execute(
                "SELECT * FROM visits WHERE icao = ? ORDER BY started DESC LIMIT 100",
                (icao,)).fetchall()

        photo = ""
        if ac["photo_thumb"]:
            credit = f"&#169; {esc(ac['photographer'])}" if ac["photographer"] else ""
            link = esc(ac["photo_link"] or "#")
            photo = (f'<div><a href="{link}" rel="noreferrer noopener">'
                     f'<img src="{esc(ac["photo_thumb"])}" alt="Photograph of '
                     f'{esc(ac["registration"] or icao)}"></a>'
                     f'<div class="label mt-s">{credit}</div></div>')

        best = min((v["closest_nm"] for v in visits if v["closest_nm"] is not None),
                   default=None)
        details = f"""
<div class="panel flex1">
<dl class="kv">
  <dt>ICAO</dt><dd>{esc(icao)}</dd>
  <dt>Registration</dt><dd>{esc(ac['registration'] or '-')}</dd>
  <dt>Type</dt><dd class="mil">{esc(ac['type_code'] or '-')}</dd>
  <dt>Description</dt><dd>{esc(ac['description'] or '-')}</dd>
  <dt>Operator</dt><dd>{esc(ac['operator'] or '-')}</dd>
  <dt>Passes</dt><dd>{ac['visit_count']}</dd>
  <dt>Closest</dt><dd>{dist_tag(best)}</dd>
  <dt>First seen</dt><dd>{esc(stamp(ac['first_seen']))}</dd>
  <dt>Last seen</dt><dd>{esc(stamp(ac['last_seen']))}</dd>
</dl></div>"""

        rows = ["<h2>Passes</h2><div class='scroll'><table><thead><tr>"
                "<th>Started</th><th>Callsign</th>"
                "<th class='r opt'>Duration</th><th class='r'>Closest</th>"
                "<th class='r'>Altitude</th><th class='r opt'>Points</th>"
                "</tr></thead><tbody>"]
        for v in visits:
            span = (v["ended"] or v["last_seen"]) - v["started"]
            alt = f"{v['min_alt']:,}&#8211;{v['max_alt']:,}" if v["max_alt"] else "-"
            rows.append(
                f"<tr><td><a href='/visit/{v['id']}'>{esc(stamp(v['started']))}</a></td>"
                f"<td>{esc(v['callsign'] or '')}</td>"
                f"<td class='r opt'>{esc(duration(span))}</td>"
                f"<td class='r'>{dist_tag(v['closest_nm'])}</td>"
                f"<td class='r'>{alt}</td><td class='r opt'>{v['points']}</td></tr>")
        rows.append("</tbody></table></div>")

        title = ac["registration"] or icao
        return page(title, f'<div class="hero">{photo}{details}</div>' + "".join(rows))

    def visit(self, visit_id: int) -> bytes | None:
        with self.connect() as db:
            v = db.execute("""
                SELECT v.*, a.registration, a.type_code, a.description, a.operator
                FROM visits v LEFT JOIN aircraft a USING (icao)
                WHERE v.id = ?""", (visit_id,)).fetchone()
            if not v:
                return None
        track = decode_track(v["track"], v["started"])
        span = (v["ended"] or v["last_seen"]) - v["started"]
        bearing = (f"{v['closest_bearing']:.0f}&#176; {compass(v['closest_bearing'])}"
                   if v["closest_bearing"] is not None else "-")
        elev = (f"{v['closest_elevation']:.0f}&#176;"
                if v["closest_elevation"] is not None else "-")
        head = f"""
<div class="grid cols">
  <div class="panel"><div class="label">Closest approach</div>
    <div class="stat am">{f"{v['closest_nm']:.1f}" if v['closest_nm'] is not None else '-'}
    <span class="unit">nm</span></div></div>
  <div class="panel"><div class="label">Bearing</div><div class="stat">{bearing}</div></div>
  <div class="panel"><div class="label">Elevation</div><div class="stat">{elev}</div></div>
  <div class="panel"><div class="label">Duration</div>
    <div class="stat">{esc(duration(span))}</div></div>
  <div class="panel"><div class="label">Peak signal</div>
    <div class="stat">{f"{v['peak_rssi']:.1f}" if v['peak_rssi'] is not None else '-'}
    <span class="unit">dBFS</span></div></div>
</div>
<div class="panel mt">
<dl class="kv">
  <dt>Aircraft</dt><dd><a href="/aircraft/{esc(v['icao'])}">
    {esc(v['registration'] or v['icao'])}</a> &#183;
    <span class="mil">{esc(v['type_code'] or '?')}</span></dd>
  <dt>Description</dt><dd>{esc(v['description'] or '-')}</dd>
  <dt>Callsign</dt><dd>{esc(v['callsign'] or '-')}</dd>
  <dt>Started</dt><dd>{esc(stamp(v['started']))}</dd>
  <dt>Ended</dt><dd>{esc(stamp(v['ended']) if v['ended'] else 'in view now')}</dd>
  <dt>Alerted</dt><dd>{'sighting' if v['notified'] else 'no'}{
      ' + approach' if v['approached'] else ''}</dd>
</dl></div>
<h2>Ground track</h2><div class="panel">{track_svg(track)}</div>
<h2>Altitude</h2><div class="panel">{altitude_svg(track) or
    '<p class="empty">no altitude recorded</p>'}</div>"""
        return page(f"Pass {visit_id}", head)

    def api_visits(self) -> bytes:
        with self.connect() as db:
            rows = db.execute("""
                SELECT v.id, v.icao, v.callsign, v.started, v.ended, v.closest_nm,
                       v.closest_bearing, v.min_alt, v.max_alt, v.points,
                       a.registration, a.type_code, a.operator
                FROM visits v LEFT JOIN aircraft a USING (icao)
                ORDER BY v.started DESC LIMIT 200""").fetchall()
        return json.dumps([dict(r) for r in rows], indent=1).encode()


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "AirScope"
    sys_version = ""
    views: Views

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            body = self.route(path)
        except sqlite3.Error as exc:
            self._send(503, page("Unavailable", f'<p class="empty">database unavailable: '
                                                f'{esc(exc)}</p>'), "text/html; charset=utf-8")
            return
        if body is None:
            self._send(404, page("Not found", '<p class="empty">no such record</p>'),
                       "text/html; charset=utf-8")
            return
        ctype = "application/json" if path.startswith("/api/") else "text/html; charset=utf-8"
        if path == "/static/app.css":
            ctype = "text/css; charset=utf-8"
        self._send(200, body, ctype)

    def route(self, path: str) -> bytes | None:
        if path == "/":
            return self.views.overview()
        if path == "/static/app.css":
            return CSS.encode()
        if path == "/aircraft":
            return self.views.airframes()
        if path == "/api/visits":
            return self.views.api_visits()
        if path.startswith("/aircraft/"):
            icao = path[10:].lower()
            # Never interpolate request data into SQL or HTML unvalidated.
            if len(icao) == 6 and all(c in "0123456789abcdef" for c in icao):
                return self.views.aircraft(icao)
            return None
        if path.startswith("/visit/"):
            ident = path[7:]
            return self.views.visit(int(ident)) if ident.isdigit() else None
        return None

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main(argv=None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, _ = pre.parse_known_args(argv)
    cfg = load_config(find_config(known.config))

    p = argparse.ArgumentParser(description="Read-only web view of AirScope history.")
    p.add_argument("--config")
    p.add_argument("--database", type=Path,
                   default=Path(setting(cfg, "web", "database", "AIRSCOPE_DATABASE")
                                or setting(cfg, "airscope", "database", None)
                                or "/var/lib/airscope/airscope.db"))
    p.add_argument("--bind", default=setting(cfg, "web", "bind", "AIRSCOPE_WEB_BIND",
                                             "0.0.0.0:8080"),
                   help="host:port. Listens on all interfaces by default so "
                        "notification links open from a phone; there is no "
                        "authentication, so keep it off untrusted networks.")
    p.add_argument("-v", "--verbose", action="store_true", help="log requests")
    args = p.parse_args(argv)

    if not args.database.is_file():
        sys.exit(f"database not found: {args.database}")

    host, _, port = args.bind.rpartition(":")
    Handler.views = Views(args.database)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        verbose = args.verbose

    httpd = Server((host or "0.0.0.0", int(port)), Handler)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    print(f"airscope-web on http://{host or '127.0.0.1'}:{port} "
          f"reading {args.database} (read-only)", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except (Stopped, KeyboardInterrupt):
        pass
    httpd.server_close()
    print("stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
