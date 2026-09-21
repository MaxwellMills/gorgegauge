#!/usr/bin/env python3
"""
Build the data behind /pulse.html — "The Gorge's River Pulse".

Two files:

  pulse/rivers.json   Geometry and long-term statistics for each gauged river.
                      Changes only when the gauge list changes. Built from the
                      USGS NLDI network (NHDPlus flowlines) and the USGS daily
                      statistics service.

  pulse/flows.json    One value per river per day since PULSE_START (five
                      water years back by default): mean daily discharge (cfs)
                      and mean daily water temperature (°C). Built from USGS
                      NWIS daily values, or from instantaneous values averaged
                      per day where a gauge publishes nothing else.

Both are written to site/pulse/ for local preview and, when PULSE_UPLOAD is
set, uploaded to the gorgegauge.com bucket (the site deploy excludes *.json,
so data files always travel this way).

Daily refresh: with an existing flows.json (local, or in the bucket when
uploading) the builder fetches only the last REFRESH_DAYS days and merges
them in, so the history is appended to rather than rebuilt. USGS revises
provisional values for a few weeks, which is why the window is generous.
PULSE_FULL=1 forces a rebuild from PULSE_START. rivers.json is only rebuilt
when it is missing or PULSE_GEOMETRY=1 is set (the gauge list changed).
"""

import os
import json
import math
import time
import logging
from datetime import date, datetime, timedelta

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(os.path.dirname(HERE), "site", "pulse")

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "gorgegauge.com")


def flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


UPLOAD = flag("PULSE_UPLOAD")
FULL = flag("PULSE_FULL")
GEOMETRY = flag("PULSE_GEOMETRY")

# Five water years of history: the water year starts 1 October.
START = os.environ.get("PULSE_START", "2021-10-01")
REFRESH_DAYS = int(os.environ.get("PULSE_REFRESH_DAYS", "45"))

# The map frame: the Cowlitz valley and the north side of Mt Adams down to
# the Clackamas headwaters south of Mt Hood, and from just west of Portland
# out to just north-east of Boardman. Geometry is clipped to this box so
# nothing pulls the view away from the Gorge.
BBOX = (-122.95, 45.00, -119.45, 46.55)   # west, south, east, north

# site, name, short label, kind, upstream km, downstream km, counted
#
# kind: "spine" is the Columbia; "gorge" rivers join it inside the Gorge;
# "beyond" rivers are in the frame but belong to the Portland basin or the
# Yakima valley. counted=True marks the rivers summed into the headline
# "Gorge tributaries combined": one gauge per river, nothing nested (the
# Sandy gauge already includes Bull Run).
#
# Not here: the West Fork Hood (NWRFC only, no USGS discharge) and the
# Washougal (USGS record ended in 1981).
#
# Downstream navigation runs until it meets a river already collected, so
# order matters: the Columbia first, then each main stem before its
# tributaries. Rivers whose downstream path reaches a river that is not
# drawn (the Clackamas to the Willamette is drawn; the Yakima to the
# Columbia is outside the frame) get a hand-set downstream cap.
GAUGES = [
    ("14105700", "Columbia River",           "Columbia",      "spine",  175, 175, False),
    ("14103000", "Deschutes River",          "Deschutes",     "gorge",  120,  25, True),
    ("14101500", "White River",              "White River",   "gorge",   70,  15, False),
    ("14048000", "John Day River",           "John Day",      "gorge",  120,  25, True),
    ("14113000", "Klickitat River",          "Klickitat",     "gorge",  140,  30, True),
    ("14120000", "Hood River",               "Hood",          "gorge",   80,  20, True),
    ("14123500", "White Salmon River",       "White Salmon",  "gorge",  100,  20, True),
    ("14128500", "Wind River",               "Wind",          "gorge",   80,  20, True),
    ("14142500", "Sandy River",              "Sandy",         "gorge",  100,  45, True),
    ("14138850", "Bull Run River",           "Bull Run",      "gorge",   40,  25, False),
    ("14211720", "Willamette River",         "Willamette",    "beyond", 120,  25, False),
    ("14211010", "Clackamas River",          "Clackamas",     "beyond", 140,   6, False),
    ("14200000", "Molalla River",            "Molalla",       "beyond",  80,  12, False),
    ("14220500", "Lewis River",              "Lewis",         "beyond",  45,  45, False),
    ("14216000", "Upper Lewis River",        "Upper Lewis",   "beyond",  60,  15, False),
    ("14222500", "East Fork Lewis River",    "EF Lewis",      "beyond",  60,  32, False),
    ("14243000", "Cowlitz River",            "Cowlitz",       "beyond", 130,  30, False),
    ("14231000", "Upper Cowlitz River",      "Upper Cowlitz", "beyond",  60,  15, False),
    ("14242580", "Toutle River",             "Toutle",        "beyond",  60,  25, False),
    ("12510500", "Yakima River",             "Yakima",        "beyond",  70,   5, False),
]

