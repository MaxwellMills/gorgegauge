"""
Lambda handler — receive a paddler conditions report and append it to
husumReports.json in the gorgegauge.com S3 bucket.
"""

import json
import boto3
import os
from datetime import datetime, timezone

BUCKET = os.environ.get("OUTPUT_BUCKET", "gorgegauge.com")
REPORTS_KEY = os.environ.get("REPORTS_KEY", "husumReports.json")
GAUGE_KEY = os.environ.get("GAUGE_KEY", "husumGauge.json")
MAX_REPORTS = 50
MAX_TEXT = 280

s3 = boto3.client("s3")

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Type": "application/json",
}


def ok(body=None):
    return {"statusCode": 200, "headers": CORS, "body": json.dumps(body or {"ok": True})}


def err(msg, code=400):
    return {"statusCode": code, "headers": CORS, "body": json.dumps({"error": msg})}


def handler(event, context):
    method = (event.get("requestContext") or {}).get("http", {}).get("method", "GET")

    if method == "OPTIONS":
        return ok()

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return err("invalid JSON")

    # Honeypot — bots fill the hidden website field
    if body.get("website"):
        return ok()

    text = (body.get("text") or "").strip()[:MAX_TEXT]
    name = (body.get("name") or "").strip()[:50] or "Anonymous"

    if not text:
        return err("text is required")

    # Grab current gauge level to attach to the report
    gauge_level = None
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=GAUGE_KEY)
        gauge_level = json.loads(obj["Body"].read()).get("level")
    except Exception:
        pass

    # Load existing reports (or start fresh)
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=REPORTS_KEY)
        reports = json.loads(obj["Body"].read()).get("reports", [])
    except Exception:
        reports = []

    reports.insert(0, {
        "name": name,
        "text": text,
        "iso": datetime.now(timezone.utc).isoformat(),
        "level": gauge_level,
    })
    reports = reports[:MAX_REPORTS]

    s3.put_object(
        Bucket=BUCKET,
        Key=REPORTS_KEY,
        Body=json.dumps({"reports": reports}).encode("utf-8"),
        ContentType="application/json",
        CacheControl="no-cache, max-age=30",
    )

    return ok()
