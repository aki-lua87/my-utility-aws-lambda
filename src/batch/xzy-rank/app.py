"""XZY ranking data fetcher Lambda function.

This function fetches XZY battle record ranking data from API,
stores the latest list_id in DynamoDB, and posts the data to Discord.
"""

import hashlib
import io
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import boto3
import requests
from PIL import Image, ImageDraw, ImageFont

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
table_name = os.environ.get("TABLE_NAME", "aki-utils-dev")
table = dynamodb.Table(table_name)
s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name="ap-northeast-1")
image_cache_bucket = os.environ.get("IMAGE_CACHE_BUCKET", "")
bedrock_analysis_enabled = os.environ.get("BEDROCK_ANALYSIS_ENABLED", "false").lower() == "true"

# Constants
P_KEY = "xzy_rank"
S_KEY = "latest_list_id"
API_BASE_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/hot-rank"
DEFAULT_LIST_ID = 106  # Starting list_id
MAX_SEARCH_INCREMENT = 10  # Maximum number of increments to search
IMAGE_CACHE_PREFIX = "images/"  # S3 key prefix for cached images
BEDROCK_MODEL_ID = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"

# Image generation constants
THUMBNAIL_SIZE = 80  # Character thumbnail size
CHAR_SPACING = 10  # Spacing between characters horizontally
CHAR_VERTICAL_SPACING = 75  # Vertical spacing between rows (includes stats height)
SECTION_MARGIN = 40  # Margin around sections
FONT_SIZE_TITLE = 24
FONT_SIZE_STATS = 14


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for XZY ranking data fetcher.

    Args:
        event: Lambda event object
        context: Lambda context object

    Returns:
        Response dict with status code and body
    """
    try:
        t_start = time.time()
        logger.info("Starting XZY ranking data fetch")

        # Get Discord webhook URL from environment
        webhook_url = os.environ.get("XZY_WEBHOOK_URL")
        if not webhook_url:
            logger.error("XZY_WEBHOOK_URL environment variable not set")
            return {"statusCode": 500, "body": json.dumps({"error": "Webhook URL not configured"})}

        # Get last list_id from DynamoDB
        t0 = time.time()
        last_list_id = get_last_list_id()
        logger.info(f"[{time.time() - t0:.2f}s] get_last_list_id: {last_list_id}")

        # Search for the latest list_id with data
        t0 = time.time()
        latest_data, latest_list_id = find_latest_data(last_list_id)
        logger.info(f"[{time.time() - t0:.2f}s] find_latest_data: list_id={latest_list_id}, count={len(latest_data)}")

        if not latest_data:
            logger.warning("No new data found")
            return {"statusCode": 200, "body": json.dumps({"message": "No new data found"})}

        # Save the latest list_id to DynamoDB
        t0 = time.time()
        save_last_list_id(latest_list_id)
        logger.info(f"[{time.time() - t0:.2f}s] save_last_list_id")

        # Bedrock analysis (find a previous list_id with different data, up to 5 decrements)
        analysis = None
        if bedrock_analysis_enabled:
            t0 = time.time()
            current_shrunk = shrink_ranking_data(latest_data)
            previous_data = None
            prev_list_id = None

            for decrement in range(1, 6):
                candidate_id = latest_list_id - decrement
                prev_response = fetch_ranking_data(candidate_id)
                if not prev_response or prev_response.get("code") != 0 or not prev_response.get("data"):
                    logger.warning(f"Could not fetch data for list_id {candidate_id}, stopping search")
                    break
                candidate_shrunk = shrink_ranking_data(prev_response["data"])
                if candidate_shrunk != current_shrunk:
                    previous_data = prev_response["data"]
                    prev_list_id = candidate_id
                    logger.info(f"Found different data at list_id {candidate_id} (decrement={decrement})")
                    break
                logger.info(f"list_id {candidate_id} is identical to current, trying next...")

            if previous_data:
                analysis = analyze_with_bedrock(latest_data, previous_data)
                logger.info(f"[{time.time() - t0:.2f}s] bedrock analysis (list_id {latest_list_id} vs {prev_list_id})")
            else:
                logger.warning("No different previous data found within 5 decrements, skipping analysis")

        # Post to Discord
        t0 = time.time()
        post_to_discord(latest_data, latest_list_id, webhook_url, analysis)
        logger.info(f"[{time.time() - t0:.2f}s] post_to_discord")

        logger.info(f"[{time.time() - t_start:.2f}s] total: Successfully completed XZY ranking data fetch")
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
    # Sort by on_rate descending (should already be sorted from API)
    sorted_data = sorted(data, key=lambda x: float(x.get("on_rate", 0)), reverse=True)

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


def shrink_ranking_data(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract only fields needed for analysis, sorted by on_rate descending.

    Args:
        data: Raw ranking data from API

    Returns:
        Minimal list with name_jp, cost, win_rate, on_rate, ban_rate only
    """
    return [
        {
            "name": item.get("role", {}).get("name_jp", "Unknown"),
            "cost": item.get("role", {}).get("cost", "?"),
            "win_rate": item.get("win_rate", "0"),
            "on_rate": item.get("on_rate", "0"),
            "ban_rate": item.get("ban_rate", "0"),
        }
        for item in sorted(data, key=lambda x: float(x.get("on_rate", 0)), reverse=True)
    ]


