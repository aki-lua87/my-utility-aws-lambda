"""Steam新着ゲーム監視 Lambda関数。

特定の検索条件でSteamストアを監視し、新着ゲームをDiscordに通知する。
初回実行時（DynamoDB空）は既存ゲームを全件保存するのみで通知はしない。
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import boto3
import requests
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("TABLE_NAME", "aki-utils-dev")
table = dynamodb.Table(table_name)
webhook_url = os.environ.get("STEAM_NEW_GAMES_WEBHOOK_URL", "")

P_KEY = "steam_new_games"
S_KEY_PREFIX = "appid_"

SEARCH_URL = (
    "https://store.steampowered.com/search/results/"
    "?sort_by=Released_DESC"
    "&maxprice=1000"
    "&supportedlang=japanese"
    "&tags=1667"
    "&category1=998"
    "&category3=2"
    "&ndl=1"
    "&json=1"
)


def extract_appid(logo_url: str) -> str | None:
    """カプセル画像URLからSteam App IDを抽出する。
    例: .../steam/apps/4451000/capsule_sm_120.jpg → "4451000"
    """
    match = re.search(r"/apps/(\d+)/", logo_url)
    return match.group(1) if match else None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        logger.info("Starting Steam new games watch")

        items = fetch_search_results()
        if not items:
            logger.warning("No items returned from Steam search")
            return {"statusCode": 200, "body": json.dumps({"message": "No results from Steam"})}

        logger.info(f"Fetched {len(items)} items from Steam search")

        if is_initial_run():
            logger.info("Initial run: seeding DynamoDB without notifications")
            seed_all(items)
            return {
                "statusCode": 200,
                "body": json.dumps({"message": "Initial seed completed", "seeded": len(items)}),
            }

        new_items = filter_new_items(items)
        logger.info(f"Found {len(new_items)} new games")

        for item in new_items:
            save_item(item)
            post_to_discord(item)

        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Completed", "new_games": len(new_items)}),
        }

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def fetch_search_results() -> list[dict]:
    """Steam検索APIを叩いて最新ページのゲーム一覧を取得する。
    レスポンスには name と logo しか含まれないため、appidはlogoのURLから抽出する。
    """
    response = requests.get(SEARCH_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    raw_items = data.get("items", [])

    items = []
    for raw in raw_items:
        name = raw.get("name", "")
        logo = raw.get("logo", "")
        appid = extract_appid(logo)
        if not appid:
            logger.warning(f"logoURLからappidを抽出できませんでした: {logo!r}, スキップ: {name!r}")
            continue
        items.append({"appid": appid, "name": name, "logo": logo})
        logger.info(f"  fetched: appid={appid} name={name!r}")

    return items


def is_initial_run() -> bool:
    """DynamoDBにレコードが1件もなければ初回実行と判定する。"""
    response = table.query(
        KeyConditionExpression=Key("p_key").eq(P_KEY),
        Limit=1,
    )
    return len(response.get("Items", [])) == 0


def filter_new_items(items: list[dict]) -> list[dict]:
    """DynamoDB未登録のアイテムだけを返す。"""
    if not items:
        return []

    keys = [{"p_key": P_KEY, "s_key": f"{S_KEY_PREFIX}{item['appid']}"} for item in items]

    response = dynamodb.batch_get_item(
        RequestItems={table_name: {"Keys": keys, "ConsistentRead": False}}
    )
    existing_keys = {
        record["s_key"] for record in response.get("Responses", {}).get(table_name, [])
    }
    logger.info(f"DynamoDB existing={len(existing_keys)} / fetched={len(items)}")

    new_items = [item for item in items if f"{S_KEY_PREFIX}{item['appid']}" not in existing_keys]
    for item in new_items:
        logger.info(f"  new game: appid={item['appid']} name={item['name']!r}")
    return new_items


def seed_all(items: list[dict]) -> None:
    """初回実行時に全件をDynamoDBへ一括保存する（通知はしない）。

    "seed" はDBの初期データ投入を指す慣用語。
    ここでの目的は「今この瞬間の状態を基準点として記録する」こと。
    2回目以降はここで保存したデータとの差分だけを通知対象にする。

    初回に通知してしまうと検索条件に該当する既存ゲームが大量にDiscordへ流れるため、
    初回は保存のみに留める。
    """
    with table.batch_writer() as writer:
        for item in items:
            writer.put_item(Item=_build_record(item))
    logger.info(f"Seeded {len(items)} items to DynamoDB")


def save_item(item: dict) -> None:
    """新着ゲームをDynamoDBに保存する。"""
    table.put_item(Item=_build_record(item))


def _build_record(item: dict) -> dict:
    return {
        "p_key": P_KEY,
        "s_key": f"{S_KEY_PREFIX}{item['appid']}",
        "appid": item["appid"],
        "name": item.get("name", ""),
        "logo": item.get("logo", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def post_to_discord(item: dict) -> None:
    """新着ゲームをDiscord webhookへ投稿する。"""
    if not webhook_url:
        logger.warning("Webhook URLが設定されていません、通知をスキップします")
        return

    appid = item["appid"]
    name = item.get("name", "Unknown")
    logo = item.get("logo", "")
    store_url = f"https://store.steampowered.com/app/{appid}/"

    embed: dict[str, Any] = {
        "title": name,
        "url": store_url,
        "color": 0x1B2838,  # Steamの濃紺
        "footer": {"text": f"Steam App ID: {appid}"},
    }
    if logo:
        embed["thumbnail"] = {"url": logo}

    payload = {
        "username": "クソゲー発掘まるめし",
        # "avatar_url": "https://store.steampowered.com/favicon.ico",
        "embeds": [embed],
    }
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
    logger.info(f"Discordへ投稿しました: {name} (appid={appid})")
