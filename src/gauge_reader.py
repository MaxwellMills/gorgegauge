#!/usr/bin/env python3
"""
Read the Husum Falls staff gauge from recent trail-cam photos.

Rewritten 2026-09-02. What changed and why:

  * Readings are now measured DOWNWARD from the nearest whole-foot hashmark
    ABOVE the waterline. That mark is always dry and legible.

    The previous prompt derived a *lower* bound from label visibility
    ("'2' visible above water -> reading MUST be >= 2.0"). That is backwards.
    A number label is printed above its hashmark, so it stays dry and readable
    as the water falls. Seeing it is an upper bound, not a floor. The rule
    clamped every low-water reading at 2.0 and the site sat at 2.1 for months.

  * Each run only looks at photos taken since the previous run's newest photo,
    so consecutive daily readings share no input images. The old code took the
    newest five regardless of date, which overlapped ~3 of 5 images day to day
    and damped out any real change.

  * The camera uploads 880x660 JPEGs, roughly 30 px per foot of staff. Readings
    are therefore reported to 0.1 ft, and the spread across images is published
    as an honest error bar instead of being hidden behind a median.

  * Every run is appended to husumGaugeHistory.json, and the concurrent USGS
    reading at White Salmon nr Underwood is recorded alongside it, so a stuck
    value is detectable instead of invisible.

Revised 2026-11-13, after the page sat at 2.2 ft for three days on a river
near 0.9:

  * Each frame is registered to the calibration frame before it is read.
    The camera's view had shifted up by 7 rows between 2026-09-15 and
    2026-10-03, which the fixed calibration turned into a 0.4 ft overread
    for six weeks. Frames that do not match the calibrated view (glare on
    the wall) are skipped instead of read.

  * The waterline detector now tells a wet, silt-stained band on the lower
    staff from the water below it, by requiring water-dark rows under the
    step and using the staff's reflection as confirmation. See the notes
    above detect_waterline for what it can and cannot separate.

  * A calibrated camera's frame that the pixel path cannot read is skipped;
    it is no longer handed to the model, whose readings on this camera were
    off by a foot or more in either direction.

Revised 2026-11-14, the day the Truss camera (016578004123334) went in:

  * The window is per camera. It used to be the IMAGE_COUNT newest exposures
    over all cameras; the Truss fires twice an hour, so those were always
    its frames and the old camera stopped being read the day it was installed.

  * Only calibrated cameras can reach the published number. The old fallback
    counted every camera when the primary had too few frames, which put five
    model-read Truss frames (0.05 to 1.7 ft, two of them setup shots of a
    ceiling and a mis-aimed wall) behind a 1.2 ft reading on a river at 0.9.

  * The Truss has a calibration (see DEFAULT_CALIBRATION) taken from two
    fixed references rather than the numerals, which it cannot resolve.

  * Both cameras are anchored to a reading of the staff by eye: 1.0 ft on
    2026-11-14, a flat day. The 2026-09-02 rows were the centres of the
    painted numerals; the foot line is at the bottom of each numeral, 0.15 ft
    lower, which is where the anchor puts it. Readings from 2026-09-03 to
    2026-11-14 are about 0.15 ft low. READER_VERSION went to 4 so the jump
    check does not compare across the change.

  * The waterline is refined to the bright-to-dark edge at the water rather
    than the steepest drop near the candidate step: a wet strip of board
    above the water is dark too, and the old rule landed anywhere in it.

  * A close-up of the staff is published next to the JSON: the frame the
    number came from, enlarged, with the calibrated foot marks and the
    detected waterline drawn on. Readers can check the number against the
    picture without reading paint the camera cannot show them.
"""

import os
import re
import json
import base64
import logging
import statistics
from datetime import datetime, timedelta
from urllib.parse import quote

import boto3
import requests
import anthropic
import pytz

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

PACIFIC = pytz.timezone("America/Los_Angeles")

SOURCE_BUCKET  = os.environ.get("SOURCE_BUCKET",  "gorgewhitewater")
SOURCE_PREFIX  = os.environ.get("SOURCE_PREFIX",  "tactacam/Husum/")
OUTPUT_BUCKET  = os.environ.get("OUTPUT_BUCKET",  "gorgegauge.com")
OUTPUT_KEY     = os.environ.get("OUTPUT_KEY",     "husumGauge.json")
HISTORY_KEY    = os.environ.get("HISTORY_KEY",    "husumGaugeHistory.json")

IMAGE_COUNT    = int(os.environ.get("IMAGE_COUNT", "5"))      # per camera
MIN_IMAGES     = int(os.environ.get("MIN_IMAGES", "2"))
STALE_CAMERA_DAYS = int(os.environ.get("STALE_CAMERA_DAYS", "7"))
STALE_FRAME_DAYS = float(os.environ.get("STALE_FRAME_DAYS", "1.5"))
HISTORY_MAX    = int(os.environ.get("HISTORY_MAX", "730"))

# Independent cross-check: USGS White Salmon River nr Underwood, WA.
USGS_SITE      = os.environ.get("USGS_SITE", "14123500")

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")

# Ignore the previous run's cutoff and read the newest IMAGE_COUNT exposures
# regardless. For re-reading after a reader change, not for daily use.
REREAD = os.environ.get("REREAD", "").strip().lower() in ("1", "true", "yes", "on")

PUBLIC_IMAGE_BASE = os.environ.get(
    "PUBLIC_IMAGE_BASE",
    "https://gorgewhitewater.s3.us-east-2.amazonaws.com"
)

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

STAMP_FMT = "%m%d%Y%H%M%S"

s3 = boto3.client("s3")


# -- S3 helpers ---------------------------------------------------------------

def list_s3_images():
    """List images using boto3 (requires ListBucket on SOURCE_BUCKET)."""
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=SOURCE_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith((".jpg", ".jpeg")):
                keys.append(key)
    return keys


def parse_stamp(key):
    """Pull the MMDDYYYYHHMMSS stamp out of a Tactacam filename."""
    match = re.search(r"-(\d{14})-", key)
    if not match:
        return None, None
    raw = match.group(1)
    try:
        return datetime.strptime(raw, STAMP_FMT), raw
    except ValueError:
        return None, None


def parse_timestamp(key):
    return parse_stamp(key)[0]


def camera_id(key):
    """Tactacam IMEI, the leading field of the filename."""
    return key.split("/")[-1].split("-")[0]


def photo_serial(key):
    """
    The camera's own photo counter, last four digits of the final token:
    W1004540 and SYEW4540 are the same exposure. Tactacam uploads a small
    frame first and, on request, an HD copy of the same exposure later under
    a new filename whose timestamp is the request time, not the capture time.
    """
    stem = key.split("/")[-1].rsplit(".", 1)[0]
    m = re.search(r"(\d{4})$", stem.split("-")[-1])
    return m.group(1) if m else None


def is_hd(key):
    return "-HD-" in key.split("/")[-1].upper()


def dedupe_exposures(dated):
    """
    Collapse each exposure to one entry: (key_to_read, capture_time). The HD
    copy is read when there is one; the capture time is the earliest stamp in
    the group, so an HD re-upload never masquerades as a newer photo.
    """
    groups = {}
    for k, t in dated:
        serial = photo_serial(k)
        gid = (camera_id(k), t.strftime("%m%d%Y"), serial) if serial else (k,)
        groups.setdefault(gid, []).append((k, t))

    out = []
    for members in groups.values():
        capture = min(t for _, t in members)
        hd = [k for k, _ in members if is_hd(k)]
        pick = hd[0] if hd else min(members, key=lambda x: x[1])[0]
        if len(members) > 1:
            log.info("  exposure %s: %d uploads, reading %s (captured %s)",
                     photo_serial(pick), len(members), pick.split("/")[-1], capture)
        out.append((pick, capture))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def select_window(keys, prev_stamp, n):
    """
    Photos to read this run, newest first: up to n from each camera.

    Only photos newer than the previous run's newest photo are used, so two
    consecutive runs never share an input image. prev_stamp is the raw
    MMDDYYYYHHMMSS string carried forward in husumGauge.json, which sidesteps
    any timezone comparison between camera time and run time.

    The cap is per camera. Until 2026-11-14 it was the n newest exposures
    over all cameras, and a camera that fires twice an hour (the Truss)
    filled it every day, so the primary camera was never read again. A
    camera with nothing new is left out rather than topped up: a dead
    camera should not keep voting with yesterday's frames.
    """
    dated = [(k, parse_timestamp(k)) for k in keys]
    dated = [(k, t) for k, t in dated if t]
    dated = dedupe_exposures(dated)      # newest capture first

    if not dated:
        return []

    cutoff = None
    if prev_stamp:
        try:
            cutoff = datetime.strptime(prev_stamp, STAMP_FMT)
        except (ValueError, TypeError):
            log.warning("Unparseable previous stamp %r — falling back to newest %d per camera.",
                        prev_stamp, n)

    by_camera = {}
    for k, t in dated:
        if camera_id(k) in IGNORE_CAMERAS:
            continue
        by_camera.setdefault(camera_id(k), []).append((k, t))
    if not by_camera:
        return []

    # A camera that has not uploaded in a week is gone (retired, dead, or
    # moved); without a cutoff it would otherwise contribute its last n frames
    # from months ago and cost model calls to read them.
    stale_before = dated[0][1] - timedelta(days=STALE_CAMERA_DAYS)

    # Frames more than STALE_FRAME_DAYS older than the newest upload describe
    # a river that may no longer exist. Without a cutoff (first run, REREAD)
    # a camera that died two days ago would otherwise vote with its last
    # frames as if they were today's: 2026-11-16, the old camera silent since
    # the 14th, a re-read published 1.0 ft from its Nov 12-14 frames.
    old_before = dated[0][1] - timedelta(days=STALE_FRAME_DAYS)

    window = []
    for cam, frames in sorted(by_camera.items()):
        if frames[0][1] < stale_before:
            log.info("Camera %s: newest frame %s is over %d days older than the newest upload — ignoring it.",
                     cam, frames[0][1], STALE_CAMERA_DAYS)
            continue
        frames = [(k, t) for k, t in frames if t >= old_before]
        if not frames:
            continue
        if cutoff is None:
            window += frames[:n]
            continue
        fresh = [(k, t) for k, t in frames if t > cutoff]
        if not fresh:
            continue
        if len(fresh) < MIN_IMAGES:
            # One new photo on its own is a thin sample. Top up to MIN_IMAGES
            # with this camera's newest already-seen photos, and say so.
            older = [(k, t) for k, t in frames if t <= cutoff][:MIN_IMAGES - len(fresh)]
            log.warning("Camera %s: only %d new photo(s) since %s — topping up with %d "
                        "already-read one(s).", cam, len(fresh), prev_stamp, len(older))
            fresh += older
        window += fresh[:n]

    window.sort(key=lambda x: x[1], reverse=True)
    return window


