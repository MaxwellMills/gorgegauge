import os
import sys
from urllib.parse import urlparse
from datetime import datetime
import mimetypes
from playwright.sync_api import sync_playwright
import requests
import boto3
from botocore.exceptions import ClientError
import time

# ---- Config via environment variables ----

REVEAL_EMAIL = os.environ.get("REVEAL_EMAIL", "maxwelltmills@gmail.com")
REVEAL_PASSWORD = os.environ.get("REVEAL_PASSWORD", "Rhonda4!!")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "gorgewhitewater")
CATEGORY = os.environ.get("CATEGORY", "Husum")

LOGIN_URL = "https://account.revealcellcam.com/login"
GALLERY_URL = "https://account.revealcellcam.com/gallery"

s3 = boto3.client("s3")


def s3_exists(bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def infer_content_type_from_url(url: str) -> str:
    ct, _ = mimetypes.guess_type(urlparse(url).path)
    return ct or "image/jpeg"


def build_s3_key(category: str, image_url: str) -> str:
    parsed = urlparse(image_url)
    filename = os.path.basename(parsed.path) or "image.jpg"
    return f"tactacam/{category}/{filename}"


def extract_latest_10_image_urls(page) -> list[str]:
    page.wait_for_load_state("networkidle", timeout=30000)

    page.wait_for_selector("img", timeout=30000)

    urls = page.evaluate(
        """
        () => {
          const imgs = Array.from(document.querySelectorAll('img'));
          const raw = [];
          for (const img of imgs) {
            const src = img.currentSrc || img.src;
            if (!src) continue;
            if (src.startsWith('data:')) continue;

            raw.push(src);
          }

          const seen = new Set();
          const out = [];
          for (const u of raw) {
            const key = u.split('?')[0];
            if (seen.has(key)) continue;
            seen.add(key);
            out.push(u);
            if (out.length >= 10) break;
          }
          return out;
        }
        """
    )

    cleaned = []
    seen = set()
    for u in urls:
        if not u:
            continue
        no_qs = u.split("?")[0]
        if no_qs in seen:
            continue
        seen.add(no_qs)
        cleaned.append(u)

    return cleaned[:10]


def upload_image_url_to_s3(image_url: str, bucket: str, key: str) -> bool:
    if s3_exists(bucket, key):
        print(f"SKIP existing: s3://{bucket}/{key}")
        return False

    print(f"UPLOAD new: s3://{bucket}/{key}")

    r = requests.get(image_url, stream=True, timeout=45)
    r.raise_for_status()

    content_type = infer_content_type_from_url(image_url)

    s3.upload_fileobj(
        r.raw,
        bucket,
        key,
        ExtraArgs={
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000, immutable",
        },
    )
    return True


def run_scrape():
    if not REVEAL_EMAIL or not REVEAL_PASSWORD:
        print("Missing REVEAL_EMAIL or REVEAL_PASSWORD env vars.")
        sys.exit(2)

    print(f"Starting job. Bucket={S3_BUCKET_NAME}, Category={CATEGORY}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print("Opening login page...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        time.sleep(2)

        email_input = (
            page.query_selector('input[type="email"]')
            or page.query_selector('input[name*="email"]')
            or page.query_selector('input[autocomplete*="email"]')
            or page.query_selector('input[type="text"]')
        )
        if not email_input:
            print("ERROR: email field not found.")
            browser.close()
            sys.exit(1)
        email_input.fill(REVEAL_EMAIL)

        password_input = (
            page.query_selector('input[type="password"]')
            or page.query_selector('input[name*="password"]')
            or page.query_selector('input[autocomplete*="current-password"]')
        )
        if not password_input:
            print("ERROR: password field not found.")
            browser.close()
            sys.exit(1)
        password_input.fill(REVEAL_PASSWORD)

        print("Submitting login...")
        password_input.press("Enter")
        time.sleep(5)

        print("Opening gallery page...")
        page.goto(GALLERY_URL, wait_until="domcontentloaded")
        time.sleep(3)

        print("Scraping latest 10 image URLs...")
        urls = extract_latest_10_image_urls(page)
        print(f"Found {len(urls)} candidate images.")

        if not urls:
            print("No images found.")
            browser.close()
            return

        uploaded = 0
        skipped = 0
        failed = 0

        for u in urls:
            try:
                key = build_s3_key(CATEGORY, u)
                did = upload_image_url_to_s3(u, S3_BUCKET_NAME, key)
                if did:
                    uploaded += 1
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                print(f"FAIL url={u} err={e}")

        print(f"Done. checked={len(urls)} uploaded={uploaded} skipped={skipped} failed={failed}")
        browser.close()


if __name__ == "__main__":
    try:
        run_scrape()
    except Exception as e:
        print("Fatal error:", e)
        sys.exit(1)

