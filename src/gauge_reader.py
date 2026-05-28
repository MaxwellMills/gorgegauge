#!/usr/bin/env python3

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

SOURCE_PREFIX  = os.environ.get("SOURCE_PREFIX",  "tactacam/Husum/")
OUTPUT_BUCKET  = os.environ.get("OUTPUT_BUCKET",  "gorgegauge.com")
OUTPUT_KEY     = os.environ.get("OUTPUT_KEY",     "husumGauge.json")
IMAGE_COUNT    = int(os.environ.get("IMAGE_COUNT", "5"))

PUBLIC_IMAGE_BASE = os.environ.get(
    "PUBLIC_IMAGE_BASE",
    "https://gorgewhitewater.s3.us-east-2.amazonaws.com"
)

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

s3 = boto3.client("s3")


# ── S3 helpers ────────────────────────────────────────────────────────────────

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "gorgewhitewater")


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


def parse_timestamp(key):
    match = re.search(r"-(\d{14})-", key)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%m%d%Y%H%M%S")
    except ValueError:
        return None


def get_latest_n(keys, n):
    """Return the n most recent (key, timestamp) pairs, newest first."""
    dated = [(k, parse_timestamp(k)) for k in keys if parse_timestamp(k)]
    dated.sort(key=lambda x: x[1], reverse=True)
    return dated[:n]


def fetch_image_bytes(key):
    """Download an image from the public S3 URL."""
    url = f"{PUBLIC_IMAGE_BASE}/{quote(key)}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def image_url_for_key(key):
    return f"{PUBLIC_IMAGE_BASE}/{quote(key)}"


# ── Claude reading ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are reading a river staff gauge at Husum Falls on the White Salmon River, WA. "

    "GAUGE LAYOUT: "
    "A white vertical staff mounted on the rock face in the background. "
    "Numbers decrease from top to bottom: 6, 5, 4, 3, 2, 1, 0. "
    "CRITICAL: each number is printed just ABOVE its corresponding wide hashmark. "
    "So the '3' label sits just above the 3 ft hashmark, '2' just above the 2 ft hashmark, etc. "
    "Narrower hashmarks appear every 0.25 ft between the wide whole-foot marks. "

    "EXPECTED RANGE: At typical flows the gauge reads 1.5 ft to 4.5 ft. "
    "If your reading is below 1.0 ft or above 5.5 ft, re-examine the image. "

    "THREE-STEP METHOD: "
    "Step 1 — Find the waterline: where the river surface meets the gauge staff. "
    "Step 2 — Identify the two whole-foot marks bracketing the waterline. "
    "  Each wide hashmark sits just BELOW its printed number label. "
    "  The upper bracket (its number label visible above water) is above the waterline. "
    "  The lower bracket mark is at or just below the waterline. "
    "Step 3 — Estimate what fraction of the 1-ft gap the waterline sits above the lower bracket mark, "
    "  then add that to the lower mark's value. "
    "  Example: lower bracket = 2 ft mark, waterline 25% up to 3 ft → level = 2.25 ft. "
    "  Example: lower bracket = 2 ft mark, waterline 75% up to 3 ft → level = 2.75 ft. "
    "  Example: lower bracket = 3 ft mark, waterline 50% up to 4 ft → level = 3.50 ft. "

    "Round to nearest 0.05 ft. Respond ONLY with raw JSON, no markdown: "
    "{\"level\": 2.25, \"confidence\": \"medium\", \"notes\": \"2 ft mark just below waterline, 3 ft mark above, waterline ~25% up\"}"
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
                {
                    "type": "text",
                    "text": "Read the water level. Top number on gauge is 6. Return JSON only.",
                },
            ],
        }],
    )

    text = message.content[0].text.strip()
    match = re.search(r'\{[^{}]+\}', text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in response: {text!r}")
    return json.loads(match.group())


# ── Consensus logic ───────────────────────────────────────────────────────────

def read_consensus(image_list):
    """
    Read up to IMAGE_COUNT images independently and return the median level.
    Returns (median_level, all_readings, best_notes).
    """
    readings = []
    notes_by_level = {}

    for key, ts in image_list:
        filename = key.split("/")[-1]
        try:
            img_bytes = fetch_image_bytes(key)
            result = ask_claude(img_bytes)
            level = float(result["level"])
            readings.append(level)
            notes_by_level[level] = result.get("notes", "")
            log.info("  %s → %.2f ft (%s)", filename, level, result.get("confidence", "?"))
        except Exception as e:
            log.warning("  %s → skipped: %s", filename, e)

    if not readings:
        raise RuntimeError("No valid readings obtained from any image")

    readings.sort()
    median = statistics.median(readings)
    # Round to nearest 0.05
    median = round(round(median / 0.05) * 0.05, 2)

    # Pick notes from the reading closest to the median
    closest = min(readings, key=lambda r: abs(r - median))
    best_notes = notes_by_level.get(closest, "")

    log.info("Readings: %s → median %.2f ft", readings, median)
    return median, readings, best_notes


# ── Output ────────────────────────────────────────────────────────────────────

def write_gauge_json(payload):
    body = json.dumps(payload, indent=2).encode("utf-8")
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=OUTPUT_KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="no-cache, max-age=60",
    )
    log.info("Wrote s3://%s/%s", OUTPUT_BUCKET, OUTPUT_KEY)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required")

    log.info("Listing images from %s%s", PUBLIC_IMAGE_BASE, SOURCE_PREFIX)
    keys = list_s3_images()
    if not keys:
        raise RuntimeError(f"No images found under {SOURCE_PREFIX}")

    latest_n = get_latest_n(keys, IMAGE_COUNT)
    log.info("Analyzing %d images (newest first):", len(latest_n))
    for k, ts in latest_n:
        log.info("  %s  (%s)", k.split("/")[-1], ts)

    median_level, all_readings, notes = read_consensus(latest_n)

    # Most recent image for display
    latest_key, photo_ts = latest_n[0]
    now_pacific = datetime.now(PACIFIC)

    payload = {
        "level": median_level,
        "readings": all_readings,
        "images_analyzed": len(all_readings),
        "notes": notes,
        "image_url": image_url_for_key(latest_key),
        "image_key": latest_key,
        "photo_timestamp": photo_ts.strftime("%b %d, %Y at %-I:%M %p") if photo_ts else None,
        "read_at": now_pacific.strftime("%b %d, %Y at %-I:%M %p PT"),
        "read_at_iso": now_pacific.isoformat(),
    }

    write_gauge_json(payload)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
