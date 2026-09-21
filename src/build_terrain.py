#!/usr/bin/env python3
"""
Build the relief behind /pulse.html — "The Gorge's River Pulse".

One file:

  pulse/terrain.json  A regular lon/lat grid of ground elevation (metres)
                      covering the map frame (BBOX in build_pulse.py), about
                      240 columns by 90 rows — roughly 1.4 km cells. The
                      browser module pulse-terrain.js turns it into the
                      hillshaded table the river walls stand on.

Source: AWS Terrain Tiles (Mapzen "terrarium" encoding, no key needed):
  https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
  elevation_m = (R * 256 + G + B / 256) - 32768

Zoom 9 (~200 m per pixel at this latitude) is plenty: each grid cell is the
mean of the ~7×7 pixels it covers, which also smooths ridge noise so the
relief reads cleanly on a tilted table.

Written to site/pulse/ for local preview and, when PULSE_UPLOAD is set,
uploaded to the gorgegauge.com bucket like the other pulse data files. The
terrain never changes, so the workflow only runs this when asked to rebuild
geometry.
"""

import io
import os
import json
import math
import time
import logging

import requests
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(os.path.dirname(HERE), "site", "pulse")

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "gorgegauge.com")


def flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


UPLOAD = flag("PULSE_UPLOAD")

# The frame is whatever build_pulse.py draws: west, south, east, north.
from src.build_pulse import BBOX

ZOOM = int(os.environ.get("TERRAIN_ZOOM", "11"))     # ~76 m pixels
COLS = int(os.environ.get("TERRAIN_COLS", "720"))    # ~0.5 km cells across the frame
# Rows follow the frame's aspect so cells stay roughly square on the ground.
_w, _s, _e, _n = BBOX
_aspect = ((_n - _s) * 110.574) / ((_e - _w) * 111.32 * math.cos(math.radians((_s + _n) / 2)))
ROWS = int(os.environ.get("TERRAIN_ROWS", str(round(COLS * _aspect))))

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
TILE_PX = 256


# -- HTTP ---------------------------------------------------------------------

def get(url, tries=4, timeout=60):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "gorgegauge.com pulse builder"})
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code} for {r.url}")
        except requests.RequestException as e:
            last = e
        wait = 3 * (i + 1)
        log.warning("  retry in %ds: %s", wait, last)
        time.sleep(wait)
    raise last


# -- Web Mercator -------------------------------------------------------------

def lon_to_px(lon, z):
    """Global pixel x at zoom z (256 px tiles)."""
    return (lon + 180.0) / 360.0 * (TILE_PX << z)