def analyze_with_bedrock(
    current_data: list[dict[str, Any]],
    previous_data: list[dict[str, Any]],
) -> str | None:
    """Analyze ranking changes using Amazon Bedrock (Claude).

    Args:
        current_data: Current week's ranking data
        previous_data: Previous week's ranking data

    Returns:
        Analysis text or None if disabled/failed
    """
    if not bedrock_analysis_enabled:
        return None

    try:
        current_summary = shrink_ranking_data(current_data)
        previous_summary = shrink_ranking_data(previous_data)
        logger.info(f"current_summary: {current_summary}")
        logger.info(f"previous_summary: {previous_summary}")

        prompt = f"""あなたはゲーム「星の翼(星之翼)」の2v2キャラクターランキングを分析するアナリストです。
先週と今週のランキングデータを比較して、日本語で簡潔に分析してください。

# 先週のランキング（出場率順）
{json.dumps(previous_summary, ensure_ascii=False, indent=2)}

# 今週のランキング（出場率順）
{json.dumps(current_summary, ensure_ascii=False, indent=2)}

以下の観点で分析してください：
- 出場率・勝率・BAN率が大きく変動したキャラ
- 新たに注目されたキャラや評価が下がったキャラ
- 今週の環境のポイント（2〜3行程度のサマリー）

400文字以内でまとめてください。"""

        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        result = json.loads(response["body"].read())
        analysis = result["content"][0]["text"]
        logger.info(f"Bedrock analysis completed: {len(analysis)} chars")
        return analysis

    except Exception as e:
        logger.error(f"Bedrock analysis failed: {e}", exc_info=True)
        return None


def post_to_discord(
    data: list[dict[str, Any]],
    list_id: int,
    webhook_url: str,
    analysis: str | None = None,
) -> None:
    """Post ranking data to Discord webhook with images.

    Args:
        data: Ranking data from API
        list_id: The list_id of the data
        webhook_url: Discord webhook URL
        analysis: Optional Bedrock analysis text
    """
    try:
        # Sort by on_rate for image generation
        sorted_data = sorted(data, key=lambda x: float(x.get("on_rate", 0)), reverse=True)
        
        # Generate ranking images
        t0 = time.time()
        images = generate_ranking_images(sorted_data)
        logger.info(f"[{time.time() - t0:.2f}s] generate_ranking_images: {len(images)} image(s)")

        # Encode all images to bytes
        t0 = time.time()
        encoded = []
        for filename, img in images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            encoded.append((filename, buf))
        logger.info(f"[{time.time() - t0:.2f}s] encode {len(encoded)} images")

        # Build content text
        content = "今週の 2v2 キャラランキング (6000-8000帯)"
        if analysis:
            content += f"\n\n{analysis}"

        # Attach all images in a single Discord message (max 10 files)
        files = {f"files[{i}]": (filename, buf, "image/png") for i, (filename, buf) in enumerate(encoded)}
        payload = {"content": content}

        t0 = time.time()
        response = requests.post(
            webhook_url,
            data={"payload_json": json.dumps(payload)},
            files=files,
            timeout=60
        )
        response.raise_for_status()
        logger.info(f"[{time.time() - t0:.2f}s] posted {len(encoded)} images to Discord in single message")

    except Exception as e:
        logger.error(f"Error posting to Discord: {e}", exc_info=True)
        raise


