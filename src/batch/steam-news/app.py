"""Steam news fetcher Lambda function.

This function fetches Steam news from RSS feeds for configured app IDs,
stores new news items in DynamoDB, and posts them to Discord.
"""

import html
import json
import logging
import os
import re
from datetime import datetime
from typing import Any

import boto3
import feedparser
import requests
from boto3.dynamodb.conditions import Key
import time

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("TABLE_NAME", "aki-utils-dev")
table = dynamodb.Table(table_name)

# Constants
P_KEY_PREFIX = "steam_news"
APP_ID_SK_PREFIX = "appid_"
NEWS_SK_PREFIX = "news_"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for Steam news fetcher.

    Args:
        event: Lambda event object
        context: Lambda context object

    Returns:
        Response dict with status code and body
    """
    try:
        logger.info("Starting Steam news fetch")

        # Get all app IDs from DynamoDB
        app_items = get_app_ids()
        logger.info(f"Found {len(app_items)} app IDs to process")

        if not app_items:
            logger.warning("No app IDs found in database")
            return {"statusCode": 200, "body": json.dumps({"message": "No app IDs configured"})}

        total_new_news = 0

        # Process each app ID
        for app_item in app_items:
            app_id = app_item["app_id"]
            game_title = app_item.get("game_title", app_id)
            webhook_url = app_item.get("webhook_url")
            logger.info(f"Processing app ID: {app_id} ({game_title})")
            new_news_count = process_app_id(app_id, game_title, webhook_url)
            total_new_news += new_news_count
            logger.info(f"Found {new_news_count} new news items for {game_title}")

        logger.info(f"Completed: {total_new_news} total new news items processed")

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Successfully processed Steam news",
                    "app_ids_processed": len(app_items),
                    "new_news_count": total_new_news,
                }
            ),
        }

    except Exception as e:
        logger.error(f"Error processing Steam news: {e}", exc_info=True)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def get_app_ids() -> list[dict[str, str]]:
    """Get all registered Steam app IDs from DynamoDB.

    Returns:
        List of dicts containing app_id, game_title, and webhook_url (if exists)
    """
    try:
        response = table.query(
            KeyConditionExpression=Key("p_key").eq(P_KEY_PREFIX)
            & Key("s_key").begins_with(APP_ID_SK_PREFIX)
        )

        app_items = []
        for item in response.get("Items", []):
            app_id = item.get("app_id")
            game_title = item.get("game_title", app_id)
            webhook_url = item.get("webhook_url")
            if app_id:
                app_item = {"app_id": app_id, "game_title": game_title}
                if webhook_url:
                    app_item["webhook_url"] = webhook_url
                app_items.append(app_item)

        return app_items

    except Exception as e:
        logger.error(f"Error fetching app IDs from DynamoDB: {e}", exc_info=True)
        raise


def process_app_id(app_id: str, game_title: str, webhook_url: str = None) -> int:
    """Process news for a specific app ID.

    Args:
        app_id: Steam app ID
        game_title: Game title
        webhook_url: Discord webhook URL (optional)

    Returns:
        Number of new news items processed
    """
    try:
        # Fetch RSS feed
        rss_url = f"https://store.steampowered.com/feeds/news/app/{app_id}/?l=japanese"
        logger.info(f"Fetching RSS from: {rss_url}")

        feed = feedparser.parse(rss_url)

        if not feed.entries:
            logger.warning(f"No entries found in RSS feed for app ID {app_id}")
            return 0

        # Collect all GUIDs from feed entries
        entry_map = {}  # guid -> entry
        guids = []
        for entry in feed.entries:
            guid = entry.get("id") or entry.get("link")
            if not guid:
                logger.warning("News item missing guid/id, skipping")
                continue
            entry_map[guid] = entry
            guids.append(guid)

        if not guids:
            logger.warning(f"No valid entries found in RSS feed for app ID {app_id}")
            return 0

        # Batch check which news items already exist
        existing_guids = batch_check_news_exists(app_id, guids)

        # Process only new news items
        new_news_count = 0
        for guid, entry in entry_map.items():
            if guid in existing_guids:
                logger.debug(f"News already exists: {guid}")
                continue

            # Save new news to DynamoDB
            save_news(app_id, entry)

            # Post to Discord
            post_to_discord(app_id, game_title, entry, webhook_url)

            new_news_count += 1

        return new_news_count

    except Exception as e:
        logger.error(f"Error processing app ID {app_id}: {e}", exc_info=True)
        return 0


def batch_check_news_exists(app_id: str, guids: list[str]) -> set[str]:
    """Check if multiple news items exist in DynamoDB using BatchGetItem.

    Args:
        app_id: Steam app ID
        guids: List of news item GUIDs

    Returns:
        Set of GUIDs that exist in DynamoDB
    """
    if not guids:
        return set()

    try:
        existing_guids = set()

        # BatchGetItem supports up to 100 items at a time
        for i in range(0, len(guids), 100):
            batch_guids = guids[i : i + 100]

            # Build keys for BatchGetItem
            keys = []
            guid_to_key = {}  # Map s_key back to original guid
            for guid in batch_guids:
                news_id = guid.split("/")[-1] if "/" in guid else guid
                s_key = f"{NEWS_SK_PREFIX}{app_id}_{news_id}"
                keys.append({"p_key": P_KEY_PREFIX, "s_key": s_key})
                guid_to_key[s_key] = guid

            # Execute BatchGetItem
            response = dynamodb.batch_get_item(
                RequestItems={table_name: {"Keys": keys, "ConsistentRead": False}}
            )

            # Process responses
            if table_name in response.get("Responses", {}):
                for item in response["Responses"][table_name]:
                    s_key = item.get("s_key")
                    if s_key in guid_to_key:
                        existing_guids.add(guid_to_key[s_key])

            # Handle unprocessed keys (throttling, etc.)
            unprocessed = response.get("UnprocessedKeys", {})
            retry_count = 0
            while unprocessed and retry_count < 3:
                logger.warning(f"Retrying {len(unprocessed)} unprocessed keys")

                time.sleep(2**retry_count)  # Exponential backoff
                response = dynamodb.batch_get_item(RequestItems=unprocessed)

                if table_name in response.get("Responses", {}):
                    for item in response["Responses"][table_name]:
                        s_key = item.get("s_key")
                        if s_key in guid_to_key:
                            existing_guids.add(guid_to_key[s_key])

                unprocessed = response.get("UnprocessedKeys", {})
                retry_count += 1

        logger.info(f"Batch check: {len(existing_guids)} / {len(guids)} news items already exist")
        return existing_guids

    except Exception as e:
        logger.error(f"Error in batch_check_news_exists: {e}", exc_info=True)
        # Fallback to empty set on error
        return set()


def save_news(app_id: str, entry: Any) -> None:
    """Save news item to DynamoDB.

    Args:
        app_id: Steam app ID
        entry: RSS feed entry
    """
    try:
        guid = entry.get("id") or entry.get("link")
        news_id = guid.split("/")[-1] if "/" in guid else guid
        s_key = f"{NEWS_SK_PREFIX}{app_id}_{news_id}"

        # Extract relevant fields
        title = entry.get("title", "")
        link = entry.get("link", "")
        description = strip_html_tags(entry.get("description", ""))
        pub_date = entry.get("published", "")

        # Store in DynamoDB
        item = {
            "p_key": P_KEY_PREFIX,
            "s_key": s_key,
            "app_id": app_id,
            "news_id": news_id,
            "guid": guid,
            "title": title,
            "link": link,
            "description": description,
            "pub_date": pub_date,
            "created_at": datetime.now().isoformat(),
        }

        table.put_item(Item=item)
        logger.info(f"Saved news to DynamoDB: {s_key}")

    except Exception as e:
        logger.error(f"Error saving news to DynamoDB: {e}", exc_info=True)
        raise


def strip_html_tags(text: str) -> str:
    """Remove HTML tags from text.

    Args:
        text: Text containing HTML tags

    Returns:
        Plain text with HTML tags removed
    """
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities (&lt; -> <, &amp; -> &, etc.)
    text = html.unescape(text)
    # Replace multiple whitespaces/newlines with single space
    text = re.sub(r"\s+", " ", text)
    # Strip leading/trailing whitespace
    return text.strip()


def post_to_discord(app_id: str, game_title: str, entry: Any, webhook_url: str = None) -> None:
    """Post news item to Discord webhook.

    Args:
        app_id: Steam app ID
        game_title: Game title
        entry: RSS feed entry
        webhook_url: Discord webhook URL (optional)
    """
    if not webhook_url:
        logger.warning(
            f"Discord webhook URL not configured for app {app_id}, skipping notification"
        )
        return

    try:
        title = entry.get("title", "")
        link = entry.get("link", "")
        description = entry.get("description", "")
        pub_date = entry.get("published", "")

        # Remove HTML tags from description
        description = strip_html_tags(description)

        # Truncate title if too long (Discord embed title limit is 256)
        if len(title) > 256:
            title = title[:253] + "..."

        # Truncate description if too long (Discord embed description limit is 4096)
        if len(description) > 500:
            description = description[:497] + "..."

        # Create Discord embed
        embed = {
            "title": title,
            "url": link,
            "description": description,
            "color": 0x1B2838,  # Steam blue color
            "author": {"name": game_title},
            "footer": {"text": f"Steam App ID: {app_id} | {pub_date}"},
        }

        payload = {"embeds": [embed]}

        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()

        logger.info(f"Posted news to Discord: {title}")

    except Exception as e:
        logger.error(f"Error posting to Discord: {e}", exc_info=True)
        # Don't raise - continue processing even if Discord post fails
