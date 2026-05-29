#!/usr/bin/env python3
"""
Build husumIndex.json — a full inventory of every trail-cam image in S3,
with pre-parsed dates so the frontend can filter by day without any server calls.
"""

import os
import re
import json
import logging
from datetime import datetime
from urllib.parse import quote

import boto3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

SOURCE_BUCKET   = os.environ.get("SOURCE_BUCKET",   "gorgewhitewater")
SOURCE_PREFIX   = os.environ.get("SOURCE_PREFIX",   "tactacam/Husum/")
OUTPUT_BUCKET   = os.environ.get("OUTPUT_BUCKET",   "gorgegauge.com")
OUTPUT_KEY      = os.environ.get("OUTPUT_KEY",      "husumIndex.json")
PUBLIC_IMAGE_BASE = os.environ.get(
    "PUBLIC_IMAGE_BASE",
    "https://gorgewhitewater.s3.us-east-2.amazonaws.com"
)

s3 = boto3.client("s3")


def parse_timestamp(key):
    match = re.search(r"-(\d{14})-", key)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%m%d%Y%H%M%S")
    except ValueError:
        return None


def main():
    log.info("Listing images under s3://%s/%s", SOURCE_BUCKET, SOURCE_PREFIX)
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SOURCE_BUCKET, Prefix=SOURCE_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith((".jpg", ".jpeg")):
                keys.append(key)

    log.info("Found %d images — parsing timestamps…", len(keys))

    images = []
    skipped = 0
    for key in keys:
        ts = parse_timestamp(key)
        if not ts:
            skipped += 1
            continue
        images.append({
            "url":          f"{PUBLIC_IMAGE_BASE}/{quote(key)}",
            "fileName":     key.split("/")[-1],
            "lastModified": ts.isoformat(),   # used by buildCard for display
            "date":         ts.strftime("%Y-%m-%d"),  # used for date-picker filter
        })

    if skipped:
        log.warning("Skipped %d images with unparseable timestamps", skipped)

    # Newest first
    images.sort(key=lambda x: x["lastModified"], reverse=True)

    payload = {
        "images":       images,
        "count":        len(images),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

    body = json.dumps(payload).encode("utf-8")
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=OUTPUT_KEY,
        Body=body,
        ContentType="application/json",
        CacheControl="public, max-age=3600",
    )
    log.info("Wrote s3://%s/%s  (%d images)", OUTPUT_BUCKET, OUTPUT_KEY, len(images))


if __name__ == "__main__":
    main()
