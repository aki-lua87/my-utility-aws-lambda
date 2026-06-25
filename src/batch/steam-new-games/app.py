"""Steam新着ゲーム監視 Lambda関数。

特定の検索条件でSteamストアを監視し、新着ゲームをDiscordに通知する。
初回実行時（DynamoDB空）は既存ゲームを全件保存するのみで通知はしない。
"""

import html
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
bedrock = boto3.client("bedrock-runtime", region_name="ap-northeast-1")
webhook_url = os.environ.get("STEAM_NEW_GAMES_WEBHOOK_URL", "")
bedrock_analysis_enabled = os.environ.get("BEDROCK_ANALYSIS_ENABLED", "false").lower() == "true"

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

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
BEDROCK_MODEL_ID = "apac.amazon.nova-pro-v1:0"
# 1回の実行で分析する新着ゲーム数に上限を設ける（コスト急増防止）
MAX_BEDROCK_ANALYSIS_PER_RUN = int(os.environ.get("MAX_BEDROCK_ANALYSIS_PER_RUN", "10"))


def extract_appid(logo_url: str) -> str | None:
    """カプセル画像URLからSteam App IDを抽出する。
    例: .../steam/apps/4451000/capsule_sm_120.jpg → "4451000"
    """
    match = re.search(r"/apps/(\d+)/", logo_url)
    return match.group(1) if match else None


def extract_json_object(text: str) -> str:
    """モデル出力からJSONオブジェクト部分を取り出す（```json フェンス等を除去）。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return match.group(0) if match else text


def strip_html(text: str) -> str:
    """HTMLタグを除去し、HTMLエンティティをデコードして整形する。"""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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

        for i, item in enumerate(new_items):
            details = fetch_app_details(item["appid"])
            reviews = fetch_app_reviews(item["appid"])
            analysis = analyze_with_bedrock(item["name"], details, reviews) if i < MAX_BEDROCK_ANALYSIS_PER_RUN else None
            save_item(item)
            post_to_discord(item, details, reviews, analysis)

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


def fetch_app_details(appid: str) -> dict[str, Any] | None:
    """Steam Store APIからゲームの概要・カテゴリ・ジャンルを取得する。"""
    try:
        response = requests.get(
            APPDETAILS_URL,
            params={"appids": appid, "l": "japanese", "cc": "jp"},
            timeout=10,
        )
        response.raise_for_status()
        entry = response.json().get(appid, {})
        if not entry.get("success"):
            return None

        data = entry.get("data", {})
        about = data.get("short_description") or data.get("about_the_game", "")
        return {
            "about": strip_html(about),
            "categories": [c.get("description", "") for c in data.get("categories", [])],
            "genres": [g.get("description", "") for g in data.get("genres", [])],
        }
    except Exception as e:
        logger.warning(f"appdetails取得に失敗しました (appid={appid}): {e}")
        return None


def fetch_app_reviews(appid: str) -> dict[str, Any] | None:
    """Steam appreviews APIからレビュー概況と不評・好評レビューを取得する。
    ネガティブレビューを先に3件、次にポジティブレビューを2件取得する。
    """
    try:
        neg_response = requests.get(
            APPREVIEWS_URL.format(appid=appid),
            params={
                "json": 1,
                "language": "japanese",
                "filter": "all",
                "review_type": "negative",
                "num_per_page": 3,
                "purchase_type": "all",
            },
            timeout=10,
        )
        neg_response.raise_for_status()
        neg_data = neg_response.json()
        summary = neg_data.get("query_summary", {})
        if neg_data.get("success") != 1 or not summary.get("total_reviews"):
            return None

        negative_samples = [
            strip_html(r["review"])[:300]
            for r in neg_data.get("reviews", [])
            if r.get("review")
        ][:3]

        pos_response = requests.get(
            APPREVIEWS_URL.format(appid=appid),
            params={
                "json": 1,
                "language": "japanese",
                "filter": "all",
                "review_type": "positive",
                "num_per_page": 2,
                "purchase_type": "all",
            },
            timeout=10,
        )
        pos_response.raise_for_status()
        pos_data = pos_response.json()
        positive_samples = [
            strip_html(r["review"])[:300]
            for r in pos_data.get("reviews", [])
            if r.get("review")
        ][:2]

        return {
            "review_score_desc": summary.get("review_score_desc", ""),
            "total_positive": summary.get("total_positive", 0),
            "total_negative": summary.get("total_negative", 0),
            "negative_samples": negative_samples,
            "positive_samples": positive_samples,
        }
    except Exception as e:
        logger.warning(f"レビュー取得に失敗しました (appid={appid}): {e}")
        return None


def analyze_with_bedrock(name: str, details: dict[str, Any] | None, reviews: dict[str, Any] | None) -> dict[str, str] | None:
    """ゲーム概要・レビューをもとにBedrock(Claude)で評価コメントを生成する。"""
    if not bedrock_analysis_enabled:
        return None

    try:
        about = (details or {}).get("about", "")
        categories = (details or {}).get("categories", [])
        genres = (details or {}).get("genres", [])

        review_section = ""
        if reviews:
            review_section = (
                "\n\n# レビュー\n"
                f"評価: {reviews['review_score_desc']}"
                f"（賛成 {reviews['total_positive']} / 不評 {reviews['total_negative']}）\n"
            )
            if reviews.get("negative_samples"):
                neg_text = "\n".join(f"  - {s}" for s in reviews["negative_samples"])
                review_section += f"## 不評レビュー\n{neg_text}\n"
            if reviews.get("positive_samples"):
                pos_text = "\n".join(f"  - {s}" for s in reviews["positive_samples"])
                review_section += f"## 好評レビュー\n{pos_text}\n"

        prompt = f"""あなたはSteamの新着ゲームを紹介するアシスタントです。以下の情報を読んで、日本語で簡潔に分析してください。