def fetch_image_bytes(key):
    url = f"{PUBLIC_IMAGE_BASE}/{quote(key)}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def image_url_for_key(key):
    return f"{PUBLIC_IMAGE_BASE}/{quote(key)}"


# -- Claude reading -----------------------------------------------------------
#
# Two passes per photo.
#
#   Pass 1 looks at the full 880x660 frame and returns a bounding box for the
#   staff. The box is padded, cropped, and enlarged so the staff fills the
#   frame at several hundred pixels tall instead of ~120. Most of the reading
#   error was simply that 0.1 ft was three pixels.
#
#   Pass 2 reads the enlarged crop. It does not ask the model to guess "what
#   fraction of a foot" the water sits below a mark — that guess is what put
#   the reading at 1.4-1.8 ft on a day the water was just under 1.0. Instead
#   it asks for the pixel row of every whole-foot hashmark it can read, plus
#   the row of the waterline. Two readable marks a known number of feet apart
#   give the pixel-per-foot scale, and the waterline's distance below the
#   lowest readable mark converts to feet with arithmetic, in code, where it
#   can be checked. The model's own estimate is kept alongside as a fallback
#   and a cross-check.

LOCATE_PROMPT_TEMPLATE = (
    "This is a trail-camera photo of a river, {w} pixels wide by {h} pixels tall. Somewhere "
    "in it is a white vertical staff gauge with black hashmarks and numbers, mounted against "
    "the rock face and running down into the water. It is a tall thin white strip, much "
    "taller than it is wide. Return its bounding box in pixel coordinates of THIS image "
    "(0..{w} across, 0..{h} down, y increasing downward), from the top of the staff to where "
    "it meets the water. Be generous rather than tight. "
    "Respond ONLY with raw JSON, no markdown: "
    "{{\"x0\": 410, \"y0\": 230, \"x1\": 470, \"y1\": 390, \"found\": true}}. "
    "If there is no staff visible, respond {{\"found\": false}}."
)

READ_PROMPT_TEMPLATE = (
    "You are reading a river staff gauge at Husum Falls on the White Salmon River, WA. "
    "This image is a cropped and enlarged view of the staff, {w} pixels wide by {h} pixels "
    "tall. Every coordinate you report is a pixel position in THIS image, y increasing "
    "downward. "

    "STAFF LAYOUT: "
    "A white staff with a wide black hashmark at each whole foot and narrow hashmarks every "
    "0.25 ft between them. Whole-foot numbers increase going UP the staff. Each number is "
    "printed just ABOVE the wide hashmark it names. The spacing between whole-foot hashmarks "
    "is identical all the way down the staff. Do NOT assume which numbers are in view: the "
    "top of the staff may be out of frame, and the topmost visible mark is not necessarily "
    "the highest number on the staff. Report only numerals you can actually read. "

    "THE STAFF IS ITS OWN RULER. Read it this way: "
    "1. Find every whole-foot HASHMARK (the wide line, not the numeral above it) whose number "
    "   you can read with confidence. For each, report the number and the y pixel of the line. "
    "   Include as many as you can read; the more marks, the better the scale. "
    "2. Find the waterline: the y pixel where the water surface meets the staff. If the "
    "   surface is broken up, use the middle of the band. "
    "3. Take the two readable marks farthest apart, A above and B below. The pixel distance "
    "   between them is exactly (A minus B) feet, which gives you pixels per foot. "
    "4. Measure the pixel distance from mark B down to the waterline and divide by pixels "
    "   per foot. level = B minus that. This works whether or not the next mark down is "
    "   underwater, and whether or not you can read the '1'. "
    "5. If the narrow 0.25 ft ticks between B and the waterline are visible, count them as a "
    "   cross-check. "

    "A visible number is NOT a floor. The water is often below the lowest number you can "
    "read, and readings under 1.0 ft are normal in late summer. If the waterline is below "
    "the bottom end of the staff, report the y of the staff's bottom end as the waterline "
    "and say so in the notes. "

    "Only report a mark if you can read its number. A mark you are unsure of is worse than "
    "no mark. Use confidence \"high\" only when you read three or more marks and the "
    "waterline crosses the staff cleanly, \"low\" when glare, shadow, foam or reflection "
    "make the waterline a guess. "

    "Respond ONLY with raw JSON, no markdown, in exactly this shape: "
    "{{\"marks\": [{{\"value\": 5, \"y\": 88}}, {{\"value\": 4, \"y\": 206}}, "
    "{{\"value\": 3, \"y\": 325}}], \"waterline_y\": 384, \"level\": 2.5, "
    "\"confidence\": \"medium\", \"notes\": \"scale about 118 px/ft from the 5 and 3 marks; "
    "waterline 59 px below the 3 mark, so 3 minus 0.5\"}}"
)

READ_USER_PROMPT = (
    "Read the gauge. Report the y pixel of every whole-foot hashmark you can read and the y "
    "pixel of the waterline, then compute the level from the spacing between the marks. "
    "Return JSON only."
)

# Crop padding as a fraction of the located box, and target crop height after
# enlarging. The staff is ~120 px tall in the raw frame; ~900 px gives the
# model 0.1 ft at roughly 20 px.
CROP_PAD_X = float(os.environ.get("CROP_PAD_X", "0.6"))
CROP_PAD_Y = float(os.environ.get("CROP_PAD_Y", "0.2"))
CROP_TARGET_H = int(os.environ.get("CROP_TARGET_H", "900"))
CROP_MAX_SCALE = float(os.environ.get("CROP_MAX_SCALE", "5"))


def _client():
    return anthropic.Anthropic()


def _ask(system, user_text, image_bytes, max_tokens=700):
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    message = _client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": user_text},
            ],
        }],
    )
    text = message.content[0].text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in response: {text!r}")
    return json.loads(match.group())


def image_size(image_bytes):
    from io import BytesIO
    from PIL import Image
    return Image.open(BytesIO(image_bytes)).size


def locate_staff(image_bytes):
    """
    Pass 1: bounding box of the staff in full-frame pixels, or None.

    The box is rejected if it does not look like a staff: it must lie inside
    the frame, be taller than it is wide, and span between 4% and 70% of the
    frame height. On 2026-09-02 two frames came back with boxes on bare rock,
    one of them an HD frame the model had evidently boxed at 880x660 scale.
    """
    W, H = image_size(image_bytes)
    prompt = LOCATE_PROMPT_TEMPLATE.format(w=W, h=H)
    box = _ask(prompt, "Where is the staff gauge? JSON only.", image_bytes, max_tokens=200)
    if not box.get("found", True):
        return None
    try:
        x0, y0, x1, y1 = (float(box[k]) for k in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        return None
    if x1 > W * 1.05 or y1 > H * 1.05 or x0 < -0.05 * W or y0 < -0.05 * H:
        log.info("    locate box %s falls outside the %dx%d frame — ignoring it",
                 [round(v) for v in (x0, y0, x1, y1)], W, H)
        return None
    if bh < bw or not (0.04 * H <= bh <= 0.70 * H):
        log.info("    locate box %s is not staff-shaped for a %dx%d frame — ignoring it",
                 [round(v) for v in (x0, y0, x1, y1)], W, H)
        return None
    return x0, y0, x1, y1


def crop_and_enlarge(image_bytes, box):
    """
    Pad the located box, crop, and enlarge with a smooth resampler. Returns
    (jpeg_bytes, width, height, scale, origin, frame_size), where origin is
    the crop's top-left in full-frame pixels, so a y in the crop maps back to
    the frame as origin_y + y / scale. Falls back to the full frame if the
    box is degenerate.
    """
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    W, H = img.size
    origin = (0, 0)

    if box is None:
        crop = img
        scale = 1.0
    else:
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        x0 = max(0, int(x0 - bw * CROP_PAD_X))
        x1 = min(W, int(x1 + bw * CROP_PAD_X))
        y0 = max(0, int(y0 - bh * CROP_PAD_Y))
        y1 = min(H, int(y1 + bh * CROP_PAD_Y))
        if x1 - x0 < 20 or y1 - y0 < 40:
            crop, scale = img, 1.0
        else:
            crop = img.crop((x0, y0, x1, y1))
            origin = (x0, y0)
            scale = max(1.0, min(CROP_MAX_SCALE, CROP_TARGET_H / crop.height))

    if scale > 1.0:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)),
                           Image.LANCZOS)

    out = BytesIO()
    crop.save(out, format="JPEG", quality=92)
    return out.getvalue(), crop.width, crop.height, scale, origin, (W, H)


