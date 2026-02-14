"""XZY ranking data fetcher Lambda function.

This function fetches XZY battle record ranking data from API,
stores the latest list_id in DynamoDB, and posts the data to Discord.
"""

import io
import json
import logging
import os
from collections import defaultdict
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

# Constants
P_KEY = "xzy_rank"
S_KEY = "latest_list_id"
API_BASE_URL = "https://xzy.shengtiangames.com/mini-game/xzy/battle-record/hot-rank"
DEFAULT_LIST_ID = 106  # Starting list_id
MAX_SEARCH_INCREMENT = 10  # Maximum number of increments to search

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


def post_to_discord(data: list[dict[str, Any]], list_id: int, webhook_url: str) -> None:
    """Post ranking data to Discord webhook with images.

    Args:
        data: Ranking data from API
        list_id: The list_id of the data
        webhook_url: Discord webhook URL
    """
    try:
        # Sort by on_rate for image generation
        sorted_data = sorted(data, key=lambda x: float(x.get("on_rate", 0)), reverse=True)
        
        # Generate ranking images
        images = generate_ranking_images(sorted_data)
        
        # Prepare embed message
        embed = {
            "title": "今週の 2v2 キャラランキング (6000-8000帯)",
            "description": f"出場率順でソートしたキャラクターランキング\n全{len(sorted_data)}キャラクター",
            "color": 0x5865F2,
            "footer": {
                "text": f"List ID: {list_id} | 取得日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            },
            "timestamp": datetime.now().isoformat()
        }
        
        # Post each image with the embed
        for i, (filename, img) in enumerate(images):
            # Convert image to bytes
            img_bytes = io.BytesIO()
            img.save(img_bytes, format="PNG")
            img_bytes.seek(0)
            
            # Prepare multipart form data
            files = {
                "file": (filename, img_bytes, "image/png")
            }
            
            # Add embed only to the first message
            if i == 0:
                payload = {
                    "embeds": [embed]
                }
            else:
                payload = {}
            
            # Post to Discord
            response = requests.post(
                webhook_url,
                data={"payload_json": json.dumps(payload)},
                files=files,
                timeout=30
            )
            response.raise_for_status()
            
            logger.info(f"Posted image {i+1}/{len(images)} to Discord: {filename}")
        
        logger.info(f"Posted {len(images)} ranking images to Discord (list_id: {list_id})")

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


def download_image(url: str, size: tuple[int, int] = None) -> Image.Image | None:
    """Download and optionally resize an image from URL.
    
    Args:
        url: Image URL
        size: Optional target size (width, height)
        
    Returns:
        PIL Image object or None if download fails
    """
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        img = Image.open(io.BytesIO(response.content))
        
        if size:
            img = img.resize(size, Image.Resampling.LANCZOS)
        
        return img
    except Exception as e:
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
    max_chars_per_row = 8
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
    
    # Draw characters in on_rate order
    for i, char_data in enumerate(chars):
        col = i % max_chars_per_row
        row = i // max_chars_per_row
        
        x = SECTION_MARGIN + col * char_box_width
        y = y_offset + row * char_box_height
        
        # Get character info
        role = char_data.get("role", {})
        avatar_url = role.get("avatar_link") or role.get("avatar_link_xcx") or role.get("img_preview")
        name = role.get("name_jp", "Unknown")
        win_rate = char_data.get("win_rate", "0")
        on_rate = char_data.get("on_rate", "0")
        ban_rate = char_data.get("ban_rate", "0")
        
        # Download and draw thumbnail
        thumbnail = download_image(avatar_url, (THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        if not thumbnail:
            thumbnail = create_placeholder_image((THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        
        img.paste(thumbnail, (x, y))
        
        # Draw stats below thumbnail
        stats_y = y + THUMBNAIL_SIZE + 5
        
        # Draw character name (truncate if too long)
        name_display = name if len(name) <= 6 else name[:5] + "..."
        draw.text((x, stats_y), name_display, fill=(220, 220, 220), font=stats_font)
        draw.text((x, stats_y + 15), f"勝率: {win_rate}%", fill=(100, 200, 100), font=stats_font)
        draw.text((x, stats_y + 28), f"出場率: {on_rate}%", fill=(100, 150, 255), font=stats_font)
        draw.text((x, stats_y + 41), f"BAN率: {ban_rate}%", fill=(255, 100, 100), font=stats_font)
    
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
    cost_images = []
    for cost in sorted(grouped.keys(), key=lambda x: float(x) if x != "Unknown" else 999):
        chars = grouped[cost]
        img = generate_cost_image(cost, chars)
        cost_images.append(img)
    
    # Calculate total height for combined image
    total_width = max(img.width for img in cost_images)
    total_height = sum(img.height for img in cost_images)
    
    # Create combined image
    combined_img = Image.new("RGB", (total_width, total_height), color=(30, 30, 40))
    
    # Paste each cost image vertically
    y_offset = 0
    for img in cost_images:
        combined_img.paste(img, (0, y_offset))
        y_offset += img.height
    
    filename = "xzy_rank_all_costs.png"
    logger.info(f"Generated combined ranking image: {total_width}x{total_height}px")
    
    return [(filename, combined_img)]