# Daily rain and snow, from Open-Meteo's reanalysis archive, sampled at a
# handful of places across the frame so the map can show the wet west end
# and the dry east end. The archive lags a few days; the forecast endpoint's
# past_days fills the tail.
RAIN_POINTS = [
    ("Portland",        45.52, -122.68),
    ("Cougar",          46.05, -122.30),
    ("Cascade Locks",   45.67, -121.89),
    ("Government Camp", 45.30, -121.75),
    ("Hood River",      45.71, -121.52),
    ("Trout Lake",      46.00, -121.53),
    ("The Dalles",      45.59, -121.18),
    ("Boardman",        45.84, -119.70),
]
METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
METEO_RECENT = "https://api.open-meteo.com/v1/forecast"

NLDI = "https://api.water.usgs.gov/nldi/linked-data/nwissite/USGS-{site}/navigation/{nav}/flowlines?distance={km}"
NWIS_DV = "https://waterservices.usgs.gov/nwis/dv/"
NWIS_IV = "https://waterservices.usgs.gov/nwis/iv/"
NWIS_STAT = "https://waterservices.usgs.gov/nwis/stat/"
NWIS_SITE = "https://waterservices.usgs.gov/nwis/site/"

WALL_TOL = 0.0004      # degrees, ~40 m: simplification for the gauged river itself
GROUND_TOL = 0.0012    # ~120 m: the faint tributary network under it
GROUND_KM = 80         # how far up the tributary network to draw
CONFLUENCE_M = 400     # a downstream reach ending this close to a drawn river stops there


# -- HTTP ---------------------------------------------------------------------

def get(url, params=None, tries=4, timeout=120):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "gorgegauge.com pulse builder"})
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code} for {r.url}")
            if r.status_code == 400:
                break               # a bad request will not get better
        except requests.RequestException as e:
            last = e
        wait = 3 * (i + 1)
        log.warning("  retry in %ds: %s", wait, last)
        time.sleep(wait)
    raise last


# -- Geometry helpers ---------------------------------------------------------

def simplify(pts, tol):
    """Douglas-Peucker on [lon, lat] lists (degrees)."""
    if len(pts) < 3:
        return pts
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    dx, dy = x1 - x0, y1 - y0
    norm = math.hypot(dx, dy)
    best, best_d = 0, -1.0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        if norm == 0:
            d = math.hypot(px - x0, py - y0)
        else:
            d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm
        if d > best_d:
            best, best_d = i, d
    if best_d > tol:
        return simplify(pts[:best + 1], tol)[:-1] + simplify(pts[best:], tol)
    return [pts[0], pts[-1]]


def rounded(pts):
    return [[round(x, 4), round(y, 4)] for x, y in pts]


def clip(pts):
    """Split a polyline into the runs of points that fall inside BBOX."""
    w, s, e, n = BBOX
    runs, cur = [], []
    for x, y in pts:
        if w <= x <= e and s <= y <= n:
            cur.append([x, y])
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) >= 2]


def metres(a, b):
    """Flat-earth distance in metres between two [lon, lat] points."""
    ky = 110_574.0
    kx = 111_320.0 * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((a[0] - b[0]) * kx, (a[1] - b[1]) * ky)


def flowlines(site, nav, km):
    r = get(NLDI.format(site=site, nav=nav, km=km))
    feats = r.json().get("features", [])
    out = []
    for f in feats:
        g = f.get("geometry") or {}
        if g.get("type") == "LineString" and len(g["coordinates"]) >= 2:
            out.append({
                "comid": f.get("properties", {}).get("nhdplus_comid"),
                "pts": [[float(x), float(y)] for x, y, *_ in g["coordinates"]],
            })
    return out


def near_any(pt, lines, limit_m):
    for line in lines:
        for q in line:
            if metres(pt, q) < limit_m:
                return True
    return False


