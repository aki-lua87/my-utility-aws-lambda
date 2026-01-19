"""XZY ranking data fetcher Lambda function.

This function fetches XZY battle record ranking data from API,
stores the latest list_id in DynamoDB, and posts the data to Discord.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

import boto3
import requests

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("TABLE_NAME", "aki-utils-dev")
table = dynamodb.Table(table_name)

# Constants
P_KEY = "xzy_rank"
S_KEY = "latest_list_id"
API_BASE_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/hot-rank"
DEFAULT_LIST_ID = 106  # Starting list_id
MAX_SEARCH_INCREMENT = 10  # Maximum number of increments to search


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for XZY ranking data fetcher.

    Args:
        event: Lambda event object
        context: Lambda context object

    Returns:
        Response dict with status code and body
    """
    try:
        logger.info("Starting XZY ranking data fetch")

        # Get Discord webhook URL from environment
        webhook_url = os.environ.get("XZY_WEBHOOK_URL")
        if not webhook_url:
            logger.error("XZY_WEBHOOK_URL environment variable not set")
            return {"statusCode": 500, "body": json.dumps({"error": "Webhook URL not configured"})}

        # Get last list_id from DynamoDB
        last_list_id = get_last_list_id()
        logger.info(f"Last list_id from DynamoDB: {last_list_id}")

        # Search for the latest list_id with data
        latest_data, latest_list_id = find_latest_data(last_list_id)

        if not latest_data:
            logger.warning("No new data found")
            return {"statusCode": 200, "body": json.dumps({"message": "No new data found"})}

        logger.info(f"Found latest data with list_id: {latest_list_id}")

        # Save the latest list_id to DynamoDB
        save_last_list_id(latest_list_id)

        # Post to Discord
        post_to_discord(latest_data, latest_list_id, webhook_url)

        logger.info("Successfully completed XZY ranking data fetch")
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Successfully processed XZY ranking data",
                    "list_id": latest_list_id,
                    "data_count": len(latest_data),
                }
            ),
        }

    except Exception as e:
        logger.error(f"Error processing XZY ranking data: {e}", exc_info=True)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def get_last_list_id() -> int:
    """Get the last processed list_id from DynamoDB.

    Returns:
        Last processed list_id (default: DEFAULT_LIST_ID)
    """
    try:
        response = table.get_item(Key={"p_key": P_KEY, "s_key": S_KEY})

        if "Item" in response:
            list_id = response["Item"].get("list_id", DEFAULT_LIST_ID)
            logger.info(f"Retrieved list_id from DynamoDB: {list_id}")
            return int(list_id)

        logger.info(f"No previous list_id found, using default: {DEFAULT_LIST_ID}")
        return DEFAULT_LIST_ID

    except Exception as e:
        logger.error(f"Error fetching list_id from DynamoDB: {e}", exc_info=True)
        return DEFAULT_LIST_ID


def save_last_list_id(list_id: int) -> None:
    """Save the latest list_id to DynamoDB.

    Args:
        list_id: The latest list_id to save
    """
    try:
        item = {
            "p_key": P_KEY,
            "s_key": S_KEY,
            "list_id": list_id,
            "updated_at": datetime.now().isoformat(),
        }

        table.put_item(Item=item)
        logger.info(f"Saved list_id to DynamoDB: {list_id}")

    except Exception as e:
        logger.error(f"Error saving list_id to DynamoDB: {e}", exc_info=True)
        raise


def fetch_ranking_data(list_id: int) -> dict[str, Any] | None:
    """Fetch ranking data from API for a specific list_id.

    Args:
        list_id: The list_id to fetch

    Returns:
        API response JSON or None if request fails
    """
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

        data = response.json()
        logger.info(f"Fetched data for list_id {list_id}: code={data.get('code')}, data_count={len(data.get('data', []))}")
        return data

    except Exception as e:
        logger.error(f"Error fetching data for list_id {list_id}: {e}")
        return None


