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

SYSTEM_PROMPT = (
    "You are reading a river staff gauge at Husum Falls on the White Salmon River, WA. "

    "PHYSICAL LAYOUT: "
    "A white staff mounted against the rock face, running down into the water. "
    "Wide horizontal hashmarks at each whole foot, narrow hashmarks every 0.25 ft. "
    "Whole-foot numbers run 6 at the top down to 1 at the bottom. "
    "Each number label is printed just ABOVE the hashmark it belongs to. "

    "WHAT A VISIBLE LABEL DOES AND DOES NOT TELL YOU: "
    "Because a label sits above its hashmark, it stays dry and readable even after the "
    "water has dropped well below that mark. So seeing the '2' above the waterline means "
    "only that the water is somewhere below the top of the '2' label. It is an UPPER bound. "
    "It establishes NO lower bound. Never treat a visible label as a floor. Readings below "
    "the lowest visible number are normal and expected at low summer flow. "

    "METHOD - measure downward from the mark above the water: "
    "1. Find the waterline: where the river surface crosses the staff. "
    "2. Find the nearest WIDE (whole-foot) hashmark ABOVE the waterline. It is dry and "
    "   clearly visible. Call its value N, which is the number printed just above it. "
    "3. The next wide hashmark down is N-1. It may be underwater, obscured, or past the "
    "   bottom of the staff. If you cannot see it, use the spacing between the wide marks "
    "   higher up the staff, which is constant. "
    "4. Estimate how far BELOW the N hashmark the waterline sits, as a fraction of that "
    "   one-foot spacing. 0% means right at the N mark, 100% means down at the N-1 mark. "
    "5. level = N - fraction. "
    "   Example: nearest dry whole-foot mark is 4, waterline sits 30% of a foot below it "
    "   -> 3.7 ft. "
    "   Example: nearest dry whole-foot mark is 2, waterline sits 40% of a foot below it "
    "   -> 1.6 ft. "
    "6. Check: your answer must be less than N and greater than N-1. "

    "Measure against the HASHMARKS, not the printed numbers. The numbers only tell you "
    "which hashmark is which. "

    "OFF THE BOTTOM: if the waterline is below the lowest hashmark on the staff, return "
    "that lowest hashmark value, set confidence to \"low\", and say in the notes that the "
    "water is off the bottom of the scale. "

    "PERSPECTIVE: the staff is not perfectly vertical in the frame and is viewed from an "
    "angle across the river. Measure along the staff itself, not against image horizontal. "

    "RESOLUTION: the photo is 880x660 and the staff spans roughly 30 pixels per foot, so "
    "0.1 ft is about 3 pixels. Report to the nearest 0.1 ft and claim no more precision "
    "than that. Use confidence \"low\" when glare, foam, shadow or reflection break up the "
    "waterline, and \"high\" only when the waterline crosses the staff cleanly. "

    "PLAUSIBLE RANGE: 0.5 to 6.0 ft. Only re-examine if you land outside that. "

    "Respond ONLY with raw JSON, no markdown: "
    "{\"level\": 3.7, \"confidence\": \"medium\", \"notes\": \"4 ft hashmark is the nearest dry "
    "whole-foot mark; waterline sits about 30% of a foot below it\"}"
)

USER_PROMPT = (
    "Read the gauge. Work downward from the nearest whole-foot hashmark above the "
    "waterline. A visible number label is an upper bound, never a floor. Return JSON only."
)


def ask_claude(image_bytes):
    """Ask Claude to read the gauge from one image. Returns parsed dict."""
    client = anthropic.Anthropic()
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                },
                {"type": "text", "text": USER_PROMPT},
            ],
        }],
    )

    text = message.content[0].text.strip()
    match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in response: {text!r}")
    return json.loads(match.group())


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


def read_consensus(image_list):
    """
    Read each image independently and reduce to a median plus an error bar.

    The spread across images taken minutes apart is measurement noise, not real
    river movement, so it gets published rather than averaged away.

    Frames the model itself flags "low" are dropped when at least MIN_KEPT
    others survive. In practice those are the pre-dawn and low-sun frames, where
    the waterline is guesswork — on 2026-09-02 the three low-confidence frames
    read 1.2-1.3 while the daylight ones read 1.4-1.8. Every sample is still
    recorded, so nothing is silently discarded.

    Camera id is recorded per sample. Two cameras watch this staff from
    different angles and appear to disagree by a few tenths; keeping the id
    makes that bias measurable in the history instead of invisible.
    """
    samples = []

    for key, ts in image_list:
        filename = key.split("/")[-1]
        try:
            result = ask_claude(fetch_image_bytes(key))
            level = float(result["level"])
            conf = str(result.get("confidence", "")).strip().lower()
            samples.append({
                "file": filename,
                "camera": camera_id(key),
                "taken": ts.isoformat() if ts else None,
                "level": level,
                "confidence": conf or "unknown",
                "notes": result.get("notes", ""),
            })
            log.info("  %s -> %.2f ft (%s)", filename, level, conf or "?")
        except Exception as e:
            log.warning("  %s -> skipped: %s", filename, e)

    if not samples:
        raise RuntimeError("No valid readings obtained from any image")

    kept = [s for s in samples if s["confidence"] != "low"]
    if len(kept) < MIN_KEPT:
        kept = samples
        if any(s["confidence"] == "low" for s in samples):
            log.warning("Too few confident frames to filter — keeping all %d.", len(samples))
    elif len(kept) < len(samples):
        log.info("Dropped %d low-confidence frame(s): %s",
                 len(samples) - len(kept),
                 [s["file"] for s in samples if s["confidence"] == "low"])

    kept.sort(key=lambda s: s["level"])
    levels = [s["level"] for s in kept]
    median = round_tenth(statistics.median(levels))
    spread = round(levels[-1] - levels[0], 2)

    closest = min(kept, key=lambda s: abs(s["level"] - median))

    by_camera = {}
    for s in kept:
        by_camera.setdefault(s["camera"], []).append(s["level"])
    if len(by_camera) > 1:
        summary = {c: round(statistics.median(v), 2) for c, v in by_camera.items()}
        log.info("Per-camera medians: %s", summary)

    log.info("Kept %d/%d: %s -> median %.1f ft (spread %.2f)",
             len(kept), len(samples), levels, median, spread)

    return {
        "level": median,
        "readings": levels,
        "samples": samples,
        "cameras": sorted(by_camera),
        "low": round_tenth(levels[0]),
        "high": round_tenth(levels[-1]),
        "spread": spread,
        "confidence": confidence_from_spread(spread, len(levels)),
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
