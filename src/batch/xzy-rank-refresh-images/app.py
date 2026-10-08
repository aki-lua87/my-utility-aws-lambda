"""XZY character image cache refresh Lambda function.

Manual-only utility for src/batch/xzy-rank. That function caches character
thumbnail images in S3 keyed by md5(url) and never re-fetches once cached, so
a single bad/corrupt download (e.g. a transient origin error that still
returned a 200 with broken image bytes) gets stuck in the cache forever.
This function re-downloads the current roster's images from origin and
overwrites the S3 cache entries, without touching DynamoDB or Discord.
"""

import hashlib
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import boto3
import requests
from PIL import Image

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("TABLE_NAME", "aki-utils-dev")
table = dynamodb.Table(table_name)
s3 = boto3.client("s3")
image_cache_bucket = os.environ.get("IMAGE_CACHE_BUCKET", "")

# Constants (must match src/batch/xzy-rank/app.py)
P_KEY = "xzy_rank"
S_KEY = "latest_list_id"
API_BASE_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/hot-rank"
DEFAULT_LIST_ID = 154
IMAGE_CACHE_PREFIX = "images/"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Re-download character images from origin and overwrite the S3 cache.

    Args:
        event: Lambda event object. Optional "list_id" (int) to refresh images
            for a specific week instead of the latest known list_id.
        context: Lambda context object

    Returns:
        Response dict with status code and a summary of refreshed/failed images
    """
    try:
        if not image_cache_bucket:
            logger.error("IMAGE_CACHE_BUCKET environment variable not set")
            return {"statusCode": 500, "body": json.dumps({"error": "Image cache bucket not configured"})}

        list_id = event.get("list_id") if event else None
        if list_id is None:
            list_id = get_last_list_id()
        logger.info(f"Refreshing character images for list_id {list_id}")

        response = fetch_ranking_data(int(list_id))
        if not response or response.get("code") != 0 or not response.get("data"):
            logger.error(f"Could not fetch ranking data for list_id {list_id}")
            return {"statusCode": 404, "body": json.dumps({"error": f"No data found for list_id {list_id}"})}

        urls = extract_avatar_urls(response["data"])
        logger.info(f"Found {len(urls)} unique avatar URLs to refresh")

        refreshed, failed = refresh_images(urls)

        logger.info(f"Refresh complete: {len(refreshed)} succeeded, {len(failed)} failed")
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Image cache refresh complete",
                    "list_id": list_id,
                    "refreshed_count": len(refreshed),
                    "failed_count": len(failed),
                    "failed_urls": failed,
                }
            ),
        }

    except Exception as e:
        logger.error(f"Error refreshing XZY image cache: {e}", exc_info=True)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def get_last_list_id() -> int:
    """Get the last processed list_id from DynamoDB (shared with XzyRankFunction)."""
    try:
        response = table.get_item(Key={"p_key": P_KEY, "s_key": S_KEY})
        if "Item" in response:
            return int(response["Item"].get("list_id", DEFAULT_LIST_ID))
        return DEFAULT_LIST_ID
    except Exception as e:
        logger.error(f"Error fetching list_id from DynamoDB: {e}", exc_info=True)
        return DEFAULT_LIST_ID


def fetch_ranking_data(list_id: int) -> dict[str, Any] | None:
    """Fetch ranking data from API for a specific list_id."""
    try:
        params = {
            "tt_type": "2v2",
            "tt_score": "≥6000，＜8000",  # 全角カンマと全角< が必要
            "order_field": "win_rate",
            "order_method": "DESC",
            "list_id": list_id,
        }
        response = requests.get(API_BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching data for list_id {list_id}: {e}")
        return None


def extract_avatar_urls(data: list[dict[str, Any]]) -> list[str]:
    """Extract unique, non-empty avatar URLs from ranking data."""
    urls: list[str] = []
    seen: set[str] = set()
    for item in data:
        role = item.get("role", {})
        url = role.get("avatar_link") or role.get("avatar_link_xcx") or role.get("img_preview")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _s3_key_from_url(url: str) -> str:
    """Generate S3 cache key from image URL (must match src/batch/xzy-rank/app.py)."""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    return f"{IMAGE_CACHE_PREFIX}{url_hash}.png"


def refresh_one_image(url: str, retries: int = 3) -> bool:
    """Force re-download a single image from origin and overwrite its S3 cache entry.

    Returns:
        True if the image was downloaded and cached successfully
    """
    s3_key = _s3_key_from_url(url)

    for attempt in range(1, retries + 1):
        timeout = 2**attempt  # 2s, 4s, 8s
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()

            # Validate that the response is actually a decodable image before
            # overwriting the cache, so a bad response can't re-poison it.
            img = Image.open(io.BytesIO(response.content))
            img.verify()

            buf = io.BytesIO()
            Image.open(io.BytesIO(response.content)).save(buf, format="PNG")
            buf.seek(0)
            s3.put_object(Bucket=image_cache_bucket, Key=s3_key, Body=buf, ContentType="image/png")
            logger.info(f"Refreshed cache: {s3_key} <- {url}")
            return True
        except Exception as e:
            if attempt < retries:
                wait = 2 ** (attempt - 1)
                logger.warning(f"Failed to refresh image (attempt {attempt}/{retries}) from {url}: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.warning(f"Failed to refresh image from {url}: {e}")

    return False


def refresh_images(urls: list[str]) -> tuple[list[str], list[str]]:
    """Refresh multiple images in parallel.

    Returns:
        Tuple of (successfully refreshed urls, failed urls)
    """
    refreshed: list[str] = []
    failed: list[str] = []

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(refresh_one_image, url): url for url in urls}
        for future in as_completed(futures):
            url = futures[future]
            if future.result():
                refreshed.append(url)
            else:
                failed.append(url)

    return refreshed, failed
