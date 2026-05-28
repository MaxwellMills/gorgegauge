#!/usr/bin/env python3

import os
import re
import json
import base64
import logging
from datetime import datetime
from urllib.parse import quote

import boto3
import requests
import anthropic
import pytz

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

PACIFIC = pytz.timezone("America/Los_Angeles")

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "gorgewhitewater")
SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "tactacam/Husum/")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "gorgegauge.com")
OUTPUT_KEY = os.environ.get("OUTPUT_KEY", "husumGauge.json")

PUBLIC_IMAGE_BASE = os.environ.get(
    "PUBLIC_IMAGE_BASE",
    "https://gorgewhitewater.s3.us-east-2.amazonaws.com"
)

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

s3 = boto3.client("s3")


def parse_timestamp(key):
    match = re.search(r"-(\d{14})-", key)
    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%m%d%Y%H%M%S")
    except ValueError:
        return None


def list_s3_images():
    keys = []
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=SOURCE_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith((".jpg", ".jpeg")):
                keys.append(key)

    return keys


def get_latest_image_key(keys):
    dated = []

    for key in keys:
        ts = parse_timestamp(key)
        if ts:
            dated.append((key, ts))

    if dated:
        return max(dated, key=lambda item: item[1])

    return max(keys), None


def read_image_bytes(key):
    obj = s3.get_object(Bucket=SOURCE_BUCKET, Key=key)
    return obj["Body"].read()


def image_url_for_key(key):
    return f"{PUBLIC_IMAGE_BASE}/{quote(key)}"


def ask_claude_to_read_gauge(image_bytes):
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    client = anthropic.Anthropic()

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=250,
        system=(
            "You are reading a river staff gauge at Husum Falls on the White Salmon River, WA. "
            "GAUGE STRUCTURE: The gauge is a vertical white staff mounted on a rock face. "
            "It has full-width horizontal hashmarks at each whole foot: 6, 5, 4, 3, 2, 1, 0. "
            "Between each whole foot there are evenly spaced shorter hashmarks at the half and quarter foot intervals. "
            "The TOP of the gauge is 6 ft. Numbers count DOWN toward the water. "
            "IMPORTANT: At typical flows only the upper portion of the gauge is visible above water — "
            "the numbers you can see will be 6, 5, 4 and possibly 3. "
            "HOW TO READ: "
            "1. Identify the full-width hashmarks — these are the whole foot marks (6, 5, 4, 3...). "
            "2. Find the lowest visible full-width hashmark above the waterline. "
            "3. Count how many smaller hashmarks the water surface is above the next hashmark below it. "
            "4. Each major division (between whole foot marks) has smaller hashmarks. "
            "Report to the nearest 0.05 ft by estimating between hashmarks. "
            "5. Report: [whole foot mark] plus [fractional distance to waterline]. "
            "Example: water surface halfway between 3 and 3.25 hashmarks = 3.10 ft. "
            "Example: water surface just above the 3 ft mark = 3.05 ft. "
            "Example: water surface one small hashmark above 3 ft mark = 3.25 ft. "
            "Round your final answer to the nearest 0.05 ft (e.g. 2.70, 2.75, 2.80, 2.85). "
            "Respond ONLY with raw JSON, no markdown: "
            "{\"level\": 2.75, \"confidence\": \"high\", \"notes\": \"water surface just below 3 ft mark, between 2.75 and 3.00\"}"
        ),
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Read the water level. Top number on gauge is 6. Return JSON only.",
                    },
                ],
            }
        ],
    )

    text = message.content[0].text.strip()
    text = re.sub(r"```json|```", "", text).strip()

    return json.loads(text)


def write_latest_gauge_json(payload):
    body = json.dumps(payload, indent=2).encode("utf-8")

    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=OUTPUT_KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="no-cache, max-age=60",
    )

    log.info("Wrote s3://%s/%s", OUTPUT_BUCKET, OUTPUT_KEY)


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is required")

    keys = list_s3_images()

    if not keys:
        raise RuntimeError(f"No images found at s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}")

    latest_key, photo_ts = get_latest_image_key(keys)
    log.info("Latest image: %s", latest_key)

    image_bytes = read_image_bytes(latest_key)
    reading = ask_claude_to_read_gauge(image_bytes)

    now_pacific = datetime.now(PACIFIC)

    payload = {
        "level": float(reading["level"]),
        "confidence": reading.get("confidence"),
        "notes": reading.get("notes"),
        "image_url": image_url_for_key(latest_key),
        "image_key": latest_key,
        "photo_timestamp": photo_ts.strftime("%b %d, %Y at %-I:%M %p") if photo_ts else None,
        "read_at": now_pacific.strftime("%b %d, %Y at %-I:%M %p PT"),
        "read_at_iso": now_pacific.isoformat(),
    }

    write_latest_gauge_json(payload)

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()