def cut_at_confluence(reaches, drawn):
    """
    Keep downstream reaches until one ends on a river already drawn. NLDI
    returns them in downstream order, so this is a walk with a stop.
    """
    kept = []
    for reach in reaches:
        kept.append(reach)
        if near_any(reach["pts"][-1], drawn, CONFLUENCE_M):
            break
    return kept


# -- USGS tabular helpers -----------------------------------------------------

def rdb_rows(text):
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    if len(lines) < 2:
        return []
    header = lines[0].split("\t")
    rows = []
    for l in lines[2:]:
        rows.append(dict(zip(header, l.split("\t"))))
    return rows


def site_locations(sites):
    r = get(NWIS_SITE, {"format": "rdb", "sites": ",".join(sites), "siteOutput": "expanded"})
    out = {}
    for row in rdb_rows(r.text):
        try:
            out[row["site_no"]] = (float(row["dec_long_va"]), float(row["dec_lat_va"]))
        except (KeyError, ValueError):
            pass
    return out


def daily_normals(sites):
    """
    Period-of-record mean discharge for each day of the year, plus the
    overall mean. The stat service rejects long site lists and is often
    briefly unavailable, so ask for a few sites at a time and carry on when
    a chunk fails (the river then has no width statistics until the next
    rebuild).
    """
    per_site = {}
    for i in range(0, len(sites), 4):
        chunk = sites[i:i + 4]
        try:
            r = get(NWIS_STAT, {
                "format": "rdb", "sites": ",".join(chunk), "statReportType": "daily",
                "statTypeCd": "mean", "parameterCd": "00060",
            }, tries=6, timeout=90)
        except Exception as e:
            log.warning("  daily normals for %s failed: %s", chunk, e)
            continue
        for row in rdb_rows(r.text):
            try:
                m, d, v = int(row["month_nu"]), int(row["day_nu"]), float(row["mean_va"])
            except (KeyError, ValueError):
                continue
            # day-of-year in a leap year so Feb 29 has a slot
            doy = (date(2024, m, d) - date(2024, 1, 1)).days
            per_site.setdefault(row["site_no"], [None] * 366)[doy] = v
    out = {}
    for site, arr in per_site.items():
        vals = [v for v in arr if v is not None]
        out[site] = {
            "normal": [round(v, 1) if v is not None else None for v in arr],
            "mean_cfs": round(sum(vals) / len(vals), 1) if vals else None,
        }
    missing = [x for x in sites if x not in out]
    if missing:
        log.warning("No daily normals for %s", missing)
    return out


def daily_values(sites, start, end):
    """Mean daily discharge and temperature per site, keyed by ISO date."""
    r = get(NWIS_DV, {
        "format": "json", "sites": ",".join(sites), "parameterCd": "00060,00010",
        "statCd": "00003", "startDT": start, "endDT": end,
    })
    out = {}
    for ts in r.json()["value"]["timeSeries"]:
        site = ts["sourceInfo"]["siteCode"][0]["value"]
        code = ts["variable"]["variableCode"][0]["value"]
        key = "cfs" if code == "00060" else "temp"
        series = out.setdefault(site, {}).setdefault(key, {})
        for v in ts["values"][0]["value"]:
            try:
                val = float(v["value"])
            except (TypeError, ValueError):
                continue
            if val <= -999:
                continue
            series[v["dateTime"][:10]] = val
    return out


def instantaneous_as_daily(site, code, start, end):
    """
    Some gauges publish a parameter only as instantaneous values (the Wind
    River's discharge, the Klickitat's temperature). Average each day's
    readings so they line up with the daily series of the other gauges.
    """
    sums, counts = {}, {}
    # One request per calendar year: five years of 15-minute readings is
    # too much for a single response.
    a = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while a <= stop:
        b = min(date(a.year, 12, 31), stop)
        try:
            r = get(NWIS_IV, {
                "format": "json", "sites": site, "parameterCd": code,
                "startDT": a.isoformat(), "endDT": b.isoformat(),
            }, tries=2)
        except Exception as e:
            log.warning("  iv %s for %s (%s..%s) failed: %s", code, site, a, b, e)
            a = b + timedelta(days=1)
            continue
        for ts in r.json()["value"]["timeSeries"]:
            for v in ts["values"][0]["value"]:
                try:
                    val = float(v["value"])
                except (TypeError, ValueError):
                    continue
                if val <= -999:
                    continue
                day = v["dateTime"][:10]
                sums[day] = sums.get(day, 0.0) + val
                counts[day] = counts.get(day, 0) + 1
        a = b + timedelta(days=1)
    return {d: sums[d] / counts[d] for d in sums}


