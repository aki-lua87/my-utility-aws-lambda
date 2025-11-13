"""Steam news APP ID management API.

This API provides endpoints to manage Steam app IDs for news fetching.
"""

import json
import logging
import os
from typing import Any, Optional

import boto3
import requests
from boto3.dynamodb.conditions import Key

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


def get_game_title_from_steam(app_id: str) -> Optional[str]:
    """Get game title from Steam Store API.

    Args:
        app_id: Steam app ID

    Returns:
        Game title if successful, None otherwise
    """
    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&l=japanese"
        logger.info(f"Fetching game title from Steam Store API: {url}")

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data.get(app_id, {}).get("success"):
            game_title = data[app_id]["data"]["name"]
            logger.info(f"Retrieved game title: {game_title}")
            return game_title
        else:
            logger.warning(f"App ID {app_id} not found in Steam Store")
            return None

    except Exception as e:
        logger.error(f"Error fetching game title from Steam: {e}", exc_info=True)
        return None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for Steam news API.

    Args:
        event: Lambda event object
        context: Lambda context object

    Returns:
        Response dict with status code and body
    """
    try:
        http_method = event.get("httpMethod", "")
        path = event.get("path", "")

        logger.info(f"Processing {http_method} request to {path}")

        # Route requests
        if http_method == "POST" and path == "/steam-news/appid":
            return register_app_id(event)
        elif http_method == "GET" and path == "/steam-news/appids":
            return list_app_ids(event)
        elif http_method == "GET" and path == "/steam-news/news":
            return get_news(event)
        else:
            return {
                "statusCode": 404,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Not found"}),
            }

    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }


def register_app_id(event: dict[str, Any]) -> dict[str, Any]:
    """Register a new Steam app ID.

    Request body:
        {
            "app_id": "2246340",
            "game_title": "Game Title (optional)",
            "webhook_url": "https://discord.com/api/webhooks/... (optional)"
        }

    Args:
        event: Lambda event object

    Returns:
        Response dict with status code and body
    """
    try:
        body = json.loads(event.get("body", "{}"))
        app_id = body.get("app_id")
        game_title = body.get("game_title")
        webhook_url = body.get("webhook_url")

        # Validate input
        if not app_id:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "app_id is required"}),
            }

        # Validate app_id is numeric
        if not app_id.isdigit():
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "app_id must be numeric"}),
            }

        # If game_title not provided, fetch from Steam Store API
        if not game_title:
            logger.info(f"Game title not provided, fetching from Steam Store API")
            game_title = get_game_title_from_steam(app_id)

            if not game_title:
                return {
                    "statusCode": 404,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps(
                        {"error": f"App ID {app_id} not found in Steam Store or failed to fetch"}
                    ),
                }

        # Store in DynamoDB
        s_key = f"{APP_ID_SK_PREFIX}{app_id}"
        item = {
            "p_key": P_KEY_PREFIX,
            "s_key": s_key,
            "app_id": app_id,
            "game_title": game_title,
        }

        # Add webhook_url if provided
        if webhook_url:
            item["webhook_url"] = webhook_url

        table.put_item(Item=item)

        logger.info(f"Registered app ID: {app_id} - {game_title}")

        response_body = {
            "message": "Successfully registered app ID",
            "app_id": app_id,
            "game_title": game_title,
        }

        if webhook_url:
            response_body["webhook_url"] = webhook_url

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(response_body),
        }

    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Invalid JSON in request body"}),
        }
    except Exception as e:
        logger.error(f"Error registering app ID: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }


def list_app_ids(event: dict[str, Any]) -> dict[str, Any]:
    """List all registered Steam app IDs.

    Args:
        event: Lambda event object

    Returns:
        Response dict with status code and body
    """
    try:
        response = table.query(
            KeyConditionExpression=Key("p_key").eq(P_KEY_PREFIX)
            & Key("s_key").begins_with(APP_ID_SK_PREFIX)
        )

        app_ids = []
        for item in response.get("Items", []):
            app_item = {
                "app_id": item.get("app_id"),
                "game_title": item.get("game_title"),
            }
            # Include webhook_url if it exists
            if item.get("webhook_url"):
                app_item["webhook_url"] = item.get("webhook_url")
            app_ids.append(app_item)

        logger.info(f"Listed {len(app_ids)} app IDs")

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"app_ids": app_ids}),
        }

    except Exception as e:
        logger.error(f"Error listing app IDs: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }


def get_news(event: dict[str, Any]) -> dict[str, Any]:
    """Get news items for specified app IDs.

    Query parameters:
        app_ids: Comma-separated app IDs (optional, all if not specified)
        limit: Number of news items to return (default: 10)

    Args:
        event: Lambda event object

    Returns:
        Response dict with status code and body
    """
    try:
        # Parse query parameters
        query_params = event.get("queryStringParameters") or {}
        app_ids_param = query_params.get("app_ids", "")
        limit = int(query_params.get("limit", "10"))

        # Validate limit
        if limit <= 0 or limit > 100:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "limit must be between 1 and 100"}),
            }

        # Parse app_ids
        if app_ids_param:
            app_ids = [aid.strip() for aid in app_ids_param.split(",") if aid.strip()]
        else:
            # Get all registered app IDs
            app_ids_response = table.query(
                KeyConditionExpression=Key("p_key").eq(P_KEY_PREFIX)
                & Key("s_key").begins_with(APP_ID_SK_PREFIX)
            )
            app_ids = [item.get("app_id") for item in app_ids_response.get("Items", [])]

        if not app_ids:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"news": [], "total": 0}),
            }

        logger.info(f"Fetching news for app IDs: {app_ids}, limit: {limit}")

        # Fetch game titles for app IDs
        game_titles = {}
        for app_id in app_ids:
            response = table.get_item(
                Key={"p_key": P_KEY_PREFIX, "s_key": f"{APP_ID_SK_PREFIX}{app_id}"}
            )
            item = response.get("Item")
            if item:
                game_titles[app_id] = item.get("game_title", app_id)
            else:
                game_titles[app_id] = app_id

        # Fetch news for each app ID
        all_news = []
        for app_id in app_ids:
            response = table.query(
                KeyConditionExpression=Key("p_key").eq(P_KEY_PREFIX)
                & Key("s_key").begins_with(f"{NEWS_SK_PREFIX}{app_id}_")
            )
            all_news.extend(response.get("Items", []))

        # Sort by pub_date (newest first)
        # Parse pub_date and sort
        def parse_date(item):
            from datetime import datetime
            pub_date = item.get("pub_date", "")
            try:
                # Try to parse RFC 2822 format: "Thu, 30 Oct 2025 02:06:11 +0000"
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(pub_date)
            except:
                # Fallback to created_at
                return item.get("created_at", "")

        all_news.sort(key=parse_date, reverse=True)

        # Limit results
        limited_news = all_news[:limit]

        # Format response
        news_items = []
        for item in limited_news:
            app_id = item.get("app_id")
            news_items.append(
                {
                    "game_title": game_titles.get(app_id, app_id),
                    "title": item.get("title"),
                    "link": item.get("link"),
                    "description": item.get("description"),
                    "pub_date": item.get("pub_date"),
                }
            )

        logger.info(f"Returning {len(news_items)} news items")

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"news": news_items, "total": len(all_news)}),
        }

    except ValueError as e:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": f"Invalid parameter: {str(e)}"}),
        }
    except Exception as e:
        logger.error(f"Error fetching news: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": str(e)}),
        }