def group_by_cost(data: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group characters by cost, maintaining on_rate sort order.
    
    Args:
        data: Ranking data from API (should be pre-sorted by on_rate)
        
    Returns:
        Dict: {cost: [characters sorted by on_rate]}
    """
    grouped = defaultdict(list)
    
    for item in data:
        role = item.get("role", {})
        cost = role.get("cost", "Unknown")
        grouped[cost].append(item)
    
    return grouped


def _s3_key_from_url(url: str) -> str:
    """Generate S3 cache key from image URL."""
    url_hash = hashlib.md5(url.encode()).hexdigest()
    return f"{IMAGE_CACHE_PREFIX}{url_hash}.png"


def _load_from_s3_cache(s3_key: str, size: tuple[int, int] | None) -> Image.Image | None:
    """Try to load image from S3 cache.
    
    Returns:
        PIL Image object or None if not cached
    """
    if not image_cache_bucket:
        return None
    try:
        obj = s3.get_object(Bucket=image_cache_bucket, Key=s3_key)
        img = Image.open(io.BytesIO(obj["Body"].read()))
        if size:
            img = img.resize(size, Image.Resampling.LANCZOS)
        return img
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning(f"S3 cache read error ({s3_key}): {e}")
        return None


def _save_to_s3_cache(s3_key: str, img: Image.Image) -> None:
    """Save image to S3 cache."""
    if not image_cache_bucket:
        return
    try:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        s3.put_object(Bucket=image_cache_bucket, Key=s3_key, Body=buf, ContentType="image/png")
        logger.info(f"Saved to S3 cache: {s3_key}")
    except Exception as e:
        logger.warning(f"S3 cache write error ({s3_key}): {e}")


def download_image(url: str, size: tuple[int, int] = None, retries: int = 3) -> Image.Image | None:
    """Download and optionally resize an image from URL. S3 cache is checked first.
    
    Args:
        url: Image URL
        size: Optional target size (width, height)
        retries: Number of retry attempts on failure
        
    Returns:
        PIL Image object or None if download fails
    """
    s3_key = _s3_key_from_url(url)

    # Check S3 cache first
    cached = _load_from_s3_cache(s3_key, size)
    if cached:
        logger.info(f"S3 cache hit: {s3_key}")
        return cached

    # Download from origin with exponential backoff
    for attempt in range(1, retries + 1):
        timeout = 2 ** attempt  # 2s, 4s, 8s
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            
            img = Image.open(io.BytesIO(response.content))

            # Save original (unresized) image to S3 cache
            _save_to_s3_cache(s3_key, img)

            if size:
                img = img.resize(size, Image.Resampling.LANCZOS)
            
            return img
        except Exception as e:
            if attempt < retries:
                wait = 2 ** (attempt - 1)  # 1s, 2s
                logger.warning(f"Failed to download image (attempt {attempt}/{retries}, timeout={timeout}s) from {url}: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                logger.warning(f"Failed to download image from {url}: {e}")
    
    return None


def create_placeholder_image(size: tuple[int, int]) -> Image.Image:
    """Create a placeholder image when character thumbnail is unavailable.
    
    Args:
        size: Image size (width, height)
        
    Returns:
        PIL Image object
    """
    img = Image.new("RGB", size, color=(50, 50, 50))
    draw = ImageDraw.Draw(img)
    
    # Draw a simple "?" in the center
    text = "?"
    # Use default font for now
    bbox = draw.textbbox((0, 0), text)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    position = ((size[0] - text_width) // 2, (size[1] - text_height) // 2)
    draw.text(position, text, fill=(200, 200, 200))
    
    return img


def get_font(size: int) -> ImageFont.FreeTypeFont:
    """Get font for text rendering. Falls back to default if custom font unavailable.
    
    Args:
        size: Font size
        
    Returns:
        ImageFont object
    """
    # Get the directory where this script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Try to use Japanese font if available
    font_paths = [
        os.path.join(script_dir, "fonts", "NotoSansJP-Regular.ttf"),  # Local font
        "/var/task/fonts/NotoSansJP-Regular.ttf",  # Lambda environment
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",  # Lambda Linux
        "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",  # macOS
        "C:\\Windows\\Fonts\\msgothic.ttc",  # Windows
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    
    for font_path in font_paths:
        try:
            return ImageFont.truetype(font_path, size)
        except (OSError, IOError):
            continue
    
    # Fallback to default font
    logger.warning(f"Japanese font not found, using default font")
    return ImageFont.load_default()


def generate_cost_image(cost: str, chars: list[dict[str, Any]]) -> Image.Image:
    """Generate ranking image for a specific cost category.
    
    Args:
        cost: Cost value (e.g., "1.0", "1.5")
        chars: List of characters sorted by on_rate
        
    Returns:
        PIL Image object
    """
    # Calculate image dimensions
    max_chars_per_row = 4
    char_box_width = THUMBNAIL_SIZE + CHAR_SPACING
    char_box_height = THUMBNAIL_SIZE + CHAR_VERTICAL_SPACING
    
    # Calculate total height needed
    rows = (len(chars) + max_chars_per_row - 1) // max_chars_per_row
    total_height = SECTION_MARGIN  # Top margin
    total_height += 40  # Title height
    total_height += rows * char_box_height
    total_height += SECTION_MARGIN  # Bottom margin
    
    img_width = SECTION_MARGIN * 2 + max_chars_per_row * char_box_width
    img = Image.new("RGB", (img_width, total_height), color=(30, 30, 40))
    draw = ImageDraw.Draw(img)
    
    # Fonts
    title_font = get_font(FONT_SIZE_TITLE)
    stats_font = get_font(FONT_SIZE_STATS)
    
    # Draw title
    title = f"コスト {cost} (出場率順)"
    draw.text((SECTION_MARGIN, SECTION_MARGIN), title, fill=(255, 255, 255), font=title_font)
    
    y_offset = SECTION_MARGIN + 40

    # Pre-fetch character info
    char_infos = []
    for char_data in chars:
        role = char_data.get("role", {})
        char_infos.append({
            "avatar_url": role.get("avatar_link") or role.get("avatar_link_xcx") or role.get("img_preview"),
            "name": role.get("name_jp", "Unknown"),
            "win_rate": char_data.get("win_rate", "0"),
            "on_rate": char_data.get("on_rate", "0"),
            "ban_rate": char_data.get("ban_rate", "0"),
        })

    # Download all thumbnails in parallel
    def fetch(idx: int, info: dict) -> tuple[int, Image.Image]:
        t0 = time.time()
        thumb = download_image(info["avatar_url"], (THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        elapsed = time.time() - t0
        if not thumb:
            logger.info(f"[{elapsed:.2f}s] download failed, using placeholder: {info['name']}")
            thumb = create_placeholder_image((THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        else:
            logger.info(f"[{elapsed:.2f}s] download OK: {info['name']}")
        return idx, thumb

    thumbnails: dict[int, Image.Image] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch, i, info): i for i, info in enumerate(char_infos)}
        for future in as_completed(futures):
            idx, thumb = future.result()
            thumbnails[idx] = thumb

    # Draw characters in on_rate order
    for i, info in enumerate(char_infos):
        col = i % max_chars_per_row
        row = i // max_chars_per_row
        
        x = SECTION_MARGIN + col * char_box_width
        y = y_offset + row * char_box_height
        
        img.paste(thumbnails[i], (x, y))
        
        # Draw stats below thumbnail
        stats_y = y + THUMBNAIL_SIZE + 5
        
        # Draw character name (truncate if too long)
        name_display = info["name"] if len(info["name"]) <= 6 else info["name"][:5] + "..."
        draw.text((x, stats_y), name_display, fill=(220, 220, 220), font=stats_font)
        draw.text((x, stats_y + 15), f"勝率: {info['win_rate']}%", fill=(100, 200, 100), font=stats_font)
        draw.text((x, stats_y + 28), f"出場率: {info['on_rate']}%", fill=(100, 150, 255), font=stats_font)
        draw.text((x, stats_y + 41), f"BAN率: {info['ban_rate']}%", fill=(255, 100, 100), font=stats_font)
    
    return img


def generate_ranking_images(data: list[dict[str, Any]]) -> list[tuple[str, Image.Image]]:
    """Generate a single ranking image with all costs.
    
    Args:
        data: Ranking data from API (should be pre-sorted by on_rate)
        
    Returns:
        List with single (filename, image) tuple
    """
    # Group by cost (maintaining on_rate sort order)
    grouped = group_by_cost(data)
    
    # Generate individual cost images
    result = []
    for cost in sorted(grouped.keys(), key=lambda x: float(x) if x != "Unknown" else -1, reverse=True):
        chars = grouped[cost]
        t0 = time.time()
        img = generate_cost_image(cost, chars)
        cost_str = str(cost).replace(".", "_")
        filename = f"xzy_rank_cost_{cost_str}.png"
        logger.info(f"[{time.time() - t0:.2f}s] generate_cost_image: cost={cost}, chars={len(chars)}, size={img.width}x{img.height}px")
        result.append((filename, img))
    
    return result
