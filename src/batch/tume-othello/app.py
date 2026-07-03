import os
import re
from io import BytesIO
from typing import Any

import requests
from PIL import Image, ImageDraw

# 定数定義
OTHELLO_URL = "https://www.othello.org/egev/"
BOARD_SIZE = 400
GRID_SIZE = 8
STONE_MARGIN = 4
BOARD_COLOR = (0, 128, 0)
BLACK_STONE = "1"
WHITE_STONE = "2"
TURN_MESSAGES = {"1": "黒の手番", "2": "白の手番"}

WEB_HOOK_URL = os.environ["WEB_HOOK_URL"]


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda関数のエントリーポイント"""
    try:
        create_othello_image_and_post_to_discord()
        return {"statusCode": 200, "body": "Success"}
    except Exception as e:
        print(f"Error: {e}")
        return {"statusCode": 500, "body": f"Error: {str(e)}"}


def fetch_game_data() -> tuple[str, str]:
    """オセロサイトから盤面データと手番情報を取得"""
    try:
        response = requests.get(OTHELLO_URL, timeout=10)
        response.raise_for_status()
        html = response.text

        board_match = re.search(r'var gBoard = "([^"]+)"', html)
        print(f"Fetched board data: {board_match.group(1) if board_match else 'Not found'}")
        turn_match = re.search(r'var gTurn\s*=\s*"([^"]+)"', html)
        print(f"Fetched turn data: {turn_match.group(1) if turn_match else 'Not found'}")
        if not board_match or not turn_match:
            raise ValueError("盤面データまたは手番情報が見つかりません")

        return board_match.group(1), turn_match.group(1)
    except requests.RequestException as e:
        raise Exception(f"Webサイトからのデータ取得に失敗: {e}") from e


def create_board_image(board_data: str) -> Image.Image:
    """盤面データから画像を生成"""
    cell_size = BOARD_SIZE // GRID_SIZE
    board_img = Image.new("RGB", (BOARD_SIZE, BOARD_SIZE), BOARD_COLOR)
    draw = ImageDraw.Draw(board_img)

    # グリッド線を描画
    draw_grid_lines(draw, cell_size)

    # 石を描画
    draw_stones(draw, board_data, cell_size)

    return board_img


def draw_grid_lines(draw: ImageDraw.Draw, cell_size: int) -> None:
    """グリッド線を描画"""
    for i in range(GRID_SIZE + 1):
        # 縦線
        draw.line((i * cell_size, 0, i * cell_size, BOARD_SIZE), fill="black")
        # 横線
        draw.line((0, i * cell_size, BOARD_SIZE, i * cell_size), fill="black")


def draw_stones(draw: ImageDraw.Draw, board_data: str, cell_size: int) -> None:
    """石を描画"""
    stone_radius = cell_size // 2 - STONE_MARGIN

    for idx, stone in enumerate(board_data):
        if stone in [BLACK_STONE, WHITE_STONE]:
            x, y = idx % GRID_SIZE, idx // GRID_SIZE
            center_x = x * cell_size + cell_size // 2
            center_y = y * cell_size + cell_size // 2

            stone_bounds = (
                center_x - stone_radius,
                center_y - stone_radius,
                center_x + stone_radius,
                center_y + stone_radius,
            )

            if stone == BLACK_STONE:
                draw.ellipse(stone_bounds, fill="black")
            elif stone == WHITE_STONE:
                draw.ellipse(stone_bounds, fill="white", outline="black")


def post_to_discord(image: Image.Image, turn: str) -> None:
    """Discordに画像とメッセージを投稿"""
    try:
        img_bytes = BytesIO()
        image.save(img_bytes, format="PNG")
        img_bytes.seek(0)

        files = {"file": ("othello_board.png", img_bytes, "image/png")}
        data = {"content": "今日の詰めオセロの時間だぞ" + "(" + TURN_MESSAGES.get(turn, "") + ")\n" + OTHELLO_URL}

        response = requests.post(WEB_HOOK_URL, data=data, files=files, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise Exception(f"Discordへの投稿に失敗: {e}") from e


def create_othello_image_and_post_to_discord() -> None:
    """メイン処理：オセロ盤面を取得して画像化し、Discordに投稿"""
    board_data, turn = fetch_game_data()
    board_image = create_board_image(board_data)
    post_to_discord(board_image, turn)