def find_latest_data(start_list_id: int) -> tuple[list[dict[str, Any]], int]:
    """Find the latest list_id with data by incrementing from start_list_id.

    Args:
        start_list_id: The list_id to start searching from

    Returns:
        Tuple of (latest_data, latest_list_id)
    """
    latest_data = []
    latest_list_id = start_list_id

    # Search forward from the last known list_id
    for i in range(MAX_SEARCH_INCREMENT):
        current_list_id = start_list_id + i
        logger.info(f"Checking list_id: {current_list_id}")

        response = fetch_ranking_data(current_list_id)

        if not response or response.get("code") != 0:
            logger.warning(f"Invalid response for list_id {current_list_id}")
            break

        data = response.get("data", [])

        if len(data) > 0:
            # Found data, update latest
            latest_data = data
            latest_list_id = current_list_id
            logger.info(f"Found data for list_id {current_list_id} with {len(data)} items")
        else:
            # Empty data found, previous one was the latest
            logger.info(f"Empty data for list_id {current_list_id}, stopping search")
            break

    return latest_data, latest_list_id


def format_ranking_message(data: list[dict[str, Any]], list_id: int) -> list[dict[str, Any]]:
    """Format ranking data into Discord embed format.

    Args:
        data: Ranking data from API
        list_id: The list_id of the data

    Returns:
        List of Discord embed dicts (may be multiple if data is large)
    """
    # Sort by win_rate descending (should already be sorted from API)
    sorted_data = sorted(data, key=lambda x: float(x.get("win_rate", 0)), reverse=True)

    # Build all description lines
    description_lines = []
    for i, item in enumerate(sorted_data, 1):
        role = item.get("role", {})
        name_jp = role.get("name_jp", "Unknown")
        win_rate = item.get("win_rate", "0")
        on_rate = item.get("on_rate", "0")
        ban_rate = item.get("ban_rate", "0")

        # Compact format to fit more data
        line = f"**{i}. {name_jp}** - 勝率: {win_rate}% | 出場: {on_rate}% | BAN: {ban_rate}%"
        description_lines.append(line)

    # Split into multiple embeds if necessary (Discord limit: 4096 chars per description)
    embeds = []
    current_lines = []
    current_length = 0
    max_length = 4000  # Leave some margin for safety

    for line in description_lines:
        line_length = len(line) + 1  # +1 for newline
        if current_length + line_length > max_length and current_lines:
            # Create embed for current batch
            embeds.append(create_embed_chunk(current_lines, list_id, len(embeds)))
            current_lines = [line]
            current_length = line_length
        else:
            current_lines.append(line)
            current_length += line_length

    # Add remaining lines
    if current_lines:
        embeds.append(create_embed_chunk(current_lines, list_id, len(embeds)))

    return embeds


def create_embed_chunk(lines: list[str], list_id: int, chunk_index: int) -> dict[str, Any]:
    """Create a single embed chunk.

    Args:
        lines: Description lines for this chunk
        list_id: The list_id of the data
        chunk_index: Index of this chunk (0-based)

    Returns:
        Discord embed dict
    """
    description = "\n".join(lines)
    
    title = "今週の 2v2 キャラランキング (6000-8000帯)"
    if chunk_index > 0:
        title += f" (続き {chunk_index + 1})"
    
    embed = {
        "title": title,
        "description": description,
        "color": 0x5865F2,  # Discord blurple color
    }
    
    # Add footer only to the last chunk (will be the first/only chunk initially)
    if chunk_index == 0 or len(lines) < 50:  # Heuristic: if small, likely the last chunk
        embed["footer"] = {
            "text": f"List ID: {list_id} | 取得日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        }
        embed["timestamp"] = datetime.now().isoformat()
    
    return embed


def post_to_discord(data: list[dict[str, Any]], list_id: int, webhook_url: str) -> None:
    """Post ranking data to Discord webhook.

    Args:
        data: Ranking data from API
        list_id: The list_id of the data
        webhook_url: Discord webhook URL
    """
    try:
        embeds = format_ranking_message(data, list_id)
        
        # Discord allows up to 10 embeds per message
        # If we have more, send multiple messages
        for i in range(0, len(embeds), 10):
            batch_embeds = embeds[i:i+10]
            payload = {"embeds": batch_embeds}

            response = requests.post(webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            
            logger.info(f"Posted {len(batch_embeds)} embed(s) to Discord (batch {i//10 + 1})")

        logger.info(f"Posted ranking data to Discord (list_id: {list_id}, total embeds: {len(embeds)})")

    except Exception as e:
        logger.error(f"Error posting to Discord: {e}", exc_info=True)
        raise