def level_from_geometry(marks, waterline_y):
    """
    Fit y = a + b*value through the reported whole-foot marks and evaluate at
    the waterline. Returns (level, px_per_ft, reason) — level is None when
    the marks do not describe a sane staff.

    Checks: at least two distinct marks; y increases as the number decreases
    (the staff reads 6 at the top); consecutive spacings agree with the fitted
    pixels-per-foot within 30%; and the waterline is not above a mark the
    model claims to have read dry.
    """
    pts = {}
    for m in marks or []:
        try:
            v, y = float(m["value"]), float(m["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if v.is_integer() and 0 <= v <= 6:
            pts[int(v)] = y
    if len(pts) < 2:
        return None, None, "fewer than two readable marks"

    seq = sorted(pts.items(), key=lambda kv: -kv[0])   # top of staff first
    for (v1, y1), (v2, y2) in zip(seq, seq[1:]):
        if y2 <= y1:
            return None, None, f"marks out of order: {v1} at y={y1:.0f}, {v2} at y={y2:.0f}"

    n = len(seq)
    xs = [v for v, _ in seq]; ys = [y for _, y in seq]
    mx = sum(xs) / n; my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    px_per_ft = -b
    if px_per_ft <= 0:
        return None, None, "non-positive scale"

    for (v1, y1), (v2, y2) in zip(seq, seq[1:]):
        local = (y2 - y1) / (v1 - v2)
        if abs(local - px_per_ft) > 0.30 * px_per_ft:
            return None, None, (f"uneven spacing: {v1}->{v2} is {local:.0f} px/ft "
                                f"vs fit {px_per_ft:.0f}")

    try:
        wy = float(waterline_y)
    except (TypeError, ValueError):
        return None, px_per_ft, "no waterline"

    lowest_v, lowest_y = seq[-1]
    if wy < lowest_y - 0.10 * px_per_ft:
        return None, px_per_ft, (f"waterline y={wy:.0f} is above the {lowest_v} mark "
                                 f"at y={lowest_y:.0f}, which was reported dry")

    level = (wy - a) / b
    return level, px_per_ft, f"{n} marks, {px_per_ft:.0f} px/ft"


# -- Per-camera calibration ---------------------------------------------------
#
# The cameras do not move much, so from a given camera the staff occupies the
# same few columns in every frame and each whole-foot hashmark sits on a fixed
# row. With that pinned down, the waterline can be found from the pixels.
#
# Measured 2026-09-02 from a native-resolution copy of frame
# 865509053179515-20-4-09022026115308-W1004538.JPG: staff column x 349-358,
# staff top at row 232, numerals 6/5/4 readable at rows ~245/264/282, so
# 18.5 px per foot, whiteness cliff at rows 338-341 on a day the water was
# just under the 1 ft mark. Rows are believed good to about ±3 px, roughly
# ±0.15 ft. To refine: stand at the river, read the staff by eye at the
# moment a photo fires, and shift "marks" so the reading matches.
#
# CORRECTED 2026-11-14. Those rows were the centres of the painted numerals;
# the foot mark on this staff is the line at the BOTTOM of each numeral.
# ANCHORED the same day: Max read the staff by eye at 1.0 ft, on a day the
# USGS flow at Underwood held between 443 and 478 cfs. The surface rule
# (see detect_waterline) put the surface at calibration rows 340, 341, 342
# and 342 in the four frames of 2026-11-13/14, so the 1 ft mark is at their
# mean, 341.25, and the other marks follow at 18.5 px/ft. That is 2.75 px
# (0.15 ft) below the numeral centres, which makes the numerals about 0.3 ft
# tall with the foot line at their foot. Readings published between
# 2026-09-03 and 2026-11-14 (reader_version 2) are about 0.15 ft LOW; the
# single reader_version 3 run on 2026-11-14 was 0.1 ft HIGH.
#
# REGISTRATION (added 2026-11-13). Between 2026-09-15 and 2026-10-03 the
# camera's view moved up by 7 rows and stayed there, which put every reading
# about 0.4 ft high for six weeks. Each frame is now registered against the
# calibration frame before it is read: "ref_profile" is the brightness of the
# staff column over "ref_rows" (the dry upper staff, numerals 6 through 3) in
# the calibration frame, and the frame's own column is slid up and down to
# find the shift that best matches it. The calibration rows are moved by that
# shift. A frame whose best match correlates below "min_corr" is not the
# calibrated view (glare washing out the staff, camera knocked, lens blocked)
# and is not read.
#
# Rows and columns scale with frame size, so the 1920x1440 HD uploads work
# unchanged. Override with CALIBRATION_JSON in the environment.

DEFAULT_CALIBRATION = {
    "865509053179515": {
        "frame": [880, 660],
        "staff_x": [349, 358],
        "staff_top_y": 232,
        "scan_bottom_y": 430,
        # 1 ft at 341.25, anchored to a 1.0 ft reading by eye on 2026-11-14;
        # 18.5 px/ft from the 2026-09-02 numeral spacing.
        "marks": {"6": 248.75, "5": 267.25, "4": 285.75, "3": 304.25, "2": 322.75, "1": 341.25},
        # Grey-level mean of columns 351-357, rows 228-311, in the calibration
        # frame. The frame is registered to this before it is read.
        "ref_x": [351, 358],
        "ref_rows": [228, 312],
        "ref_profile": [
            64, 100, 104, 163, 137, 140, 141, 165, 154, 139, 112, 144, 179, 152,
            198, 137, 211, 187, 151, 187, 182, 221, 150, 163, 154, 158, 147, 176,
            182, 183, 218, 210, 207, 188, 228, 223, 195, 224, 222, 242, 176, 187,
            169, 191, 184, 168, 167, 180, 216, 197, 195, 200, 222, 234, 211, 187,
            195, 243, 198, 200, 209, 213, 214, 210, 218, 192, 211, 221, 239, 229,
            241, 242, 234, 239, 241, 243, 227, 223, 226, 227, 225, 220, 217, 221,
        ],
        "min_corr": 0.80,
    },
    # The Truss camera, installed 2026-11-14. It transmits 1280x720 (about
    # 25.6 px per foot of staff) at JPEG quality ~54; its "HD" request is a
    # 3840x2160 upscale at quality ~15 and the rows below scale to it. The
    # numerals are not resolvable at either size, so the marks come from two
    # fixed references tied to the old camera's calibration on the same day:
    # the top of the staff board (row 223 here) and the centre of the white
    # stripe painted across the rock (row 313 here), 3.52 ft apart on the old
    # camera, so 25.6 px/ft. ANCHORED 2026-11-14 to Max's 1.0 ft reading by
    # eye: the surface rule put the surface at rows 375, 375, 375, 374, 374
    # in the five clean frames that afternoon (two more sat on the top of
    # the wet band, as the old camera also does), so the 1 ft mark is at
    # 374.6. On that calibration the board top is 6.92 ft and the stripe
    # 3.41 ft; the old camera's say 6.91 and 3.39. Calibration frame:
    # 016578004123334-100-4-11142026130143-SYFW00141.jpg. Frames from before
    # the final aim that day (setup shots) correlate below 0.66.
    "016578004123334": {
        "frame": [1280, 720],
        "staff_x": [544, 552],
        "staff_top_y": 223,
        "scan_bottom_y": 420,
        "marks": {"6": 246.6, "5": 272.2, "4": 297.8, "3": 323.4, "2": 349.0, "1": 374.6},
        "ref_x": [545, 552],
        "ref_rows": [219, 305],
        "ref_profile": [
            111, 125, 118, 115, 140, 188, 153, 155, 148, 145, 153, 144, 152, 154,
            157, 159, 167, 193, 222, 237, 237, 188, 224, 241, 240, 237, 241, 238,
            226, 246, 235, 142, 173, 183, 173, 164, 167, 194, 199, 196, 189, 196,
            221, 242, 251, 243, 227, 236, 250, 244, 243, 251, 250, 242, 253, 229,
            185, 190, 199, 190, 191, 198, 189, 174, 172, 188, 205, 217, 226, 220,
            220, 219, 227, 245, 248, 228, 203, 213, 207, 244, 246, 209, 216, 226,
            229, 226,
        ],
        "min_corr": 0.80,
    },
}

def load_calibration():
    raw = os.environ.get("CALIBRATION_JSON", "").strip()
    if not raw:
        return DEFAULT_CALIBRATION
    try:
        return json.loads(raw)
    except ValueError as e:
        log.warning("CALIBRATION_JSON is not valid JSON (%s) — using defaults", e)
        return DEFAULT_CALIBRATION

CALIBRATION = load_calibration()

# How far (in calibration-frame rows) the staff may have shifted and still be found.
REGISTER_SLACK = int(os.environ.get("REGISTER_SLACK", "12"))


def calibration_for(camera, frame_size):
    """
    Calibration for this camera scaled to this frame, or None. Returns a dict
    with the fitted (a, b) such that frame_y = a + b*value, plus the scaled
    staff column and scan rows. Rows are for the calibration frame's view;
    apply register_frame() and shift_calibration() before reading a frame.
    """
    cal = CALIBRATION.get(camera) if camera else None
    if not cal or not cal.get("marks"):
        return None
    fw, fh = cal.get("frame", [0, 0])
    kx = frame_size[0] / float(fw) if fw else 1.0
    ky = frame_size[1] / float(fh) if fh else 1.0

    pts = sorted((float(v), float(y) * ky) for v, y in cal["marks"].items())
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(v for v, _ in pts) / n; my = sum(y for _, y in pts) / n
    sxx = sum((v - mx) ** 2 for v, _ in pts)
    sxy = sum((v - mx) * (y - my) for v, y in pts)
    b = sxy / sxx
    a = my - b * mx

    sx = cal.get("staff_x")
    return {
        "a": a, "b": b, "px_per_ft": -b, "ky": ky, "kx": kx,
        "staff_x": (round(sx[0] * kx), round(sx[1] * kx)) if sx else None,
        "staff_top_y": round(cal["staff_top_y"] * ky) if cal.get("staff_top_y") is not None else None,
        "scan_bottom_y": round(cal["scan_bottom_y"] * ky) if cal.get("scan_bottom_y") is not None else None,
        "lowest_mark": min(v for v, _ in pts),
        "highest_mark": max(v for v, _ in pts),
        "ref_profile": cal.get("ref_profile"),
        "ref_x": cal.get("ref_x"),
        "ref_rows": cal.get("ref_rows"),
        "min_corr": float(cal.get("min_corr", 0.0)),
        "shift": 0.0,
        "corr": None,
    }


def level_at(cal, frame_y):
    return (float(frame_y) - cal["a"]) / cal["b"]


def register_frame(image_bytes, cal):
    """
    Vertical offset of this frame's staff from the calibration frame, in
    frame pixels, and the correlation of the match (None when the calibration
    carries no reference profile).

    The reference is the grey-level profile of the staff column over the dry
    upper staff. The frame's column is sampled at calibration-row spacing
    (so HD frames compare like for like), slid up to REGISTER_SLACK rows
    either way, and the shift with the highest normalised correlation wins.
    Glare on the wall drops the correlation to ~0.75; a normal frame scores
    0.87-0.99.
    """
    from io import BytesIO
    from PIL import Image

    ref = cal.get("ref_profile")
    if not ref or not cal.get("ref_x") or not cal.get("ref_rows"):
        return 0.0, None
    img = Image.open(BytesIO(image_bytes)).convert("L")
    p = img.load()
    W, H = img.size
    kx, ky = cal["kx"], cal["ky"]
    rx0, rx1 = (round(v * kx) for v in cal["ref_x"])
    ry0, ry1 = cal["ref_rows"]
    n = len(ref)
    slack = REGISTER_SLACK

    def column(y):
        return sum(p[x, y] for x in range(rx0, rx1)) / float(rx1 - rx0)

    prof = []
    for j in range(-slack, (ry1 - ry0) + slack):
        y = round((ry0 + j) * ky)
        prof.append(column(y) if 0 <= y < H else None)

    ma = sum(ref) / n
    va = sum((a - ma) ** 2 for a in ref) ** 0.5 or 1.0
    best = (-2.0, 0)
    for s in range(-slack, slack + 1):
        seg = prof[slack + s: slack + s + n]
        if len(seg) < n or any(v is None for v in seg):
            continue
        mb = sum(seg) / n
        vb = sum((b - mb) ** 2 for b in seg) ** 0.5 or 1.0
        c = sum((a - ma) * (b - mb) for a, b in zip(ref, seg)) / (va * vb)
        if c > best[0]:
            best = (c, s)
    return best[1] * ky, best[0]


def shift_calibration(cal, dy):
    """The calibration with every row moved by dy frame pixels."""
    out = dict(cal)
    out["a"] = cal["a"] + dy
    out["shift"] = dy
    for k in ("staff_top_y", "scan_bottom_y"):
        if cal.get(k) is not None:
            out[k] = round(cal[k] + dy)
    return out


# -- Waterline from the pixels -----------------------------------------------
#
# Whiteness per row = brightness minus saturation. The dry staff scores high,
# water and the staff's reflection score much lower, so the waterline is a
# downward step in the whiteness profile of the staff's column.
#
# Two profiles are kept: the MEAN over the staff's columns, and the CORE, the
# mean of the three brightest pixels in the row. They agree on a clean staff.
# They differ when the lower staff is wet or silt-stained (seen 2026-10-27
# through 2026-11-13, after rain and at high flow): the stain darkens most
# of the staff's width but a white core survives, so the mean drops at the
# top of the stain while the core only drops at the water. On 2026-11-10 the
# mean step at the top of the stain read 2.2 ft on a day the water was near
# 0.9, and held there for three days.
#
# Every downward step in either profile is a candidate. A candidate is the
# waterline only if:
#   * there is staff within 20 rows above it (the lower edge of the staff's
#     reflection is a step too, but only reflection sits above it);
#   * the rows just below it are water-dark (the top of a stain is followed
#     by stained staff, which is dimmer than clean staff but not water);
#   * nothing below it is ever full white for three rows running (a numeral
#     or hashmark is dark, then the staff is white again).
# The staff's reflection makes the profile dip at the surface and brighten
# again a few rows down. Of the candidates that pass, the highest one that
# is followed by that brightening wins: the top of a wet band passes the
# tests (a dark band is as dark as water) but has stained staff under it,
# while the surface below it has the reflection. With no confirmed
# candidate the highest passing one is used at no better than medium
# confidence, since dark water on an overcast day shows no reflection at
# all. If nothing passes, the highest candidate that failed only the
# water-dark test is used and marked as a wet band (low confidence); its
# level is then an upper bound.
#
# Known limit: on an overcast day with a wet band as dark as the water
# (2026-11-11) nothing in the pixels separates the two, and the reading is
# the top of the wet band. The staff needs cleaning when that happens.
#
# Verified 2026-11-13 over 202 frames from 2026-09-01 to 2026-11-13 against
# USGS flow at Underwood: see the notes in the commit that added this.

CAND_FLOOR = float(os.environ.get("CAND_FLOOR", "0.15"))      # candidate step, fraction of range
STAFF_ABOVE = float(os.environ.get("STAFF_ABOVE", "0.60"))    # staff within 20 rows above
WATER_BELOW = float(os.environ.get("WATER_BELOW", "0.25"))    # rows just below are water-dark
WHITE_BELOW = float(os.environ.get("WHITE_BELOW", "0.80"))    # no full-white run below
REFLECT_RISE = float(os.environ.get("REFLECT_RISE", "0.10"))  # brightening under the surface


def _whiteness(c):
    return (c[0] + c[1] + c[2]) / 3.0 - (max(c) - min(c))


def _smooth3(v):
    if len(v) < 3:
        return list(v)
    return [v[0]] + [(v[i - 1] + v[i] + v[i + 1]) / 3.0 for i in range(1, len(v) - 1)] + [v[-1]]


def _refs(sm):
    srt = sorted(sm)
    n = len(srt)
    staff_ref = srt[int(0.9 * (n - 1))]
    water_ref = srt[int(0.1 * (n - 1))]
    return staff_ref, water_ref, staff_ref - water_ref


def _step_candidates(sm, up, down, floor, nb=3):
    steps = [(sum(sm[i - up:i]) / up - sum(sm[i + 1:i + 1 + down]) / down, i)
             for i in range(up, len(sm) - down)]
    out = []
    for k, (s, i) in enumerate(steps):
        if s < floor:
            continue
        if all(s >= steps[j][0] for j in range(max(0, k - nb), min(len(steps), k + nb + 1))):
            out.append((s, i))
    return out


def detect_waterline(image_bytes, cal, debug=None):
    """
    Find the waterline from the pixels in the staff's column. See the notes
    above. cal must already be registered to the frame.

    Returns (frame_y, detail, quality, flags) or (None, reason, 0.0, {}).
    quality is the chosen step as a fraction of the profile's range, scaled
    down when the reflection is missing or the rows below the step look like
    wet staff rather than water; flags carries "reflection" and "band".
    debug, if a list, receives every judged candidate (for offline study).
    """
    from io import BytesIO
    from PIL import Image

    if not cal or not cal.get("staff_x") or cal.get("staff_top_y") is None:
        return None, "no staff column in calibration", 0.0, {}

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    W, H = img.size
    x0, x1 = cal["staff_x"]
    y0 = max(0, cal["staff_top_y"])
    y1 = min(H, cal["scan_bottom_y"] or H)
    if x1 - x0 < 2 or y1 - y0 < 30:
        return None, "staff column out of frame", 0.0, {}
    p = img.load()
    ky = cal["ky"]

    def whiteness_rows(bx0, bx1):
        return [sum(_whiteness(p[x, y]) for x in range(bx0, bx1)) / float(bx1 - bx0)
                for y in range(y0, y1)]

    rows = []
    core = []
    for y in range(y0, y1):
        ws = sorted((_whiteness(p[x, y]) for x in range(x0, x1)), reverse=True)
        rows.append(sum(ws) / len(ws))
        core.append(sum(ws[:3]) / len(ws[:3]))

    # The staff must stand out from the rock either side of it. If it does
    # not, the camera has moved or something is in the way, and any step we
    # find would be rock-meets-water, not staff-meets-water.
    gap = max(3, round(4 * ky))
    wide = max(6, round((x1 - x0) * 1.0))
    flank = []
    if x0 - gap - wide >= 0:
        flank += whiteness_rows(x0 - gap - wide, x0 - gap)
    if x1 + gap + wide <= W:
        flank += whiteness_rows(x1 + gap, x1 + gap + wide)
    if flank:
        fs = sorted(flank)
        flank_ref = fs[int(0.9 * (len(fs) - 1))]
        rs = sorted(rows)
        col_ref = rs[int(0.9 * (len(rs) - 1))]
        if col_ref - flank_ref < 0.15 * col_ref:
            return None, (f"staff column ({col_ref:.0f}) is no whiter than the rock beside it "
                          f"({flank_ref:.0f}) — camera moved or staff obscured"), 0.0, {}

    sm = _smooth3(rows)
    sc = _smooth3(core)
    n = len(sm)
    staff_ref, water_ref, rng = _refs(sm)
    if rng < 40:
        return None, (f"no contrast in staff column (staff {staff_ref:.0f}, water {water_ref:.0f}) "
                      f"— staff not where the calibration expects it, or fully submerged"), 0.0, {}
    c_staff, c_water, c_rng = _refs(sc)

    up = max(6, round(10 * ky))
    down = max(5, round(8 * ky))
    dark_n = max(4, round(6 * ky))
    refl_n = max(8, round(20 * ky))
    look_up = max(12, round(20 * ky))
    white_run = max(2, round(3 * ky))

    # Candidates from both profiles, merged within two rows.
    cands = {}
    for s, i in _step_candidates(sm, up, down, CAND_FLOOR * rng):
        cands[i] = max(cands.get(i, 0.0), s / rng)
    if c_rng >= 40:
        for s, i in _step_candidates(sc, up, down, CAND_FLOOR * c_rng):
            near = [j for j in cands if abs(j - i) <= 2]
            if near:
                j = near[0]
                cands[j] = max(cands[j], s / c_rng)
            else:
                cands[i] = s / c_rng
    if not cands:
        return None, (f"no clear step in profile (range {rng:.0f}) "
                      f"— glare on the water, or staff obscured"), 0.0, {}

    white_level = water_ref + WHITE_BELOW * rng
    look_far = max(look_up, round(1.5 * cal["px_per_ft"]))
    judged = []
    for i, strength in cands.items():
        above = max(sm[max(0, i - look_up):i])
        staff_above = above >= water_ref + STAFF_ABOVE * rng
        # Staff anywhere in the 1.5 ft above: true of the surface under a
        # wet band (band directly above, white staff above that), false of
        # an edge down in the reflection.
        staff_far_above = max(sm[max(0, i - look_far):i]) >= water_ref + STAFF_ABOVE * rng
        dz = sm[i + 1:i + 1 + dark_n]
        if len(dz) < 3:
            continue
        dark = statistics.median(dz)
        water_below = dark <= water_ref + WATER_BELOW * rng
        run = 0
        white_below = False
        for v in sm[i + 1:]:
            run = run + 1 if v >= white_level else 0
            if run >= white_run:
                white_below = True
                break
        rz = sm[i + 1 + dark_n:i + 1 + dark_n + refl_n]
        rise = (sum(rz) / len(rz) - dark) / rng if len(rz) >= 4 else None
        judged.append({
            "i": i, "strength": strength, "staff_above": staff_above,
            "staff_far_above": staff_far_above,
            "water_below": water_below, "white_below": white_below,
            "dark": (dark - water_ref) / rng, "rise": rise,
        })

    if debug is not None:
        debug.extend(dict(c, row=y0 + c["i"], level=level_at(cal, y0 + c["i"] + 1)) for c in judged)
    passing = [c for c in judged if c["staff_above"] and c["water_below"] and not c["white_below"]]
    # The surface's own signature: water-dark below, no white below, the
    # staff's reflection under it, and staff somewhere within 1.5 ft above.
    # It need not have staff in the 20 rows directly above, because a wet
    # band as dark as the water can sit there: on 2026-11-15 the Truss read
    # 2.0 ft on a river at 1.2 ft for exactly that reason, the band top
    # passing every test and the real surface 0.7 ft lower failing only
    # staff_above while showing a 0.3 reflection rise.
    confirmed = [c for c in judged if c["water_below"] and not c["white_below"]
                 and c["staff_far_above"] and c["rise"] is not None and c["rise"] >= REFLECT_RISE]
    band = False
    if confirmed:
        # The highest candidate the reflection confirms. The top of a wet
        # band passes the plain tests but has stained staff under it, not a
        # reflection; the surface has both.
        best = min(confirmed, key=lambda c: c["i"])
    elif passing:
        # No reflection anywhere (dark overcast water): the highest passing
        # candidate. The reflection's lower edge is always lower, so it
        # never wins by default.
        best = min(passing, key=lambda c: c["i"])
    else:
        fallback = [c for c in judged if c["staff_above"] and not c["white_below"]]
        if not fallback:
            return None, (f"no step looks like a surface ({len(judged)} candidates)"), 0.0, {}
        best = min(fallback, key=lambda c: c["i"])
        band = True

    i = best["i"]
    reflection = best["rise"] is not None and best["rise"] >= REFLECT_RISE
    water_lim = water_ref + WATER_BELOW * rng

    # A wet band as dark as the water hides the surface from the candidate
    # search: the step from band to water is too small to be a candidate, so
    # the band top is chosen with nothing confirming it. Below an unconfirmed
    # choice, look up to 1.5 ft down for the surface's own signature (a
    # strong bright-to-dark edge, the dark side water, the staff's reflection
    # brightening beneath it) and take the strongest such edge if there is one.
    if not reflection and not band:
        far = max(8, round(1.5 * cal["px_per_ft"]))
        found, found_edge = None, 0.0
        for j in range(i + dark_n, min(n - refl_n - dark_n - 2, i + far)):
            edge = max(rows[j - 2], rows[j - 1]) - min(rows[j], rows[j + 1])
            if edge < 0.3 * rng or min(rows[j], rows[j + 1]) > water_lim:
                continue
            dark_j = statistics.median(sm[j + 1:j + 1 + dark_n])
            rz = sm[j + 1 + dark_n:j + 1 + dark_n + refl_n]
            rise_j = (sum(rz) / len(rz) - dark_j) / rng if len(rz) >= 4 else None
            if rise_j is not None and rise_j >= REFLECT_RISE and edge > found_edge:
                found, found_edge = j, edge
        if found is not None:
            best = dict(best, i=found, rise=None, strength=found_edge / rng)
            i = found
            reflection = True

    # Refine to the surface itself. The candidate step is the top of the
    # transition, and a wet, darkened strip of board sits just above the
    # water (up to 0.3 ft of it from the Truss camera), so the step can land
    # anywhere in that strip. The surface shows as a bright row (the
    # specular line where the board meets the water) followed by the
    # darkest rows in the column. Take the lowest bright-to-dark edge within
    # 0.4 ft below the candidate that is at least 60% as strong as the
    # strongest and whose dark side is water. On 2026-11-14, a flat day read
    # at 1.0 ft by eye, this put the surface on the same row in all seven
    # Truss frames; the previous rule spread them over 0.27 ft.
    w_down = max(4, round(0.4 * cal["px_per_ft"]))
    lo, hi = max(2, i - 1), min(n - 2, i + w_down)
    edges = [(max(rows[j - 2], rows[j - 1]) - min(rows[j], rows[j + 1]), j)
             for j in range(lo, hi) if min(rows[j], rows[j + 1]) <= water_lim]
    if edges:
        strongest = max(e for e, _ in edges)
        ref = max(j for e, j in edges if e >= 0.6 * strongest)
    else:
        ref = i + 1
    frame_y = y0 + float(ref)

    quality = best["strength"]
    tags = []
    if reflection:
        tags.append("reflection below")
    else:
        quality *= 0.6
        tags.append("no reflection below")
    if band:
        quality *= 0.5
        tags.append("rows below are wet staff, not water — reading is an upper bound")
    detail = (f"whiteness staff {staff_ref:.0f} / water {water_ref:.0f}, step {best['strength']:.2f} "
              f"of range at row {frame_y:.0f}; {'; '.join(tags)}")
    return frame_y, detail, quality, {"reflection": reflection, "band": band}


def read_one(image_bytes, camera=None):
    """
    Read one photo. Returns a dict with the chosen level, how it was chosen,
    and everything that went into it, so a bad day is diagnosable.

    Calibrated camera: the frame is registered to the calibration frame, the
    waterline comes from the pixel profile and the level from the
    calibration. That is the answer; the model is not asked. A frame that
    does not match the calibrated view, or whose column has no readable
    step, raises ValueError and is skipped by read_consensus. It is not
    handed to the model: on this camera the model's readings have been off
    by a foot or more either way (5.25, 0.25 and -1.35 ft on the primary
    camera in early November 2026), and a wrong number at low confidence
    still moves the weighted median.

    Uncalibrated camera: locate + read + geometry, as before.
    """
    frame = image_size(image_bytes)
    cal = calibration_for(camera, frame)

    # ---- pixel path (calibrated cameras) ----
    if cal is not None:
        dy, corr = register_frame(image_bytes, cal)
        if corr is not None:
            if corr < cal["min_corr"]:
                raise ValueError(f"frame does not match the calibrated view "
                                 f"(correlation {corr:.2f} at shift {dy:+.0f} px)")
            log.info("    registered: shift %+.0f px, correlation %.2f", dy, corr)
        cal = shift_calibration(cal, dy)

        floor = cal["lowest_mark"] - 1.0
        ceiling = cal["highest_mark"] + 0.5
        px_y, px_detail, px_q, flags = detect_waterline(image_bytes, cal)
        if px_y is None:
            raise ValueError(f"pixel waterline unavailable: {px_detail}")
        px_level = level_at(cal, px_y)
        if not (floor <= px_level <= ceiling):
            raise ValueError(f"pixel level {px_level:.2f} outside {floor:.1f}..{ceiling:.1f} "
                             f"— {px_detail}")

        if flags.get("band"):
            conf = "low"
        elif px_q >= 0.42 and flags.get("reflection") and (corr is None or corr >= 0.88):
            conf = "high"
        elif px_q >= 0.20:
            conf = "medium"
        else:
            conf = "low"
        return {
            "level": round(max(px_level, 0.0), 2),
            "method": "pixels",
            "confidence": conf,
            "notes": f"waterline from the pixel profile at row {px_y:.0f} ({px_detail})",
            "pixel_level": round(px_level, 2),
            "pixel_waterline_y": round(px_y, 1),
            "pixel_detail": px_detail,
            "pixel_quality": round(px_q, 2),
            "reflection": bool(flags.get("reflection")),
            "wet_band": bool(flags.get("band")),
            "registration_shift": round(dy, 1),
            "registration_corr": round(corr, 3) if corr is not None else None,
            "frame": list(frame),
        }

    # ---- model path (uncalibrated cameras) ----
    box = None
    try:
        box = locate_staff(image_bytes)
    except Exception as e:
        log.warning("    locate pass failed (%s) — reading the full frame", e)

    crop_bytes, w, h, scale, origin, frame = crop_and_enlarge(image_bytes, box)
    system = READ_PROMPT_TEMPLATE.format(w=w, h=h)
    result = _ask(system, READ_USER_PROMPT, crop_bytes)

    if box is not None and not result.get("marks"):
        log.info("    crop showed no marks — retrying on the full frame")
        box = None
        crop_bytes, w, h, scale, origin, frame = crop_and_enlarge(image_bytes, None)
        system = READ_PROMPT_TEMPLATE.format(w=w, h=h)
        result = _ask(system, READ_USER_PROMPT, crop_bytes)

    model_level = result.get("level")
    try:
        model_level = float(model_level) if model_level is not None else None
    except (TypeError, ValueError):
        model_level = None

    geom_level, px_per_ft, reason = level_from_geometry(
        result.get("marks"), result.get("waterline_y"))

    conf = str(result.get("confidence", "")).strip().lower() or "unknown"
    n_marks = len([m for m in (result.get("marks") or []) if isinstance(m, dict)])
    lowest_mark = None
    try:
        lowest_mark = min(float(m["value"]) for m in result.get("marks") or [] if isinstance(m, dict))
    except (ValueError, KeyError, TypeError):
        pass

    if geom_level is not None and 0.0 <= geom_level <= 6.5:
        level, method = geom_level, "geometry"
        if n_marks < 3:
            conf = "low"
        if lowest_mark is not None and lowest_mark - geom_level > 1.6:
            conf = "low"
        if model_level is not None and abs(model_level - geom_level) > 0.4:
            conf = "low" if conf == "high" else conf
    elif model_level is not None:
        level, method = model_level, "model"
        conf = "low"
    else:
        raise ValueError(f"no usable level in response: {result!r}")

    return {
        "level": round(max(level, 0.0), 2),
        "method": method,
        "confidence": conf,
        "notes": result.get("notes", ""),
        "model_level": model_level,
        "geometry_level": round(geom_level, 2) if geom_level is not None else None,
        "px_per_ft": round(px_per_ft, 1) if px_per_ft else None,
        "geometry_note": reason,
        "marks": result.get("marks"),
        "waterline_y": result.get("waterline_y"),
        "crop_box": [round(v) for v in box] if box else None,
        "crop_scale": round(scale, 2),
        "frame": list(frame),
    }


# -- Consensus logic ----------------------------------------------------------

def round_tenth(x):
    return round(round(x / 0.1) * 0.1, 2)


def confidence_from_spread(spread, n):
    if n < 2:
        return "low"
    if spread <= 0.15:
        return "high"
    if spread <= 0.35:
        return "medium"
    return "low"


MIN_KEPT = int(os.environ.get("MIN_KEPT", "2"))

# A day-to-day change bigger than JUMP_FT while USGS flow changed by less
# than JUMP_FLOW_FRACTION is published at low confidence (see main()).
JUMP_FT = float(os.environ.get("JUMP_FT", "0.5"))
# Bumped when readings stop being comparable with the previous run's (a
# new detector, a new calibration). The jump check only compares like with
# like; the first run after a bump is never flagged against the old reader.
# 3: 2026-11-14, foot rows moved 0.25 ft down to the line under each numeral.
# 4: 2026-11-14, both cameras anchored to a 1.0 ft reading by eye, and the
#    surface refined to the bright-to-dark edge at the water.
READER_VERSION = 4
JUMP_FLOW_FRACTION = float(os.environ.get("JUMP_FLOW_FRACTION", "0.25"))

# The staff is watched by two cameras: 865509053179515, the original, and
# 016578004123334, the Truss (from 2026-11-14). The primary is the one the
# published number comes from while it delivers MIN_KEPT frames a run; the
# other calibrated camera is read and recorded every run and takes over when
# it does not.
PRIMARY_CAMERA = os.environ.get("PRIMARY_CAMERA", "865509053179515").strip()

# Cameras under SOURCE_PREFIX that are not at Husum at all. The scraper files
# every camera in the Reveal account under tactacam/Husum/, and 016578000423746
# is the Trout Creek camera: a different river, a different staff. Until
# 2026-11-14 the reader modelled it every run as a "second Husum camera that
# reads half a foot high". Its frames are not read.
IGNORE_CAMERAS = {c.strip() for c in os.environ.get("IGNORE_CAMERAS", "016578000423746").split(",") if c.strip()}

CONF_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def weighted_median(pairs):
    """
    pairs: [(value, weight)]. Median of the values under integer weights.
    When the weight splits exactly in half (two lows against one medium, say)
    the lower and upper medians differ and their mean is returned.
    """
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    lower = upper = None
    acc = 0
    for v, w in pairs:
        acc += w
        if lower is None and acc * 2 >= total:
            lower = v
        if acc * 2 > total:
            upper = v
            break
    if upper is None:
        upper = pairs[-1][0]
    return (lower + upper) / 2


def read_consensus(image_list):
    """
    Read each image independently and reduce to one number plus an error bar.

    The published level is a confidence-weighted median (high=3, medium=2,
    low=1) over the primary camera's frames. Nothing is discarded, but a clean
    midday frame outvotes a pre-dawn guess. The spread is taken over the
    medium-or-better frames when there are at least two, otherwise over all,
    so a single bad frame does not paint the whole day as uncertain.

    Every sample is recorded with camera id, method, both levels, scale, the
    marks and waterline the model reported, and the crop box, so any bad day
    can be traced to the step that went wrong.
    """
    samples = []

    for key, ts in image_list:
        filename = key.split("/")[-1]
        try:
            r = read_one(fetch_image_bytes(key), camera_id(key))
            sample = {
                "file": filename,
                "key": key,
                "camera": camera_id(key),
                "taken": ts.isoformat() if ts else None,
            }
            sample.update(r)
            samples.append(sample)
            log.info("  %s -> %.2f ft (%s, %s%s)", filename, r["level"], r["confidence"],
                     r["method"],
                     f", {r['px_per_ft']} px/ft" if r.get("px_per_ft") else "")
        except Exception as e:
            log.warning("  %s -> skipped: %s", filename, e)

    if not samples:
        raise RuntimeError("No valid readings obtained from any image")

    by_camera = {}
    for smp in samples:
        by_camera.setdefault(smp["camera"], []).append(smp)
    if len(by_camera) > 1:
        log.info("Per-camera medians: %s",
                 {c: round(statistics.median(x["level"] for x in v), 2)
                  for c, v in by_camera.items()})

    # Only calibrated cameras (method "pixels") can reach the published
    # number. An uncalibrated camera is read by the model and recorded so a
    # bad day is diagnosable, but its readings are not comparable: five of
    # them put 1.2 ft on the page the day the Truss went in, on a river at 0.9.
    primary = [smp for smp in samples if smp["camera"] == PRIMARY_CAMERA]
    calibrated = [smp for smp in samples if smp["method"] == "pixels"]
    used_primary = len(primary) >= MIN_KEPT
    if used_primary:
        counted = primary
        counted_camera = PRIMARY_CAMERA
    elif calibrated:
        counted = calibrated
        counted_camera = ",".join(sorted({smp["camera"] for smp in calibrated}))
        log.warning("Only %d usable frame(s) from primary camera %s — counting %d frame(s) "
                    "from calibrated camera(s) %s.", len(primary), PRIMARY_CAMERA,
                    len(calibrated), counted_camera)
    else:
        raise RuntimeError(
            f"no usable frames from a calibrated camera "
            f"({len(samples)} model-read frame(s) from other cameras are not published)")

    counted = sorted(counted, key=lambda x: x["level"])
    weighted = [(x["level"], CONF_WEIGHT.get(x["confidence"], 1)) for x in counted]
    median = round_tenth(weighted_median(weighted))

    good = [x for x in counted if x["confidence"] in ("high", "medium")]
    basis = good if len(good) >= 2 else counted
    levels = [x["level"] for x in basis]
    spread = round(max(levels) - min(levels), 2)

    closest = min(counted, key=lambda x: (abs(x["level"] - median),
                                          -CONF_WEIGHT.get(x["confidence"], 1)))

    log.info("Counted %d/%d (%s): %s -> weighted median %.1f ft (spread %.2f over %d)",
             len(counted), len(samples),
             "primary" if used_primary else f"calibrated: {counted_camera}",
             [(x["level"], x["confidence"][0]) for x in counted], median, spread, len(basis))

    return {
        "level": median,
        "readings": [x["level"] for x in counted],
        "samples": samples,
        "cameras": sorted(by_camera),
        "counted_camera": counted_camera,
        "low": round_tenth(min(levels)),
        "high": round_tenth(max(levels)),
        "spread": spread,
        "confidence": confidence_from_spread(spread, len(basis)),
        "notes": closest["notes"],
        # The frame that best represents the published number; the close-up
        # is drawn from it.
        "closest": closest,
    }


def reader_summary(result):
    """
    One plain sentence for the page about where the number came from. The
    technical detail stays in "notes" and "samples" for diagnosis.
    """
    counted = [s for s in result["samples"]
               if s["camera"] in result["counted_camera"].split(",")]
    n = len(result["readings"])
    taken = sorted(s["taken"] for s in counted if s.get("taken"))
    span = ""
    if taken:
        try:
            t0, t1 = datetime.fromisoformat(taken[0]), datetime.fromisoformat(taken[-1])
            if t0.date() == t1.date():
                span = t0.strftime(" taken %b %-d")
            else:
                span = f" taken {t0.strftime('%b %-d')} to {t1.strftime('%b %-d')}"
        except ValueError:
            pass
    text = f"Read from {n} photo{'s' if n != 1 else ''}{span}."
    if result["spread"] >= 0.15:
        text += f" The readings ranged {result['low']:.1f} to {result['high']:.1f} ft."
    return text


# -- Cross-check --------------------------------------------------------------

def fetch_usgs():
    """
    Concurrent USGS reading, recorded alongside ours. Not used to correct the
    staff reading — it is the independent signal that makes a stuck value
    detectable after the fact.
    """
    out = {}
    try:
        url = ("https://waterservices.usgs.gov/nwis/iv/"
               f"?sites={USGS_SITE}&parameterCd=00060,00065&period=PT6H&format=json")
        data = requests.get(url, timeout=20).json()
        for series in data["value"]["timeSeries"]:
            code = series["variable"]["variableCode"][0]["value"]
            values = series["values"][0]["value"]
            if not values:
                continue
            v = float(values[-1]["value"])
            if v <= -999:
                continue
            if code == "00060":
                out["usgs_cfs"] = v
            elif code == "00065":
                out["usgs_stage_ft"] = v
    except Exception as e:
        log.warning("USGS cross-check unavailable: %s", e)
    return out


# -- Previous reading / history ----------------------------------------------

def read_previous():
    """Fetch the existing gauge JSON from S3. Returns {} if absent."""
    try:
        obj = s3.get_object(Bucket=OUTPUT_BUCKET, Key=OUTPUT_KEY)
        return json.loads(obj["Body"].read())
    except Exception as e:
        log.warning("Could not read previous gauge data: %s", e)
        return {}


def append_history(entry):
    try:
        obj = s3.get_object(Bucket=OUTPUT_BUCKET, Key=HISTORY_KEY)
        history = json.loads(obj["Body"].read())
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    history.append(entry)
    history = history[-HISTORY_MAX:]

    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=HISTORY_KEY,
        Body=json.dumps(history, indent=2).encode("utf-8"),
        ContentType="application/json",
        CacheControl="no-cache, max-age=300",
    )
    log.info("Appended to s3://%s/%s (%d entries)", OUTPUT_BUCKET, HISTORY_KEY, len(history))


# -- Staff close-up -----------------------------------------------------------
#
# The numerals on the staff are not legible in what the Truss camera
# transmits, and are marginal from the old camera. The reader does not need
# them (it has the calibration), but a person checking the number does. So
# the frame the number came from is cut down to the staff, enlarged, and the
# calibrated foot marks are drawn on it along with the waterline the detector
# chose. Published beside the JSON; the page shows it next to the number.

CLOSEUP_KEY = os.environ.get("CLOSEUP_KEY", "gauge/husum-closeup.jpg")
CLOSEUP_HEIGHT = int(os.environ.get("CLOSEUP_HEIGHT", "1000"))
CLOSEUP_LOCAL = os.environ.get("CLOSEUP_LOCAL", "husum-closeup.jpg")   # DRY_RUN output


def level_color(level):
    """The page's level colours (see levelColor in site/appv6.js)."""
    if level < 2.5:
        return (34, 197, 94)
    if level < 4.0:
        return (59, 130, 246)
    if level < 6.0:
        return (249, 115, 22)
    return (239, 68, 68)


def level_mood(level):
    """The page's level words (see levelLabel in site/appv6.js)."""
    if level < 2.5:
        return "Chill"
    if level < 4.0:
        return "A Vibe"
    if level < 6.0:
        return "Spicy"
    return "Wildin'"


def render_closeup(image_bytes, cal, waterline_y, level, caption=""):
    """
    JPEG bytes: the staff cut out of the frame and enlarged, with a
    tape-measure ruler down the left side drawn from the calibration (every
    tenth of a foot, bigger at the half, a numeral above each whole-foot
    line, exactly as the staff itself is painted), the whole-foot lines
    carried across the photo so the ruler can be checked against the paint,
    the water tinted blue below the waterline the reader chose, and the
    level in a pill coloured the way the page colours it. cal must already
    be registered to the frame.

    Geometry: output row = CAP + (frame_row - top) * scale, so every line is
    placed from the same (a, b) fit the reading itself came from.
    """
    import math
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    W, H = img.size
    ky = cal["ky"]
    x0, x1 = cal["staff_x"]
    ppf = cal["px_per_ft"]

    def row(ft):
        return cal["a"] + cal["b"] * ft

    # From just above the top of the board to 0.7 ft under the surface.
    top = int(max(0, min(cal["staff_top_y"] - 8 * ky, row(cal["highest_mark"] + 0.85))))
    bottom = int(min(H, max(waterline_y + 0.7 * ppf, top + 30)))
    ch = max(bottom - top, 1)
    cw = max(int(ch * 0.40), (x1 - x0) * 4)
    cx = (x0 + x1) / 2.0
    left = int(max(0, cx - cw / 2.0))
    right = int(min(W, left + cw))
    left = max(0, right - cw)

    CAP = round(CLOSEUP_HEIGHT * 0.046)            # caption band
    PH = CLOSEUP_HEIGHT - CAP                      # photo height
    scale = PH / float(ch)
    RB = round(PH * 0.115)                         # ruler bar width
    pw = max(1, round((right - left) * scale))
    crop = ImageOps.autocontrast(img.crop((left, top, right, bottom)), cutoff=1)
    crop = crop.resize((pw, PH), Image.LANCZOS)

    canvas = Image.new("RGB", (RB + pw, CAP + PH), (15, 23, 42))
    canvas.paste(crop, (RB, CAP))
    d = ImageDraw.Draw(canvas)

    def font(px):
        try:
            return ImageFont.load_default(size=px)
        except TypeError:                           # Pillow < 10.1
            return ImageFont.load_default()

    f_num = font(round(RB * 0.58))
    f_big = font(round(PH * 0.05))
    f_mid = font(round(PH * 0.026))
    f_cap = font(round(CAP * 0.5))

    def Y(frame_y):
        return CAP + (frame_y - top) * scale

    def tsize(s, fnt):
        bx0, by0, bx1, by1 = d.textbbox((0, 0), s, font=fnt)
        return bx1 - bx0, by1 - by0, bx0, by0

    # The page's palette (site/stylesV2.css): page #0b1120, cards #0f172a,
    # slate borders, #e5e7eb text, #9ca3af muted, level colours for accents.
    NAVY, CARD = (11, 17, 32), (15, 23, 42)
    TEXT, MUTED, SLATE = (229, 231, 235), (156, 163, 175), (148, 163, 184)
    col = level_color(level)

    # Ruler bar: a dark card with slate graduations and light numerals. Each
    # numeral sits on top of its foot line, exactly as the paint on the staff
    # does (the foot mark is at the bottom of the number).
    d.rectangle([0, CAP, RB - 1, CAP + PH], fill=CARD)
    d.line([(RB - 1, CAP), (RB - 1, CAP + PH)], fill=SLATE, width=1)
    top_tenth = int(round((cal["highest_mark"] + 1.0) * 10))
    for tenth in range(0, top_tenth + 1):
        ft = tenth / 10.0
        y = Y(row(ft))
        if not (CAP <= y < CAP + PH):
            continue
        if tenth % 10 == 0:
            d.line([(0, y), (RB, y)], fill=TEXT, width=3)
            label = str(tenth // 10)
            tw, th, ox, oy = tsize(label, f_num)
            ty = y - th - oy - 5
            if ty >= CAP:
                d.text(((RB - 24 - tw) / 2 - ox, ty), label, font=f_num, fill=TEXT,
                       stroke_width=1, stroke_fill=TEXT)
            # the same line carried across the photo, outlined so it reads on
            # white staff and dark rock alike
            d.line([(RB, y), (canvas.width, y)], fill=NAVY, width=4)
            d.line([(RB, y), (canvas.width, y)], fill=TEXT, width=2)
        elif tenth % 5 == 0:
            d.line([(RB - round(RB * 0.42), y), (RB, y)], fill=TEXT, width=3)
        else:
            d.line([(RB - round(RB * 0.2), y), (RB, y)], fill=SLATE, width=2)

    # Water: tint everything below the surface, ruler included, in the page's
    # blue, with a few waves.
    wy = Y(waterline_y)
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([0, wy, canvas.width, canvas.height], fill=(59, 130, 246, 88))
    for k in range(3):
        yy = wy + 16 + k * 18
        pts = [(x, yy + 4 * math.sin(x / 16.0 + k * 1.3)) for x in range(RB, canvas.width + 3, 3)]
        od.line(pts, fill=(191, 219, 254, 120 - k * 30), width=2)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    d = ImageDraw.Draw(canvas)

    # The waterline, in the page's colour for this level, with a pointer on the ruler.
    d.line([(0, wy), (canvas.width, wy)], fill=NAVY, width=8)
    d.line([(0, wy), (canvas.width, wy)], fill=col, width=4)
    d.polygon([(RB - 2, wy), (RB - 20, wy - 11), (RB - 20, wy + 11)], fill=NAVY)
    d.polygon([(RB - 4, wy), (RB - 17, wy - 7), (RB - 17, wy + 7)], fill=col)

    # Level pill, styled like the page's badge: dark card, coloured border,
    # the level in the level colour and the page's word for it beneath.
    big = f"{level:.1f} ft"
    mood = level_mood(level)
    bw, bh, bx, by = tsize(big, f_big)
    mw, mh, mx, my = tsize(mood, f_mid)
    pad = round(PH * 0.014)
    pill_w = max(bw, mw) + 2 * pad + 8
    pill_h = bh + mh + 2 * pad + 8
    px = canvas.width - pill_w - 10
    py = wy - pill_h - 12
    if py < CAP + 4:
        py = wy + 12
    d.rounded_rectangle([px, py, px + pill_w, py + pill_h], radius=round(pill_h * 0.3),
                        fill=CARD, outline=col, width=2)
    d.text((px + (pill_w - bw) / 2 - bx, py + pad - by), big, font=f_big, fill=col,
           stroke_width=1, stroke_fill=col)
    d.text((px + (pill_w - mw) / 2 - mx, py + pad + bh + 6 - my), mood, font=f_mid, fill=TEXT)

    # Caption band: the wordmark and when the frame was taken.
    d.rectangle([0, 0, canvas.width, CAP], fill=NAVY)
    wm = "GORGEGAUGE"
    ww, wh, wox, woy = tsize(wm, f_cap)
    d.text((10 - wox, (CAP - wh) / 2 - woy), wm, font=f_cap, fill=TEXT, stroke_width=1, stroke_fill=TEXT)
    sub = "Husum staff" + (f" · {caption}" if caption else "")
    sw_, shh, sox, soy = tsize(sub, f_cap)
    if 10 + ww + 24 + sw_ + 10 > canvas.width and caption:
        sub = caption                      # not room for both; the time matters more
        sw_, shh, sox, soy = tsize(sub, f_cap)
    d.text((canvas.width - sw_ - 10 - sox, (CAP - shh) / 2 - soy), sub, font=f_cap, fill=MUTED)

    out = BytesIO()
    canvas.save(out, format="JPEG", quality=90)
    return out.getvalue()


def build_closeup(sample):
    """
    Close-up fields for the payload, rendered from the sample the published
    number is closest to. Only a pixel-path sample has the registered
    calibration and waterline this needs; anything else yields {}.
    """
    if not sample or sample.get("method") != "pixels" or not sample.get("key"):
        return {}
    try:
        image_bytes = fetch_image_bytes(sample["key"])
        cal = calibration_for(sample["camera"], image_size(image_bytes))
        cal = shift_calibration(cal, float(sample.get("registration_shift") or 0.0))
        when = ""
        if sample.get("taken"):
            try:
                when = datetime.fromisoformat(sample["taken"]).strftime("%b %-d, %-I:%M %p")
            except ValueError:
                when = sample["taken"]
        caption = when
        jpeg = render_closeup(image_bytes, cal, float(sample["pixel_waterline_y"]),
                              float(sample["level"]), caption)
    except Exception as e:
        log.warning("Close-up not rendered: %s", e)
        return {}

    if DRY_RUN:
        with open(CLOSEUP_LOCAL, "wb") as f:
            f.write(jpeg)
        log.info("DRY_RUN — close-up written to %s", CLOSEUP_LOCAL)
    else:
        s3.put_object(
            Bucket=OUTPUT_BUCKET,
            Key=CLOSEUP_KEY,
            Body=jpeg,
            ContentType="image/jpeg",
            CacheControl="no-cache, max-age=60",
        )
        log.info("Wrote s3://%s/%s", OUTPUT_BUCKET, CLOSEUP_KEY)
    return {
        "closeup_url": "/" + CLOSEUP_KEY,
        "closeup_frame": sample["file"],
        "closeup_camera": sample["camera"],
        "closeup_level": sample["level"],
        "closeup_taken": sample.get("taken"),
    }


# -- Output -------------------------------------------------------------------

def update_preview_image(source_key):
    try:
        s3.copy_object(
            Bucket=OUTPUT_BUCKET,
            CopySource={"Bucket": SOURCE_BUCKET, "Key": source_key},
            Key="husum-preview.jpg",
            ContentType="image/jpeg",
            MetadataDirective="REPLACE",
            CacheControl="max-age=3600",
        )
        log.info("Updated og:image -> s3://%s/husum-preview.jpg", OUTPUT_BUCKET)
    except Exception as e:
        log.warning("Could not update preview image: %s", e)


def write_gauge_json(payload):
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=OUTPUT_KEY,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
        CacheControl="no-cache, max-age=60",
    )
    log.info("Wrote s3://%s/%s", OUTPUT_BUCKET, OUTPUT_KEY)


# -- Entry point --------------------------------------------------------------

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required")

    if DRY_RUN:
        log.info("DRY_RUN is set — nothing will be written to S3.")

    keys = list_s3_images()
    if not keys:
        raise RuntimeError(f"No images found under {SOURCE_PREFIX}")

    previous = read_previous()
    prev_stamp = previous.get("newest_image_stamp")
    if REREAD and prev_stamp:
        log.info("REREAD is set — ignoring previous cutoff %s", prev_stamp)
        prev_stamp = None

    window = select_window(keys, prev_stamp, IMAGE_COUNT)
    if not window:
        log.warning(
            "No photos newer than %s. Camera may be down. Leaving %s unchanged so the "
            "page shows a stale read time rather than a fresh-looking repeat.",
            prev_stamp, OUTPUT_KEY,
        )
        return

    log.info("Analyzing %d image(s), newest first:", len(window))
    for k, ts in window:
        log.info("  %s  (%s)", k.split("/")[-1], ts)

    try:
        result = read_consensus(window)
    except RuntimeError as e:
        log.warning("%s. Leaving %s unchanged so the page shows a stale read time "
                    "rather than a number from an uncalibrated camera.", e, OUTPUT_KEY)
        return

    prev_level = previous.get("level")
    if prev_level is not None:
        log.info("Previous level: %.1f ft -> current: %.1f ft (delta %+.2f)",
                 prev_level, result["level"], result["level"] - prev_level)

    latest_key, photo_ts = window[0]
    newest_stamp = photo_ts.strftime(STAMP_FMT)
    now_pacific = datetime.now(PACIFIC)

    payload = {
        "level": result["level"],
        "low": result["low"],
        "high": result["high"],
        "spread": result["spread"],
        "confidence": result["confidence"],
        "previous_level": prev_level,
        "previous_read_at_iso": previous.get("read_at_iso"),
        "readings": result["readings"],
        "samples": result["samples"],
        "cameras": result["cameras"],
        "counted_camera": result["counted_camera"],
        "images_analyzed": len(result["readings"]),
        "images_read": len(result["samples"]),
        "notes": result["notes"],
        "image_url": image_url_for_key(latest_key),
        "image_key": latest_key,
        "newest_image_stamp": newest_stamp,
        "photo_timestamp": photo_ts.strftime("%b %d, %Y at %-I:%M %p") if photo_ts else None,
        "read_at": now_pacific.strftime("%b %d, %Y at %-I:%M %p PT"),
        "read_at_iso": now_pacific.isoformat(),
        "reader_version": READER_VERSION,
    }
    payload.update(fetch_usgs())
    payload.update(build_closeup(result.get("closest")))
    payload["summary"] = reader_summary(result)

    # A real jump at Husum shows up at Underwood too. A big change in the
    # staff reading while the USGS flow barely moved is the signature of a
    # wet band on the staff being read as the waterline (2026-11-10), so it
    # is published at low confidence rather than as fact. The comparison is
    # against the last reading that was not itself low confidence (the
    # "baseline"), so a band that lasts several days stays flagged until
    # the flow agrees with the staff again.
    base_level = previous.get("baseline_level", prev_level)
    base_cfs = previous.get("baseline_cfs", previous.get("usgs_cfs"))
    cfs = payload.get("usgs_cfs")
    comparable = previous.get("reader_version") == READER_VERSION
    if comparable and base_level is not None and base_cfs and cfs:
        jump = payload["level"] - base_level
        flow_change = cfs / base_cfs - 1.0
        if abs(jump) > JUMP_FT and abs(flow_change) < JUMP_FLOW_FRACTION:
            log.warning("Level moved %+.2f ft from the %.1f ft baseline while USGS flow moved "
                        "%+.0f%% — publishing at low confidence.", jump, base_level, flow_change * 100)
            payload["confidence"] = "low"
            payload["notes"] = (f"{payload['notes']} | level moved {jump:+.2f} ft from {base_level:.1f} "
                                f"while USGS flow moved {flow_change:+.0%}; a wet band on the staff "
                                f"may be reading as the waterline")
    if payload["confidence"] in ("high", "medium") and cfs:
        payload["baseline_level"] = payload["level"]
        payload["baseline_cfs"] = cfs
        payload["baseline_read_at_iso"] = payload["read_at_iso"]
    else:
        for k in ("baseline_level", "baseline_cfs", "baseline_read_at_iso"):
            if previous.get(k) is not None:
                payload[k] = previous[k]

    if DRY_RUN:
        log.info("DRY_RUN — would have written husumGauge.json and appended history.")
        print(json.dumps(payload, indent=2))
        return

    write_gauge_json(payload)
    append_history({
        "read_at_iso": payload["read_at_iso"],
        "level": payload["level"],
        "low": payload["low"],
        "high": payload["high"],
        "spread": payload["spread"],
        "confidence": payload["confidence"],
        "readings": payload["readings"],
        "samples": payload["samples"],
        "cameras": payload["cameras"],
        "image_key": payload["image_key"],
        "usgs_cfs": payload.get("usgs_cfs"),
        "usgs_stage_ft": payload.get("usgs_stage_ft"),
    })
    update_preview_image(latest_key)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
