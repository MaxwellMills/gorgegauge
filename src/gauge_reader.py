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
"""

import os
import re
import json
import base64
import logging
import statistics
from datetime import datetime
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

IMAGE_COUNT    = int(os.environ.get("IMAGE_COUNT", "5"))
MIN_IMAGES     = int(os.environ.get("MIN_IMAGES", "2"))
HISTORY_MAX    = int(os.environ.get("HISTORY_MAX", "730"))

# Independent cross-check: USGS White Salmon River nr Underwood, WA.
USGS_SITE      = os.environ.get("USGS_SITE", "14123500")

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")

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


def select_window(keys, prev_stamp, n):
    """
    Photos to read this run, newest first.

    Only photos newer than the previous run's newest photo are used, so two
    consecutive runs never share an input image. prev_stamp is the raw
    MMDDYYYYHHMMSS string carried forward in husumGauge.json, which sidesteps
    any timezone comparison between camera time and run time.
    """
    dated = [(k, parse_timestamp(k)) for k in keys]
    dated = [(k, t) for k, t in dated if t]
    dated.sort(key=lambda x: x[1], reverse=True)

    if not dated:
        return []

    cutoff = None
    if prev_stamp:
        try:
            cutoff = datetime.strptime(prev_stamp, STAMP_FMT)
        except (ValueError, TypeError):
            log.warning("Unparseable previous stamp %r — falling back to newest %d.",
                        prev_stamp, n)

    if cutoff is None:
        return dated[:n]

    fresh = [(k, t) for k, t in dated if t > cutoff]

    if len(fresh) >= MIN_IMAGES:
        return fresh[:n]

    if not fresh:
        return []

    # One new photo on its own is a thin sample. Top up to MIN_IMAGES with the
    # newest already-seen photos, and say so in the log.
    older = [(k, t) for k, t in dated if t <= cutoff][:MIN_IMAGES - len(fresh)]
    log.warning("Only %d new photo(s) since %s — topping up with %d already-read one(s).",
                len(fresh), prev_stamp, len(older))
    return (fresh + older)[:n]


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
    "0.25 ft between them. Whole-foot numbers run 6 at the top down to 1 at the bottom. Each "
    "number is printed just ABOVE the wide hashmark it names. The spacing between whole-foot "
    "hashmarks is identical all the way down the staff. "

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
    (jpeg_bytes, width, height, scale). Falls back to the full frame if the
    box is degenerate.
    """
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(image_bytes)).convert("RGB")
    W, H = img.size

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
            scale = max(1.0, min(CROP_MAX_SCALE, CROP_TARGET_H / crop.height))

    if scale > 1.0:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)),
                           Image.LANCZOS)

    out = BytesIO()
    crop.save(out, format="JPEG", quality=92)
    return out.getvalue(), crop.width, crop.height, scale


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


def read_one(image_bytes):
    """
    Read one photo. Returns a dict with the chosen level, how it was chosen,
    and everything the model reported, so a bad day is diagnosable.
    """
    box = None
    try:
        box = locate_staff(image_bytes)
    except Exception as e:
        log.warning("    locate pass failed (%s) — reading the full frame", e)

    crop_bytes, w, h, scale = crop_and_enlarge(image_bytes, box)
    system = READ_PROMPT_TEMPLATE.format(w=w, h=h)
    result = _ask(system, READ_USER_PROMPT, crop_bytes)

    # A crop that misses the staff comes back with no marks. Try once more on
    # the whole frame before giving up on the photo.
    if box is not None and not result.get("marks"):
        log.info("    crop showed no marks — retrying on the full frame")
        box = None
        crop_bytes, w, h, scale = crop_and_enlarge(image_bytes, None)
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
        # Two marks give a scale but no redundancy: a single mislabeled mark
        # shifts the answer by a whole foot with nothing to catch it. That is
        # what a 0.02 ft reading from two marks looked like on 2026-09-02.
        if n_marks < 3:
            conf = "low"
        # If the water is far below the lowest mark the model could read, it
        # most likely skipped a readable mark or mislabeled one.
        if lowest_mark is not None and lowest_mark - geom_level > 1.6:
            log.info("    level %.2f is %.1f ft below the lowest read mark (%d) — capping confidence",
                     geom_level, lowest_mark - geom_level, lowest_mark)
            conf = "low"
        if model_level is not None and abs(model_level - geom_level) > 0.4:
            # The model's arithmetic disagrees with its own coordinates. Trust
            # the coordinates, but say so and knock the confidence down.
            log.info("    model said %.2f, geometry says %.2f (%s) — using geometry",
                     model_level, geom_level, reason)
            conf = "low" if conf == "high" else conf
    elif model_level is not None:
        level, method = model_level, "model"
        conf = "low"
        log.info("    geometry unusable (%s) — falling back to model estimate %.2f",
                 reason, model_level)
    else:
        raise ValueError(f"no usable level in response: {result!r}")

    return {
        "level": round(level, 2),
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

# The staff is watched by two cameras. 865509053179515 is the close one and
# agrees with ground truth; 016578000423746 has read about half a foot high on
# three consecutive days from a different angle. The secondary is still read
# and recorded every run, but only counts toward the published number when the
# primary produced fewer than MIN_KEPT usable frames.
PRIMARY_CAMERA = os.environ.get("PRIMARY_CAMERA", "865509053179515").strip()

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
            r = read_one(fetch_image_bytes(key))
            sample = {
                "file": filename,
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

    primary = [smp for smp in samples if smp["camera"] == PRIMARY_CAMERA]
    used_primary = len(primary) >= MIN_KEPT
    if used_primary:
        counted = primary
    elif primary:
        counted = samples
        log.warning("Only %d usable frame(s) from primary camera %s — counting all cameras.",
                    len(primary), PRIMARY_CAMERA)
    else:
        counted = samples
        log.warning("No usable frames from primary camera %s — counting all cameras.",
                    PRIMARY_CAMERA)

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
             "primary" if used_primary else "all cameras",
             [(x["level"], x["confidence"][0]) for x in counted], median, spread, len(basis))

    return {
        "level": median,
        "readings": [x["level"] for x in counted],
        "samples": samples,
        "cameras": sorted(by_camera),
        "counted_camera": PRIMARY_CAMERA if used_primary else "all",
        "low": round_tenth(min(levels)),
        "high": round_tenth(max(levels)),
        "spread": spread,
        "confidence": confidence_from_spread(spread, len(basis)),
        "notes": closest["notes"],
    }


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

    result = read_consensus(window)

    prev_level = previous.get("level")
    if prev_level is not None:
        log.info("Previous level: %.1f ft -> current: %.1f ft (delta %+.2f)",
                 prev_level, result["level"], result["level"] - prev_level)

    latest_key, photo_ts = window[0]
    newest_stamp = parse_stamp(latest_key)[1]
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
    }
    payload.update(fetch_usgs())

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