レビューがある場合は、その内容（プレイヤーの実際の感想）を評価の根拠として重視してください。
特に不評レビューに注目し、問題点や欠点を正直に伝えてください。

# タイトル
{name}

# 概要
{about or "(概要情報なし)"}

# カテゴリ
{", ".join(categories) or "不明"}

# ジャンル
{", ".join(genres) or "不明"}{review_section}

以下のJSON形式で出力してください（説明文や```は不要、JSONのみ）:
{{
  "multi_play": "マルチプレイ対応かどうかを明記し、対応の場合は3〜4人プレイの可否も記載。レビューに言及があれば触れること（50文字程度）",
  "session_length": "1回の配信・実況セッションにどれくらいの時間/雰囲気が向いているか。不評レビューで指摘された問題点があれば触れること（100文字程度）"
}}"""

        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 512},
        )
        text = response["output"]["message"]["content"][0]["text"]
        analysis = json.loads(extract_json_object(text))
        return {
            "multi_play": str(analysis.get("multi_play", "")).strip()[:200] or "情報なし",
            "session_length": str(analysis.get("session_length", "")).strip()[:200] or "情報なし",
        }
    except Exception as e:
        logger.error(f"Bedrock analysis failed (name={name}): {e}", exc_info=True)
        return None


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


def post_to_discord(
    item: dict,
    details: dict[str, Any] | None = None,
    reviews: dict[str, Any] | None = None,
    analysis: dict[str, str] | None = None,
) -> None:
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
    if details and details.get("about"):
        embed["description"] = details["about"][:300]
    if reviews:
        review_text = f"{reviews['review_score_desc']}（賛成 {reviews['total_positive']} / 不評 {reviews['total_negative']}）"
        embed["fields"] = [{"name": "レビュー", "value": review_text, "inline": False}]

    embeds = [embed]
    if analysis:
        embeds.append(
            {
                "title": "AI評価",
                "color": 0x57F287,  # Discordグリーン
                "fields": [
                    {"name": "マルチプレイ対応", "value": analysis["multi_play"], "inline": False},
                    {"name": "配信時間の目安", "value": analysis["session_length"], "inline": False},
                ],
            }
        )

    payload = {
        "username": "クソゲー発掘まるめし",
        # "avatar_url": "https://store.steampowered.com/favicon.ico",
        "embeds": embeds,
    }
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
    logger.info(f"Discordへ投稿しました: {name} (appid={appid})")