def lat_to_px(lat, z):
    """Global pixel y at zoom z (256 px tiles), increasing southward."""
    rad = math.radians(lat)
    return (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * (TILE_PX << z)


# -- Build --------------------------------------------------------------------

def fetch_mosaic(z):
    """Every tile touching BBOX, decoded to metres, as one flat row-major list.

    Returns (elev, width, height, px0, py0): elev is the mosaic's elevation in
    metres, (px0, py0) is the global pixel coordinate of its top-left corner.
    """
    w, s, e, n = BBOX
    tx0, tx1 = int(lon_to_px(w, z) // TILE_PX), int(lon_to_px(e, z) // TILE_PX)
    ty0, ty1 = int(lat_to_px(n, z) // TILE_PX), int(lat_to_px(s, z) // TILE_PX)
    ncols, nrows = tx1 - tx0 + 1, ty1 - ty0 + 1
    width, height = ncols * TILE_PX, nrows * TILE_PX
    log.info("Fetching %d tiles at zoom %d (%d×%d px)", ncols * nrows, z, width, height)

    # Tiles are cached on disk (they never change) and fetched in parallel:
    # a zoom-11 frame is a few hundred of them.
    from concurrent.futures import ThreadPoolExecutor
    cache = os.path.join(os.path.expanduser("~"), ".cache", "gorgegauge-terrain")
    os.makedirs(cache, exist_ok=True)

    def tile_bytes(tx, ty):
        path = os.path.join(cache, f"{z}-{tx}-{ty}.png")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return tx, ty, f.read()
        body = get(TILE_URL.format(z=z, x=tx, y=ty)).content
        with open(path, "wb") as f:
            f.write(body)
        return tx, ty, body

    coords = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    mosaic = Image.new("RGB", (width, height))
    with ThreadPoolExecutor(max_workers=12) as pool:
        for tx, ty, body in pool.map(lambda c: tile_bytes(*c), coords):
            tile = Image.open(io.BytesIO(body)).convert("RGB")
            mosaic.paste(tile, ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX))

    # terrarium: elevation = R*256 + G + B/256 - 32768, as a float image so
    # the whole mosaic (tens of millions of pixels) is handled by Pillow.
    from PIL import ImageMath
    rr, gg, bb = [ch.convert("I") for ch in mosaic.split()]
    elev_img = ImageMath.eval("float(r) * 256 + float(g) + float(b) / 256 - 32768", r=rr, g=gg, b=bb)
    return elev_img, width, height, tx0 * TILE_PX, ty0 * TILE_PX


def build_terrain():
    w, s, e, n = BBOX
    elev, width, height, px0, py0 = fetch_mosaic(ZOOM)   # a float image

    # Pixel column/row edges for every grid cell, in mosaic coordinates.
    xedges = [lon_to_px(w + (e - w) * c / COLS, ZOOM) - px0 for c in range(COLS + 1)]
    yedges = [lat_to_px(n - (n - s) * r / ROWS, ZOOM) - py0 for r in range(ROWS + 1)]

    def span(edges, i, limit):
        a = max(0, int(math.floor(edges[i])))
        b = min(limit, int(math.ceil(edges[i + 1])))
        return a, max(b, a + 1)

    # Crop the mosaic to the frame and box-average it down to the grid.
    crop = elev.crop((int(math.floor(xedges[0])), int(math.floor(yedges[0])),
                      int(math.ceil(xedges[-1])), int(math.ceil(yedges[-1]))))
    grid = crop.resize((COLS, ROWS), Image.BOX)
    out = [max(0, int(round(v))) for v in grid.getdata()]   # nodata / sea → 0

    # The tiles carry the odd nodata spike at their seams; nothing in this
    # frame is higher than Mt Adams, so anything above 3,800 m takes the
    # median of its neighbours instead.
    for k, v in enumerate(out):
        if v > 3800:
            r, c = divmod(k, COLS)
            near = [out[rr * COLS + cc] for rr in range(max(r - 1, 0), min(r + 2, ROWS))
                    for cc in range(max(c - 1, 0), min(c + 2, COLS)) if (rr, cc) != (r, c)]
            near = sorted(x for x in near if x <= 3800)
            out[k] = near[len(near) // 2] if near else 0
    lo, hi = min(out), max(out)
    log.info("Grid %d×%d: %d m .. %d m", COLS, ROWS, lo, hi)

    # Metres packed into two 8-bit channels (R*256 + G), which the browser
    # reads back from a canvas. Far smaller than JSON at this resolution.
    png = Image.new("RGB", (COLS, ROWS))
    png.putdata([(m >> 8, m & 255, 0) for m in out])
    buf = io.BytesIO()
    png.save(buf, format="PNG", optimize=True)
    return {
        "bbox": list(BBOX),
        "cols": COLS,
        "rows": ROWS,
        "png": "terrain.png",
        "max_m": hi,
    }, buf.getvalue()


# -- Output -------------------------------------------------------------------

def write(name, body, content_type):
    os.makedirs(SITE_DIR, exist_ok=True)
    path = os.path.join(SITE_DIR, name)
    with open(path, "wb") as f:
        f.write(body)
    log.info("Wrote %s (%d KB)", path, len(body) // 1024)
    if UPLOAD:
        import boto3
        boto3.client("s3").put_object(
            Bucket=OUTPUT_BUCKET, Key=f"pulse/{name}", Body=body,
            ContentType=content_type, CacheControl="public, max-age=86400",
        )
        log.info("Uploaded s3://%s/pulse/%s", OUTPUT_BUCKET, name)


def main():
    meta, png = build_terrain()
    write("terrain.png", png, "image/png")
    write("terrain.json", json.dumps(meta, separators=(",", ":")).encode("utf-8"), "application/json")


if __name__ == "__main__":
    main()
