#!/usr/bin/env python3
"""
airscope - poll a local tar1090 feed and fire an action the first time each
military aircraft comes into view.

This receiver's readsb has no aircraft database, so aircraft.json carries no
dbFlags, registration or type at all. The tar1090 web UI works around that by
looking each ICAO address up in a static database it serves itself, shaped as a
prefix trie of gzipped JSON files:

    <base>/<db-folder>/<prefix>.js
        -> {"<rest-of-hex>": [reg, type, flags, desc],
            "children": ["A0", "A1", ...]}

`flags` is a binary string, least-significant bit first: position 0 = military,
1 = interesting, 2 = PIA, 3 = LADD. tar1090 itself tests `data[2][0] == '1'`
for military (planeObject.js), which is what we reproduce here.

The lookup also backfills registration/type/description, which this feed lacks.
If readsb ever does grow a database, aircraft.json's own dbFlags bitmask is
preferred automatically.

Each visit produces up to two notifications, so nothing is missed while the
useful ones still stand out:
  * detection - the moment an aircraft is identified, wherever it is;
  * approach  - only for one predicted to pass within --overhead-radius, sent
                --lead-time seconds before closest approach, at higher priority.

Aircraft alerted in the same poll are combined into one notification. Nothing is
ever filtered out: geometry only affects wording and priority, and aircraft with
no position at all are still reported. Closest approach is a straight-line
projection from track and ground speed, so it is wrong the moment they turn; it
is re-evaluated every poll and must hold for --confirm-polls before firing.

A visit ends once an aircraft has been missing for --absence seconds, so a later
reappearance counts as new and notifies again.

Stdlib only, Python 3.8+. Runs as a systemd user service; see airscope.service.

Copyright (C) 2026 AirScope contributors. Licensed under the GNU General Public
License v3 or later; see LICENSE. This program comes with ABSOLUTELY NO WARRANTY.
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import json
import math
import os
import re
import signal
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

DB_MILITARY = 1
DB_INTERESTING = 2
DB_PIA = 4
DB_LADD = 8

PROWL_API = "https://api.prowlapp.com/publicapi/add"
NTFY_SERVER = "https://ntfy.sh"
ADSBDB_API = "https://api.adsbdb.com/v0/aircraft/"
PLANESPOTTERS_API = "https://api.planespotters.net/pub/photos/hex/"
USER_AGENT = "AirScope/1.0 (+https://github.com/mcherry/AirScope)"

FT_PER_NM = 6076.115
COMPASS = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


class Stopped(Exception):
    """Raised from the SIGTERM/SIGINT handler to break out of the poll loop."""


def _handle_stop(signum, frame):
    raise Stopped


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def _s(value) -> str:
    return "" if value is None else str(value)


def _f(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _f_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compass(degrees: float) -> str:
    return COMPASS[int((degrees % 360) / 22.5 + 0.5) % 16]


def duration(seconds: float) -> str:
    total = int(round(abs(seconds)))
    return f"{total // 60}m{total % 60:02d}s" if total >= 60 else f"{total}s"


def range_bearing(observer: tuple[float, float], target: tuple[float, float]) -> tuple[float, float]:
    """Great-circle range in nm and true bearing in degrees."""
    p1, p2 = math.radians(observer[0]), math.radians(target[0])
    dl = math.radians(target[1] - observer[1])
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    dist = 2 * 3440.065 * math.asin(math.sqrt(a))
    brg = math.degrees(math.atan2(
        math.sin(dl) * math.cos(p2),
        math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return dist, brg % 360


def closest_approach(dist_nm: float, bearing: float, track: float, gs_kt: float):
    """Straight-line CPA in a plane centred on the observer.

    Returns (seconds_to_cpa, miss_nm, bearing_at_cpa). Seconds is negative once
    the aircraft is already past its closest point.
    """
    b, t = math.radians(bearing), math.radians(track)
    px, py = dist_nm * math.sin(b), dist_nm * math.cos(b)
    vx, vy = gs_kt * math.sin(t), gs_kt * math.cos(t)
    speed_sq = vx * vx + vy * vy
    if speed_sq <= 0:
        return None
    hours = -(px * vx + py * vy) / speed_sq
    cx, cy = px + vx * hours, py + vy * hours
    return hours * 3600.0, math.hypot(cx, cy), math.degrees(math.atan2(cx, cy)) % 360


def sun_position(epoch: float, lat: float, lon: float) -> tuple[float, float]:
    """Low-precision solar azimuth and elevation in degrees (NOAA algorithm)."""
    n = epoch / 86400.0 + 2440587.5 - 2451545.0
    mean_long = math.radians((280.460 + 0.9856474 * n) % 360)
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic = mean_long + math.radians(1.915 * math.sin(anomaly) + 0.020 * math.sin(2 * anomaly))
    obliquity = math.radians(23.439 - 0.0000004 * n)

    right_asc = math.atan2(math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic))
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic))

    gmst = (18.697374558 + 24.06570982441908 * n) % 24
    hour_angle = math.radians((gmst * 15 + lon) % 360) - right_asc

    latr = math.radians(lat)
    elevation = math.asin(math.sin(latr) * math.sin(declination)
                          + math.cos(latr) * math.cos(declination) * math.cos(hour_angle))
    azimuth = math.atan2(
        -math.sin(hour_angle),
        math.tan(declination) * math.cos(latr) - math.sin(latr) * math.cos(hour_angle))
    return math.degrees(azimuth) % 360, math.degrees(elevation)


def lighting(sun_az: float, sun_el: float, target_bearing: float) -> str:
    if sun_el < -6:
        return "night"
    if sun_el < 0:
        return "twilight"
    offset = abs((sun_az - target_bearing + 180) % 360 - 180)
    if offset < 45:
        return "backlit"
    if offset > 135:
        return "front-lit"
    return "side-lit"


def geometry(ac: dict, observer: tuple[float, float] | None) -> dict | None:
    """Where it is now and where it will pass; None when position is unknown."""
    dist, bearing = _f(ac.get("r_dst")), _f(ac.get("r_dir"))
    if dist is None and observer and _f(ac.get("lat")) is not None:
        dist, bearing = range_bearing(observer, (ac["lat"], ac["lon"]))
    if dist is None or bearing is None:
        return None

    geo = {"dist_nm": dist, "bearing": bearing, "eta": None, "miss_nm": None,
           "alt_cpa": None, "elevation": None, "bearing_cpa": None, "light": ""}

    track, gs = _f(ac.get("track")), _f(ac.get("gs"))
    if track is None or gs is None or gs < 30:
        return geo
    result = closest_approach(dist, bearing, track, gs)
    if result is None:
        return geo

    eta, miss, bearing_cpa = result
    geo.update(eta=eta, miss_nm=miss, bearing_cpa=bearing_cpa)

    alt = _f(ac.get("alt_baro"))
    if alt is not None:
        alt_cpa = max(0.0, alt + (_f(ac.get("baro_rate")) or 0.0) * (eta / 60.0))
        geo["alt_cpa"] = alt_cpa
        geo["elevation"] = math.degrees(math.atan2(alt_cpa / FT_PER_NM, max(miss, 1e-6)))
    return geo


def add_lighting(geo: dict | None, observer: tuple[float, float] | None, when: float) -> None:
    if not geo or not observer:
        return
    bearing = geo["bearing_cpa"] if geo["bearing_cpa"] is not None else geo["bearing"]
    at = when + geo["eta"] if geo["eta"] and geo["eta"] > 0 else when
    sun_az, sun_el = sun_position(at, observer[0], observer[1])
    geo["light"] = lighting(sun_az, sun_el, bearing)
    geo["sun_elevation"] = sun_el


def http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip",
                                               "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        raw = resp.read()
        gzipped = resp.headers.get("Content-Encoding") == "gzip"
    if gzipped or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def fetch_json(url: str, timeout: float) -> dict:
    return json.loads(http_get(url, timeout))


def tar1090_base(url: str) -> str:
    for suffix in ("/data/aircraft.json", "/aircraft.json"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url.rsplit("/", 1)[0]


def service_dir(systemd_var: str, xdg_var: str, xdg_default: str) -> Path:
    """systemd's StateDirectory=/CacheDirectory= win, then XDG, then ~."""
    supplied = os.environ.get(systemd_var)
    if supplied:
        return Path(supplied.split(":")[0])
    base = os.environ.get(xdg_var)
    if not base:
        try:
            base = Path.home() / xdg_default
        except RuntimeError:  # no HOME and no passwd entry
            base = Path(".")
    return Path(base) / "airscope"