def fetch_rain(start, end):
    """
    {"YYYY-MM-DD": ([mm per point], [cm of snow per point])} for the range.
    The archive covers up to about a week ago; the last days come from the
    forecast endpoint's history, which is the same model a few days fresher.
    """
    lats = ",".join(str(p[1]) for p in RAIN_POINTS)
    lons = ",".join(str(p[2]) for p in RAIN_POINTS)
    out = {}

    def absorb(payload):
        results = payload if isinstance(payload, list) else [payload]
        for k, r in enumerate(results):
            d = r.get("daily", {})
            for i, day in enumerate(d.get("time", [])):
                mm = d["precipitation_sum"][i]
                cm = d.get("snowfall_sum", [None] * len(d["time"]))[i]
                rec = out.setdefault(day, ([None] * len(RAIN_POINTS), [None] * len(RAIN_POINTS)))
                rec[0][k] = None if mm is None else round(float(mm), 1)
                rec[1][k] = None if cm is None else round(float(cm), 1)

    archive_end = min(end, date.today() - timedelta(days=7))
    if archive_end >= start:
        try:
            absorb(get(METEO_ARCHIVE, {
                "latitude": lats, "longitude": lons,
                "start_date": start.isoformat(), "end_date": archive_end.isoformat(),
                "daily": "precipitation_sum,snowfall_sum", "timezone": "America/Los_Angeles",
            }, timeout=180).json())
        except Exception as e:
            log.warning("  rain archive failed: %s", e)
    try:
        absorb(get(METEO_RECENT, {
            "latitude": lats, "longitude": lons, "past_days": 14, "forecast_days": 1,
            "daily": "precipitation_sum,snowfall_sum", "timezone": "America/Los_Angeles",
        }).json())
    except Exception as e:
        log.warning("  recent rain failed: %s", e)
    return out


# -- Builders -----------------------------------------------------------------

def build_rivers():
    sites = [g[0] for g in GAUGES]
    log.info("Site locations…")
    locs = site_locations(sites)
    log.info("Daily normals…")
    normals = daily_normals(sites)

    rivers = []
    drawn = []          # every wall polyline collected so far, for confluence cuts
    for site, name, short, kind, um_km, dm_km, counted in GAUGES:
        log.info("%s (%s)", name, site)
        up = flowlines(site, "UM", um_km)
        down = flowlines(site, "DM", dm_km)
        if kind != "spine":
            down = cut_at_confluence(down, drawn)
        up_ids = {r["comid"] for r in up}

        walls = []
        for reach in up + down:
            for run in clip(reach["pts"]):
                pts = rounded(simplify(run, WALL_TOL))
                if len(pts) >= 2:
                    walls.append(pts)
        drawn.extend(r["pts"] for r in up + down)

        ground = []
        if kind != "spine":
            for reach in flowlines(site, "UT", GROUND_KM):
                if reach["comid"] in up_ids:
                    continue
                for run in clip(reach["pts"]):
                    pts = rounded(simplify(run, GROUND_TOL))
                    if len(pts) >= 2:
                        ground.append(pts)

        stats = normals.get(site, {})
        rivers.append({
            "site": site,
            "name": name,
            "short": short,
            "kind": kind,
            "counted": counted,
            "gauge": [round(v, 4) for v in locs.get(site, (None, None))],
            "mean_cfs": stats.get("mean_cfs"),
            "normal": stats.get("normal"),
            "walls": walls,
            "ground": ground,
        })
        log.info("  %d wall reaches, %d ground reaches, mean %s cfs",
                 len(walls), len(ground), stats.get("mean_cfs"))

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "bbox": list(BBOX),
        "rivers": rivers,
    }


def fetch_window(sites, start, end):
    """Daily flow and temperature for every site over one date range."""
    log.info("Daily values %s → %s…", start, end)
    dv = daily_values(sites, start.isoformat(), end.isoformat())
    n_expected = (end - start).days + 1
    for site in sites:
        rec = dv.setdefault(site, {})
        for key, code in (("cfs", "00060"), ("temp", "00010")):
            if len(rec.get(key, {})) < n_expected * 0.5:
                log.info("  %s has sparse daily %s; averaging instantaneous readings", site, key)
                iv = instantaneous_as_daily(site, code, start.isoformat(), end.isoformat())
                merged = dict(iv)
                merged.update(rec.get(key, {}))   # published daily means win
                rec[key] = merged
    return dv