class AircraftDB:
    """tar1090's static aircraft database, walked as a prefix trie."""

    def __init__(self, base_url: str, folder_url: str | None, cache_dir: Path, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.folder_url = folder_url.rstrip("/") if folder_url else None
        self.pinned = bool(folder_url)
        self.cache_dir = cache_dir
        self.timeout = timeout
        self.blocks: dict[str, dict | None] = {}

    def discover(self) -> str:
        """The db folder name carries a content hash that changes on db updates."""
        if self.folder_url:
            return self.folder_url
        html = http_get(self.base_url + "/", self.timeout).decode("utf-8", "replace")
        match = re.search(r"\bdb-[0-9a-f]{4,}\b", html)
        if not match:
            raise RuntimeError(f"no aircraft database referenced by {self.base_url}/")
        self.folder_url = f"{self.base_url}/{match.group(0)}"
        return self.folder_url

    def _refresh_folder(self) -> bool:
        """Re-resolve the hashed folder after a 404; True if the database moved."""
        if self.pinned:
            return False
        previous, self.folder_url = self.folder_url, None
        try:
            current = self.discover()
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            log(f"WARN: could not re-discover aircraft database: {exc}")
            self.folder_url = previous
            return False
        if current == previous:
            return False
        log(f"aircraft database moved to {current}")
        self.blocks.clear()
        return True

    def _block(self, bkey: str) -> dict | None:
        if bkey in self.blocks:
            return self.blocks[bkey]

        try:
            folder = self.discover()
        except (urllib.error.URLError, OSError, RuntimeError) as exc:
            # Not cached: a receiver that is merely down should recover by itself.
            log(f"WARN: aircraft database unavailable: {exc}")
            return None

        path = self.cache_dir / folder.rsplit("/", 1)[-1] / f"{bkey}.json"
        data = None
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            try:
                raw = http_get(f"{folder}/{bkey}.js", self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and self._refresh_folder():
                    return self._block(bkey)
                if exc.code != 404:
                    log(f"WARN: db block {bkey}: {exc}")
            except (urllib.error.URLError, OSError) as exc:
                log(f"WARN: db block {bkey}: {exc}")
                return None
            else:
                try:
                    data = json.loads(raw)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                except ValueError as exc:
                    log(f"WARN: db block {bkey} is not JSON: {exc}")
                except OSError:
                    pass

        self.blocks[bkey] = data
        return data

    def lookup(self, icao: str) -> list | None:
        icao = icao.strip().upper()
        if not icao or icao.startswith("~"):
            return None
        level = 1
        while level <= len(icao):
            bkey, dkey = icao[:level], icao[level:]
            data = self._block(bkey)
            if not data:
                return None
            if dkey in data:
                record = data[dkey]
                return record if isinstance(record, list) else None
            if bkey + dkey[:1] in data.get("children", ()):
                level += 1
                continue
            return None
        return None


def military(ac: dict, db: AircraftDB | None) -> tuple[bool, list | None]:
    """Returns (is_military, db_record). Prefers readsb's dbFlags when present."""
    if "dbFlags" in ac:
        try:
            return bool(int(ac["dbFlags"]) & DB_MILITARY), None
        except (TypeError, ValueError):
            pass
    if db is None:
        return False, None
    record = db.lookup(_s(ac.get("hex")))
    flags = record[2] if record and len(record) > 2 else None
    return (isinstance(flags, str) and flags[:1] == "1"), record


def action_env(ac: dict, record: list | None, geo: dict | None = None) -> dict[str, str]:
    """Aircraft fields for the action, as environment variables."""
    record = record or []
    field = lambda i: _s(record[i]) if len(record) > i else ""  # noqa: E731
    env = {
        "HEX": _s(ac.get("hex")).strip().lower(),
        "FLIGHT": _s(ac.get("flight")).strip(),
        "REG": _s(ac.get("r")) or field(0),
        "TYPE": _s(ac.get("t")) or field(1),
        "DESC": _s(ac.get("desc")) or field(3),
        "OWNER": _s(ac.get("ownOp")),
        "ALT": _s(ac.get("alt_baro", ac.get("alt_geom"))),
        "GS": _s(ac.get("gs")),
        "SQUAWK": _s(ac.get("squawk")),
        "LAT": _s(ac.get("lat")),
        "LON": _s(ac.get("lon")),
        "RSSI": _s(ac.get("rssi")),
    }
    fmt = {"DIST_NM": "dist_nm", "BEARING": "bearing", "MISS_NM": "miss_nm",
           "ETA_S": "eta", "ELEVATION": "elevation", "ALT_CPA": "alt_cpa"}
    for name, key in fmt.items():
        value = geo.get(key) if geo else None
        env[name] = f"{value:.2f}" if isinstance(value, float) else ""
    env["LIGHT"] = geo.get("light", "") if geo else ""
    return env


DEFAULT_PROWL_KEY_FILE = "~/.prowl_api_key"


def read_secret(value: str | None) -> str | None:
    """A literal secret, or the path of a file holding one."""
    if not value:
        return None
    # A path that does not exist is a missing secret, not a literal one.
    looks_like_path = value.startswith("~") or "/" in value
    try:
        path = Path(value).expanduser()
        if path.is_file():
            return path.read_text().strip() or None
    except (OSError, ValueError, RuntimeError):
        pass
    return None if looks_like_path else (value.strip() or None)


def _strip_tags(xml: str) -> str:
    return " ".join(re.sub(r"<[^>]*>", " ", xml).split()) or "empty response"


def post(url: str, data: bytes, headers: dict, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        return resp.status, resp.read().decode("utf-8", "replace")


class Notifier:
    """Priority is a normalised -2..2; each backend maps it to its own scale."""

    name = "notifier"

    def send(self, event: str, body: str, link: str, priority: int) -> bool:
        raise NotImplementedError

    def _fail(self, detail: str) -> bool:
        log(f"WARN: {self.name}: {detail}")
        return False


class ProwlNotifier(Notifier):
    name = "prowl"

    def __init__(self, key: str, application: str, api_url: str, timeout: float):
        self.key, self.application = key, application[:256]
        self.api_url, self.timeout = api_url, timeout

    def send(self, event: str, body: str, link: str, priority: int) -> bool:
        payload = urllib.parse.urlencode({
            "apikey": self.key,
            "application": self.application,
            "event": event[:1024],
            "description": body[:10000],
            "priority": max(-2, min(2, priority)),
            "url": link[:512],
        }).encode()
        try:
            _, text = post(self.api_url, payload,
                           {"Content-Type": "application/x-www-form-urlencoded"}, self.timeout)
        except urllib.error.HTTPError as exc:
            return self._fail(f"rejected ({exc.code}): "
                              f"{_strip_tags(exc.read().decode('utf-8', 'replace'))}")
        except (urllib.error.URLError, OSError) as exc:
            return self._fail(f"unreachable: {exc}")
        if "success" not in text.lower():
            return self._fail(f"error: {_strip_tags(text)}")
        return True


class NtfyNotifier(Notifier):
    name = "ntfy"
    LEVELS = {-2: "1", -1: "2", 0: "3", 1: "4", 2: "5"}

    def __init__(self, server: str, topic: str, token: str | None, timeout: float):
        self.url = f"{server.rstrip('/')}/{topic.lstrip('/')}"
        self.token, self.timeout = token, timeout

    def send(self, event: str, body: str, link: str, priority: int) -> bool:
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            # Header values must be latin-1 safe, and titles cannot contain newlines.
            "Title": event.encode("ascii", "replace").decode()[:250],
            "Priority": self.LEVELS[max(-2, min(2, priority))],
            "Tags": "airplane",
        }
        if link:
            headers["Click"] = link
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            post(self.url, body.encode("utf-8"), headers, self.timeout)
        except urllib.error.HTTPError as exc:
            return self._fail(f"rejected ({exc.code}): "
                              f"{exc.read().decode('utf-8', 'replace')[:200]}")
        except (urllib.error.URLError, OSError) as exc:
            return self._fail(f"unreachable: {exc}")
        return True


class WebhookNotifier(Notifier):
    name = "webhook"

    def __init__(self, url: str, token: str | None, timeout: float):
        self.url, self.token, self.timeout = url, token, timeout

    def send(self, event: str, body: str, link: str, priority: int) -> bool:
        payload = json.dumps({"event": event, "body": body,
                              "link": link, "priority": priority}).encode()
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            post(self.url, payload, headers, self.timeout)
        except urllib.error.HTTPError as exc:
            return self._fail(f"rejected ({exc.code})")
        except (urllib.error.URLError, OSError) as exc:
            return self._fail(f"unreachable: {exc}")
        return True


def label(ac: dict, record: list | None) -> str:
    f = action_env(ac, record)
    return " ".join(x for x in (f["TYPE"], f["FLIGHT"]) if x) or f["HEX"]


def altitude_phrase(alt: str) -> str:
    if not alt:
        return ""
    return f"{alt} ft" if alt.lstrip("-").isdigit() else alt


def where_now(geo: dict | None, alt: str) -> str:
    if geo is None:
        return " ".join(x for x in ("position unknown", altitude_phrase(alt)) if x)
    text = f"{geo['dist_nm']:.0f} nm {compass(geo['bearing'])}"
    if alt:
        text += f" at {altitude_phrase(alt)}"
    return text


def where_next(geo: dict | None) -> str:
    """The predicted pass, or empty when it cannot be predicted."""
    if not geo or geo["eta"] is None:
        return ""
    if geo["eta"] <= 0:
        return "outbound"
    parts = [f"closest {geo['miss_nm']:.1f} nm in {duration(geo['eta'])}"]
    if geo["miss_nm"] < 0.5:
        # Bearing to a point directly above you is numerically meaningless.
        parts.append("directly overhead")
    else:
        if geo["bearing_cpa"] is not None:
            parts.append(f"look {geo['bearing_cpa']:03.0f} {compass(geo['bearing_cpa'])}")
        if geo["elevation"] is not None:
            parts.append(f"{geo['elevation']:.0f} deg up")
    return ", ".join(parts)


def summary_line(sighting) -> str:
    ac, record, geo = sighting
    f = action_env(ac, record, geo)
    bits = [label(ac, record), where_now(geo, f["ALT"])]
    nxt = where_next(geo)
    if nxt:
        bits.append(nxt)
    return " - ".join(bits)


def receiver_link(args, hexid: str = "") -> str:
    """The receiver's own live map, which is the useful one mid-flight."""
    base = tar1090_base(args.url)
    if not base.startswith("http"):
        return ""
    return f"{base}/?icao={hexid}" if hexid else base


def web_link(args, hexid: str = "") -> str:
    """The AirScope history page for this airframe."""
    if not args.web_url:
        return ""
    base = args.web_url.rstrip("/")
    return f"{base}/aircraft/{hexid}" if hexid else base


def link_lines(args, hexid: str = "") -> list[str]:
    """Prowl only surfaces its url parameter inside the app, so the URLs go in
    the body too, where every client shows them."""
    lines = []
    live = receiver_link(args, hexid)
    history = web_link(args, hexid)
    if live:
        lines.append(f"Live: {live}")
    if history:
        lines.append(f"History: {history}")
    return lines


def primary_link(args, hexid: str = "") -> str:
    if args.link_target == "web":
        return web_link(args, hexid) or receiver_link(args, hexid)
    return receiver_link(args, hexid) or web_link(args, hexid)


def compose_message(kind: str, sightings: list, args) -> tuple[str, str, str]:
    if len(sightings) > 1:
        noun = "approaching overhead" if kind == "approach" else "military aircraft"
        event = f"{len(sightings)} {noun}"
        lines = [summary_line(s) for s in sightings]
        lines += link_lines(args)
        return event, "\n".join(lines), primary_link(args)

    ac, record, geo = sightings[0]
    f = action_env(ac, record, geo)
    name = label(ac, record)
    event = f"{name} overhead in {duration(geo['eta'])}" if kind == "approach" else name

    lines = [x for x in (f["DESC"], f"Reg {f['REG']}" if f["REG"] else "") if x]
    lines.append(where_now(geo, f["ALT"]))
    nxt = where_next(geo)
    if nxt:
        lines.append(nxt)
    if geo and geo.get("light"):
        lines.append(geo["light"])
    lines.append(f"ICAO {f['HEX']}")
    lines += link_lines(args, f["HEX"])
    return event, "\n".join(lines), primary_link(args, f["HEX"])


def message_priority(kind: str, sightings: list, args) -> int:
    if kind == "approach":
        return args.approach_priority
    closest = [s[2]["miss_nm"] for s in sightings
               if s[2] and s[2]["miss_nm"] is not None and s[2]["eta"] and s[2]["eta"] > 0]
    if closest and min(closest) <= args.overhead_radius:
        return min(2, args.priority + 1)
    return args.priority


def run_action(command: str, ac: dict, record: list | None, geo: dict | None) -> None:
    # Aircraft data goes in as environment variables rather than being
    # interpolated into the command string: it is external input either way.
    env = {**os.environ, **action_env(ac, record, geo)}
    try:
        result = subprocess.run(command, shell=True, env=env, check=False)
    except OSError as exc:
        log(f"WARN: action failed for {ac.get('hex')}: {exc}")
        return
    if result.returncode != 0:
        log(f"WARN: action exited {result.returncode} for {ac.get('hex')}")


def notify(kind: str, sightings: list, args, notifiers: list) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    tag = "APPROACH" if kind == "approach" else "MIL"
    for sighting in sightings:
        print(f"{stamp}  {tag} {summary_line(sighting)}", flush=True)
    if notifiers:
        event, body, link = compose_message(kind, sightings, args)
        priority = message_priority(kind, sightings, args)
        for notifier in notifiers:
            notifier.send(event, body, link, priority)
    if args.action:
        for ac, record, geo in sightings:
            run_action(args.action, ac, record, geo)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS aircraft (
  icao         TEXT PRIMARY KEY,
  registration TEXT,
  type_code    TEXT,
  description  TEXT,
  operator     TEXT,
  db_flags     TEXT,
  photo_thumb  TEXT,
  photo_link   TEXT,
  photographer TEXT,
  enriched_at  INTEGER,
  first_seen   INTEGER NOT NULL,
  last_seen    INTEGER NOT NULL,
  visit_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS visits (
  id           INTEGER PRIMARY KEY,
  icao         TEXT NOT NULL,
  callsign     TEXT,
  started      INTEGER NOT NULL,
  last_seen    INTEGER NOT NULL,
  ended        INTEGER,
  closest_nm   REAL,
  closest_at   INTEGER,
  closest_bearing REAL,
  closest_elevation REAL,
  min_alt      INTEGER,
  max_alt      INTEGER,
  max_gs       REAL,
  peak_rssi    REAL,
  points       INTEGER NOT NULL DEFAULT 0,
  notified     INTEGER NOT NULL DEFAULT 0,
  approached   INTEGER NOT NULL DEFAULT 0,
  qualified    INTEGER NOT NULL DEFAULT 0,
  track        BLOB
);
CREATE INDEX IF NOT EXISTS visits_icao ON visits(icao);
CREATE INDEX IF NOT EXISTS visits_started ON visits(started DESC);
"""

# offset seconds, lat, lon, alt/25, ground speed, track*10, range*10
TRACK_POINT = struct.Struct("<IffhHHH")


def encode_track(points: list) -> bytes:
    raw = b"".join(TRACK_POINT.pack(*p) for p in points)
    return zlib.compress(raw, 6)


def decode_points(blob: bytes) -> list[tuple]:
    """Packed tuples, as the poller keeps them for re-encoding."""
    if not blob:
        return []
    try:
        return list(TRACK_POINT.iter_unpack(zlib.decompress(blob)))
    except zlib.error:
        return []


def decode_track(blob: bytes, started: int) -> list[dict]:
    """Readable form, for the web UI."""
    return [{"t": started + offset, "lat": round(lat, 5), "lon": round(lon, 5),
             "alt": alt * 25, "gs": gs, "track": trk / 10.0, "dist": dist / 10.0}
            for offset, lat, lon, alt, gs, trk, dist in decode_points(blob)]


def track_point(offset: int, ac: dict, geo: dict | None) -> tuple:
    alt = _f(ac.get("alt_baro"))
    return (max(0, int(offset)),
            _f(ac.get("lat")) or 0.0,
            _f(ac.get("lon")) or 0.0,
            int(max(-32000, min(32000, (alt or 0) / 25))),
            int(max(0, min(65535, _f(ac.get("gs")) or 0))),
            int(max(0, min(65535, (_f(ac.get("track")) or 0) * 10))),
            int(max(0, min(65535, (geo["dist_nm"] if geo else 0) * 10))))


class Store:
    """Visit history. The poller writes; the web UI opens the same file read-only."""

    def __init__(self, path: Path, readonly: bool = False):
        self.path = path
        if not readonly:
            path.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{urllib.parse.quote(str(path))}" + ("?mode=ro" if readonly else "")
        self.db = sqlite3.connect(uri, uri=True, timeout=10.0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if not readonly:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) "
                        "DO UPDATE SET value = excluded.value", (key, str(value)))

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def open_visits(self, cutoff: float) -> dict[str, dict]:
        """Resume in-flight visits so a restart does not re-alert everything."""
        rows = self.db.execute(
            "SELECT * FROM visits WHERE ended IS NULL AND last_seen >= ?", (int(cutoff),))
        state = {}
        for row in rows:
            state[row["icao"]] = {
                "visit_id": row["id"], "first_seen": row["started"],
                "last_seen": row["last_seen"], "announced": bool(row["notified"]),
                "approached": bool(row["approached"]), "qualified": row["qualified"],
                "points": decode_points(row["track"]),
            }
        self.db.execute("UPDATE visits SET ended = last_seen "
                        "WHERE ended IS NULL AND last_seen < ?", (int(cutoff),))
        return state

    def upsert_aircraft(self, icao: str, record: list | None, now: float) -> None:
        reg, typ, flags, desc = ((record + [None] * 4)[:4] if record else [None] * 4)
        self.db.execute("""
            INSERT INTO aircraft (icao, registration, type_code, db_flags, description,
                                  first_seen, last_seen, visit_count)
            VALUES (?,?,?,?,?,?,?,0)
            ON CONFLICT(icao) DO UPDATE SET
              last_seen = excluded.last_seen,
              registration = COALESCE(excluded.registration, aircraft.registration),
              type_code = COALESCE(excluded.type_code, aircraft.type_code),
              db_flags = COALESCE(excluded.db_flags, aircraft.db_flags),
              description = COALESCE(excluded.description, aircraft.description)
        """, (icao, reg, typ, flags, desc, int(now), int(now)))

    def begin_visit(self, icao: str, now: float, callsign: str) -> int:
        cur = self.db.execute(
            "INSERT INTO visits (icao, callsign, started, last_seen) VALUES (?,?,?,?)",
            (icao, callsign or None, int(now), int(now)))
        self.db.execute("UPDATE aircraft SET visit_count = visit_count + 1 WHERE icao = ?",
                        (icao,))
        return cur.lastrowid

    def record(self, entry: dict, now: float, ac: dict, geo: dict | None) -> None:
        alt = _f(ac.get("alt_baro"))
        gs = _f(ac.get("gs"))
        rssi = _f(ac.get("rssi"))
        if geo:
            entry["points"].append(track_point(now - entry["first_seen"], ac, geo))
        self.db.execute("""
            UPDATE visits SET
              last_seen = ?, points = ?, track = ?,
              callsign = COALESCE(?, callsign),
              min_alt = CASE WHEN min_alt IS NULL OR ? < min_alt THEN ? ELSE min_alt END,
              max_alt = CASE WHEN max_alt IS NULL OR ? > max_alt THEN ? ELSE max_alt END,
              max_gs  = CASE WHEN max_gs  IS NULL OR ? > max_gs  THEN ? ELSE max_gs  END,
              peak_rssi = CASE WHEN peak_rssi IS NULL OR ? > peak_rssi THEN ? ELSE peak_rssi END
            WHERE id = ?
        """, (int(now), len(entry["points"]), encode_track(entry["points"]),
              _s(ac.get("flight")).strip() or None,
              alt, alt, alt, alt, gs, gs, rssi, rssi, entry["visit_id"]))

        if geo and geo.get("dist_nm") is not None:
            self.db.execute("""
                UPDATE visits SET closest_nm = ?, closest_at = ?, closest_bearing = ?,
                                  closest_elevation = ?
                WHERE id = ? AND (closest_nm IS NULL OR ? < closest_nm)
            """, (geo["dist_nm"], int(now), geo["bearing"], geo.get("elevation"),
                  entry["visit_id"], geo["dist_nm"]))

    def set_flags(self, entry: dict) -> None:
        self.db.execute(
            "UPDATE visits SET notified = ?, approached = ?, qualified = ? WHERE id = ?",
            (int(entry["announced"]), int(entry["approached"]),
             entry["qualified"], entry["visit_id"]))

    def close_visit(self, entry: dict) -> None:
        self.db.execute("UPDATE visits SET ended = last_seen WHERE id = ?",
                        (entry["visit_id"],))

    def unenriched(self, limit: int = 3) -> list[str]:
        rows = self.db.execute(
            "SELECT icao FROM aircraft WHERE enriched_at IS NULL LIMIT ?", (limit,))
        return [r["icao"] for r in rows]

    def save_enrichment(self, icao: str, data: dict, now: float) -> None:
        self.db.execute("""
            UPDATE aircraft SET
              enriched_at = ?,
              operator = COALESCE(?, operator),
              description = COALESCE(?, description),
              registration = COALESCE(registration, ?),
              type_code = COALESCE(type_code, ?),
              photo_thumb = ?, photo_link = ?, photographer = ?
            WHERE icao = ?
        """, (int(now), data.get("operator"), data.get("description"),
              data.get("registration"), data.get("type_code"), data.get("photo_thumb"),
              data.get("photo_link"), data.get("photographer"), icao))


def enrich_aircraft(icao: str, timeout: float) -> dict:
    """Operator and a photo of this exact airframe. Enrichment only -- a failure
    here must never affect alerting, so every error is swallowed."""
    out: dict = {}
    try:
        payload = fetch_json(ADSBDB_API + icao, timeout)
        ac = (payload.get("response") or {}).get("aircraft") or {}
        out["operator"] = ac.get("registered_owner") or None
        out["description"] = ac.get("type") or None
        out["registration"] = ac.get("registration") or None
        out["type_code"] = ac.get("icao_type") or None
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        payload = fetch_json(PLANESPOTTERS_API + icao, timeout)
        photos = payload.get("photos") or []
        if photos:
            photo = photos[0]
            thumb = photo.get("thumbnail_large") or photo.get("thumbnail") or {}
            out["photo_thumb"] = thumb.get("src")
            out["photo_link"] = photo.get("link")
            out["photographer"] = photo.get("photographer")
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return out


def get_observer(args) -> tuple[float, float] | None:
    if args.lat is not None and args.lon is not None:
        return (args.lat, args.lon)
    url = tar1090_base(args.url) + "/data/receiver.json"
    try:
        data = fetch_json(url, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"WARN: no receiver location ({exc}); sun angles disabled")
        return None
    lat, lon = _f(data.get("lat")), _f(data.get("lon"))
    if lat is None or lon is None:
        log("WARN: receiver.json has no position; sun angles disabled")
        return None
    return (lat, lon)


def poll_once(args, db: AircraftDB | None, notifiers: list, store: Store,
              state: dict[str, dict], observer: tuple[float, float] | None) -> None:
    try:
        payload = fetch_json(args.url, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"WARN: fetch failed for {args.url}: {exc}")
        return

    now = time.time()
    detections: list = []
    approaches: list = []
    seen = 0

    for ac in payload.get("aircraft", []):
        is_mil, record = military(ac, db)
        if not is_mil:
            continue
        hexid = _s(ac.get("hex")).strip().lower()
        if not hexid:
            continue
        seen += 1

        geo = geometry(ac, observer)
        entry = state.get(hexid)
        store.upsert_aircraft(hexid, record, now)
        if entry is None or now - entry.get("last_seen", 0) > args.absence:
            if entry is not None:
                store.close_visit(entry)
            entry = {"first_seen": now, "last_seen": now, "announced": False,
                     "approached": False, "qualified": 0, "points": [],
                     "visit_id": store.begin_visit(hexid, now, _s(ac.get("flight")).strip())}
            state[hexid] = entry
        entry["last_seen"] = now
        store.record(entry, now, ac, geo)

        if not entry["announced"]:
            # A distant target is heard well before its position decodes, so
            # alerting on the first poll usually means alerting without one.
            # Some aircraft never report a position, hence the cap.
            if geo is None and now - entry["first_seen"] < args.position_grace:
                store.set_flags(entry)
                continue
            entry["announced"] = True
            detections.append((ac, record, geo))

        if not entry["approached"] and geo and geo["eta"] is not None:
            inbound = (geo["miss_nm"] <= args.overhead_radius
                       and args.min_lead <= geo["eta"] <= args.lead_time)
            entry["qualified"] = entry["qualified"] + 1 if inbound else 0
            if entry["qualified"] >= args.confirm_polls:
                entry["approached"] = True
                approaches.append((ac, record, geo))
        store.set_flags(entry)

    # An approach alert supersedes a first-sighting alert for the same aircraft.
    if approaches:
        pending = {_s(a[0].get("hex")).strip().lower() for a in approaches}
        detections = [d for d in detections
                      if _s(d[0].get("hex")).strip().lower() not in pending]

    for kind, group in (("detected", detections), ("approach", approaches)):
        if not group:
            continue
        for _, _, geo in group:
            add_lighting(geo, observer, now)
        notify(kind, group, args, notifiers)

    for hexid in [h for h, v in state.items() if now - v.get("last_seen", 0) > args.absence]:
        store.close_visit(state.pop(hexid))

    if not args.no_enrich:
        for icao in store.unenriched():
            store.save_enrichment(icao, enrich_aircraft(icao, args.timeout), now)

    if args.verbose:
        log(f"{seen} military in view, {len(state)} tracked")


def check(args, db: AircraftDB) -> int:
    print(f"feed: {args.url}")
    try:
        payload = fetch_json(args.url, args.timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"FAIL: could not fetch feed: {exc}")
        return 1

    aircraft = payload.get("aircraft", [])
    print(f"  aircraft in feed : {len(aircraft)}")
    print(f"  carrying dbFlags : {sum(1 for a in aircraft if 'dbFlags' in a)}")
    print(f"  carrying reg     : {sum(1 for a in aircraft if a.get('r'))}")

    try:
        folder = db.discover()
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        print(f"\nFAIL: could not locate tar1090's aircraft database: {exc}")
        print("Pass --db-url explicitly if the UI lives somewhere unusual.")
        return 1

    print(f"\ndatabase: {folder}")
    observer = get_observer(args)
    resolved, hits = 0, []
    for ac in aircraft:
        is_mil, record = military(ac, db)
        if record or "dbFlags" in ac:
            resolved += 1
        if is_mil:
            geo = geometry(ac, observer)
            add_lighting(geo, observer, time.time())
            hits.append(summary_line((ac, record, geo)))

    print(f"  resolved         : {resolved}/{len(aircraft)}")
    print(f"  with position    : {sum(1 for a in aircraft if a.get('lat') is not None)}")
    print(f"  military         : {len(hits)}")
    for row in hits:
        print(f"    MIL {row}")

    if observer:
        sun_az, sun_el = sun_position(time.time(), observer[0], observer[1])
        print(f"\nobserver: {observer[0]:.5f}, {observer[1]:.5f}")
        print(f"  sun            : {sun_az:.0f} deg {compass(sun_az)}, {sun_el:+.0f} deg elevation")

    if not aircraft:
        print("\nNothing in view at all -- check the receiver.")
    elif not resolved:
        print("\nNo aircraft resolved against the database. Detection will not work.")
        return 1
    return 0


CONFIG_SECTION = "airscope"


def find_config(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"config file not found: {explicit}")
        return path

    candidates = []
    if os.environ.get("AIRSCOPE_CONFIG"):
        candidates.append(Path(os.environ["AIRSCOPE_CONFIG"]).expanduser())
    base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        try:
            base = Path.home() / ".config"
        except RuntimeError:
            base = None
    if base:
        candidates.append(Path(base) / "airscope.conf")
    candidates.append(Path("/etc/airscope.conf"))

    for path in candidates:
        try:
            if path.is_file():
                return path
        except (OSError, ValueError):
            continue
    return None


def load_config(path: Path | None) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if path:
        try:
            config.read(path)
        except (OSError, configparser.Error) as exc:
            raise SystemExit(f"cannot read {path}: {exc}")
    return config


def setting(config, section: str, key: str, envvar: str | None, default=None):
    """Resolution order below the command line: env var, config file, default."""
    value = os.environ.get(envvar) if envvar else None
    if value is None and config.has_option(section, key):
        value = config.get(section, key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, _ = pre.parse_known_args(argv)
    config_path = find_config(known.config)
    cfg = load_config(config_path)

    def main_setting(key, envvar, default=None):
        return setting(cfg, CONFIG_SECTION, key, envvar, default)

    p = argparse.ArgumentParser(
        description="Alert when a military aircraft comes into view, and again "
                    "when one is predicted to pass overhead.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--config", default=str(config_path) if config_path else None,
                   help="config file; searched at $AIRSCOPE_CONFIG, "
                        "$XDG_CONFIG_HOME/airscope.conf, /etc/airscope.conf")
    p.add_argument(
        "--url", default=main_setting("url", "AIRSCOPE_URL",
                                     "http://localhost/tar1090/data/aircraft.json"),
        help="tar1090 aircraft.json URL",
    )
    p.add_argument(
        "--action", default=main_setting("action", "AIRSCOPE_ACTION"),
        help="shell command to run per sighting; aircraft fields arrive as "
        "$HEX $FLIGHT $REG $TYPE $DESC $OWNER $ALT $GS $SQUAWK $LAT $LON $RSSI "
        "$DIST_NM $BEARING $MISS_NM $ETA_S $ELEVATION $ALT_CPA $LIGHT.",
    )
    p.add_argument(
        "--interval", type=float, default=float(main_setting("interval", "AIRSCOPE_INTERVAL", "10")),
        help="seconds between polls",
    )
    p.add_argument(
        "--overhead-radius", type=float,
        default=float(main_setting("overhead_radius", "AIRSCOPE_OVERHEAD_RADIUS", "5")),
        help="nm; a predicted pass within this raises priority and arms the approach alert",
    )
    p.add_argument(
        "--lead-time", type=float,
        default=float(main_setting("lead_time", "AIRSCOPE_LEAD_TIME", "240")),
        help="seconds before closest approach to send the approach alert",
    )
    p.add_argument(
        "--min-lead", type=float,
        default=float(main_setting("min_lead", "AIRSCOPE_MIN_LEAD", "30")),
        help="seconds; below this there is no time to react, so do not alert",
    )
    p.add_argument(
        "--confirm-polls", type=int,
        default=int(main_setting("confirm_polls", "AIRSCOPE_CONFIRM_POLLS", "2")),
        help="consecutive polls a prediction must hold before the approach alert fires",
    )
    p.add_argument(
        "--position-grace", type=float,
        default=float(main_setting("position_grace", "AIRSCOPE_POSITION_GRACE", "150")),
        help="seconds to wait for a position to decode before alerting without "
             "one; 0 alerts immediately",
    )
    p.add_argument("--lat", type=float,
                   default=_f_or_none(main_setting("lat", "AIRSCOPE_LAT")),
                   help="observer latitude; taken from receiver.json when unset")
    p.add_argument("--lon", type=float,
                   default=_f_or_none(main_setting("lon", "AIRSCOPE_LON")),
                   help="observer longitude; taken from receiver.json when unset")
    p.add_argument(
        "--absence", type=float,
        default=float(main_setting("absence", "AIRSCOPE_ABSENCE", "600")),
        help="seconds an aircraft must be missing before a reappearance counts "
        "as a new visit; also absorbs signal dropouts",
    )
    p.add_argument(
        "--database", type=Path,
        default=Path(main_setting("database", "AIRSCOPE_DATABASE") or
                     service_dir("STATE_DIRECTORY", "XDG_STATE_HOME", ".local/state") / "airscope.db"),
        help="SQLite file holding visit history and dedup state",
    )
    p.add_argument(
        "--web-url", default=main_setting("web_url", "AIRSCOPE_WEB_URL"),
        help="base URL of the AirScope web UI; adds a history link to notifications",
    )
    p.add_argument(
        "--link-target", choices=("tar1090", "web"),
        default=main_setting("link_target", "AIRSCOPE_LINK_TARGET", "tar1090"),
        help="which URL notification clients open when tapped; both appear in "
             "the message body either way",
    )
    p.add_argument(
        "--db-url", default=main_setting("db_url", "AIRSCOPE_DB_URL"),
        help="URL of the folder holding the database trie files; "
        "auto-discovered from the tar1090 page when unset",
    )
    p.add_argument(
        "--cache", type=Path,
        default=Path(main_setting("cache", "AIRSCOPE_CACHE") or
                     service_dir("CACHE_DIRECTORY", "XDG_CACHE_HOME", ".cache") / "db"),
        help="where database blocks are cached on disk",
    )
    p.add_argument(
        "--notifiers", default=main_setting("notifiers", "AIRSCOPE_NOTIFIERS", "prowl"),
        help="comma-separated: prowl, ntfy, webhook",
    )
    p.add_argument("--priority", type=int, choices=range(-2, 3),
                   default=int(main_setting("priority", "AIRSCOPE_PRIORITY", "0")),
                   help="-2 very low, 0 normal, 2 emergency")
    p.add_argument("--approach-priority", type=int, choices=range(-2, 3),
                   default=int(main_setting("approach_priority", "AIRSCOPE_APPROACH_PRIORITY", "1")),
                   help="priority for the overhead approach alert")

    p.add_argument("--prowl-key",
                   default=setting(cfg, "prowl", "key", "AIRSCOPE_PROWL_KEY", DEFAULT_PROWL_KEY_FILE),
                   help="Prowl API key, or a file containing one")
    p.add_argument("--prowl-app",
                   default=setting(cfg, "prowl", "application", "AIRSCOPE_PROWL_APP", "AirScope"),
                   help="application name shown in the notification")
    p.add_argument("--prowl-url",
                   default=setting(cfg, "prowl", "api_url", "AIRSCOPE_PROWL_URL", PROWL_API),
                   help="Prowl API endpoint")
    p.add_argument("--ntfy-server",
                   default=setting(cfg, "ntfy", "server", "AIRSCOPE_NTFY_SERVER", NTFY_SERVER),
                   help="ntfy server base URL")
    p.add_argument("--ntfy-topic",
                   default=setting(cfg, "ntfy", "topic", "AIRSCOPE_NTFY_TOPIC"),
                   help="ntfy topic to publish to")
    p.add_argument("--ntfy-token",
                   default=setting(cfg, "ntfy", "token", "AIRSCOPE_NTFY_TOKEN"),
                   help="ntfy access token, or a file containing one")
    p.add_argument("--webhook-url",
                   default=setting(cfg, "webhook", "url", "AIRSCOPE_WEBHOOK_URL"),
                   help="URL to POST notification JSON to")
    p.add_argument("--webhook-token",
                   default=setting(cfg, "webhook", "token", "AIRSCOPE_WEBHOOK_TOKEN"),
                   help="bearer token for the webhook, or a file containing one")

    p.add_argument("--no-notify", action="store_true", help="suppress all notifiers")
    p.add_argument("--no-enrich", action="store_true",
                   help="do not look up operator and photos from public APIs")
    p.add_argument("--test-notify", action="store_true",
                   help="send one test notification to each configured notifier and exit")
    p.add_argument("--print-config", action="store_true",
                   help="show effective settings (secrets redacted) and exit")
    p.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    p.add_argument("--once", action="store_true", help="single pass, then exit (for cron)")
    p.add_argument("--check", action="store_true", help="resolve everything in view and report")
    p.add_argument("-v", "--verbose", action="store_true", help="log every poll")
    return p.parse_args(argv)


SECRET_ARGS = ("prowl_key", "ntfy_token", "webhook_token")


def print_config(args) -> int:
    print(f"config file : {args.config or '(none found)'}")
    for key in sorted(vars(args)):
        if key == "config":
            continue
        value = getattr(args, key)
        if key in SECRET_ARGS:
            resolved = read_secret(value)
            value = f"(set, {len(resolved)} chars)" if resolved else "(unset)"
        print(f"  {key:18} = {value}")
    return 0


def build_notifiers(args) -> list:
    if args.no_notify:
        return []
    built = []
    for name in [n.strip().lower() for n in (args.notifiers or "").split(",") if n.strip()]:
        if name == "prowl":
            key = read_secret(args.prowl_key)
            if not key:
                log("WARN: prowl: no API key (--prowl-key, $AIRSCOPE_PROWL_KEY or "
                    f"{DEFAULT_PROWL_KEY_FILE}); skipped")
                continue
            built.append(ProwlNotifier(key, args.prowl_app, args.prowl_url, args.timeout))
        elif name == "ntfy":
            if not args.ntfy_topic:
                log("WARN: ntfy: no topic configured; skipped")
                continue
            built.append(NtfyNotifier(args.ntfy_server, args.ntfy_topic,
                                      read_secret(args.ntfy_token), args.timeout))
        elif name == "webhook":
            if not args.webhook_url:
                log("WARN: webhook: no url configured; skipped")
                continue
            built.append(WebhookNotifier(args.webhook_url,
                                         read_secret(args.webhook_token), args.timeout))
        else:
            log(f"WARN: unknown notifier '{name}'")
    return built


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.print_config:
        return print_config(args)

    db = AircraftDB(tar1090_base(args.url), args.db_url, args.cache, args.timeout)

    if args.check:
        return check(args, db)

    notifiers = build_notifiers(args)

    if args.test_notify:
        if not notifiers:
            print("No notifiers configured; nothing to test.")
            return 1
        body = "\n".join(["AirScope can reach this notifier.",
                          "Links below use a sample aircraft:"]
                         + link_lines(args, "ae6044"))
        results = [(n.name, n.send("AirScope test", body, primary_link(args, "ae6044"), 0))
                   for n in notifiers]
        for name, ok in results:
            print(f"{name}: {'sent' if ok else 'FAILED'}")
        return 0 if all(ok for _, ok in results) else 1

    if not notifiers and not args.action:
        log("WARN: no notifiers and no --action; sightings will only be logged")

    observer = get_observer(args)
    store = Store(args.database)
    if observer:
        store.set_meta("observer_lat", observer[0])
        store.set_meta("observer_lon", observer[1])
    state = store.open_visits(time.time() - args.absence)
    if state:
        log(f"resumed {len(state)} in-flight visit(s) from {args.database}")

    if args.once:
        poll_once(args, db, notifiers, store, state, observer)
        store.close()
        return 0

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    active = ",".join(n.name for n in notifiers) or "none"
    log(f"polling {args.url} every {args.interval:g}s "
        f"(visit closes after {args.absence:g}s absent, notifiers {active})")
    if observer:
        log(f"observer {observer[0]:.5f},{observer[1]:.5f}; "
            f"approach alert at {args.lead_time:g}s for passes within "
            f"{args.overhead_radius:g} nm")
    try:
        while True:
            poll_once(args, db, notifiers, store, state, observer)
            time.sleep(args.interval)
    except (Stopped, KeyboardInterrupt):
        pass
    store.close()
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