def build_flows(existing=None):
    sites = [g[0] for g in GAUGES]
    start = date.fromisoformat(START)
    end = date.today() - timedelta(days=1)

    # Append to the existing record when there is one that starts on the
    # same day; otherwise rebuild from the start.
    prior = None
    same_sites = existing and set(existing.get("sites", {})) == set(sites)
    if existing and not FULL and same_sites and existing.get("start") == start.isoformat():
        prior = existing
        fetch_from = max(start, date.fromisoformat(existing["end"]) - timedelta(days=REFRESH_DAYS))
        log.info("Appending to record %s → %s; refreshing from %s",
                 existing["start"], existing["end"], fetch_from)
    else:
        fetch_from = start
        log.info("Building the full record from %s", start)

    dv = fetch_window(sites, fetch_from, end)

    n = (end - start).days + 1
    days = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    first_new = (fetch_from - start).days
    out = {}
    for site in sites:
        cfs = dv.get(site, {}).get("cfs", {})
        temp = dv.get(site, {}).get("temp", {})
        rec = {"cfs": [None] * n, "temp": [None] * n}
        if prior and site in prior["sites"]:
            for key in ("cfs", "temp"):
                old = prior["sites"][site].get(key, [])
                rec[key][:min(len(old), n)] = old[:n]
        for i in range(first_new, n):
            d = days[i]
            rec["cfs"][i] = cfs.get(d)
            rec["temp"][i] = round(temp[d], 1) if d in temp else None
        out[site] = rec
        have = sum(v is not None for v in rec["cfs"])
        havet = sum(v is not None for v in rec["temp"])
        log.info("  %s: %d/%d days of flow, %d of temperature", site, have, n, havet)

    # Rain and snow: the whole record the first time, then just the window.
    rain_from = fetch_from if prior and prior.get("rain") else start
    log.info("Rain %s → %s…", rain_from, end)
    fresh = fetch_rain(rain_from, end)
    npts = len(RAIN_POINTS)
    precip = [[None] * npts for _ in range(n)]
    snow = [[None] * npts for _ in range(n)]
    if prior and prior.get("rain"):
        old = prior["rain"]
        for i in range(min(len(old.get("precip", [])), n)):
            precip[i] = list(old["precip"][i])
            snow[i] = list(old["snow"][i])
    for i, d in enumerate(days):
        if d in fresh:
            precip[i], snow[i] = fresh[d]
    have = sum(1 for row in precip if row[0] is not None)
    log.info("  rain: %d/%d days", have, n)

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": n,
        "sites": out,
        "rain": {
            "points": [{"name": nm, "lat": la, "lon": lo} for nm, la, lo in RAIN_POINTS],
            "precip": precip,     # mm per day per point
            "snow": snow,         # cm of snowfall per day per point
        },
    }


# -- Output -------------------------------------------------------------------

def write(name, payload):
    os.makedirs(SITE_DIR, exist_ok=True)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path = os.path.join(SITE_DIR, name)
    with open(path, "wb") as f:
        f.write(body)
    log.info("Wrote %s (%d KB)", path, len(body) // 1024)
    if UPLOAD:
        import boto3
        boto3.client("s3").put_object(
            Bucket=OUTPUT_BUCKET, Key=f"pulse/{name}", Body=body,
            ContentType="application/json", CacheControl="public, max-age=900",
        )
        log.info("Uploaded s3://%s/pulse/%s", OUTPUT_BUCKET, name)


def read_existing(name):
    """The current file: from the bucket when uploading, else the local copy."""
    if UPLOAD:
        try:
            import boto3
            obj = boto3.client("s3").get_object(Bucket=OUTPUT_BUCKET, Key=f"pulse/{name}")
            return json.loads(obj["Body"].read())
        except Exception as e:
            log.info("No s3://%s/pulse/%s yet (%s)", OUTPUT_BUCKET, name, e.__class__.__name__)
            return None
    path = os.path.join(SITE_DIR, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def main():
    if GEOMETRY or read_existing("rivers.json") is None:
        write("rivers.json", build_rivers())
    else:
        log.info("rivers.json exists; set PULSE_GEOMETRY=1 to rebuild it")
    write("flows.json", build_flows(read_existing("flows.json")))


if __name__ == "__main__":
    main()
