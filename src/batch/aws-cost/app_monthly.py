import os
from datetime import date, timedelta
from typing import Any

import boto3
import requests

AWS_COST_WEBHOOK_URL = os.environ["AWS_COST_WEBHOOK_URL"]

EMBED_COLOR = 0xFF9900
MAX_SERVICE_DISPLAY = 15


def lambda_handler_monthly(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """月次レポート（Cost Explorer API 使用・$0.01/回）"""
    try:
        post_monthly_cost_to_discord()
        return {"statusCode": 200, "body": "Success"}
    except Exception as e:
        print(f"Error: {e}")
        return {"statusCode": 500, "body": f"Error: {str(e)}"}


def get_last_month_period() -> tuple[str, str]:
    """先月の開始日・終了日を取得 (Cost Explorer は半開区間: [start, end))"""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    last_of_last_month = first_of_this_month - timedelta(days=1)
    first_of_last_month = last_of_last_month.replace(day=1)
    return (
        first_of_last_month.strftime("%Y-%m-%d"),
        first_of_this_month.strftime("%Y-%m-%d"),  # 半開区間の終端
    )


def get_cost_and_usage(start: str, end: str) -> dict[str, Any]:
    """AWS Cost Explorer からコストデータを取得"""
    ce = boto3.client("ce", region_name="us-east-1")
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    return response


def build_discord_payload(cost_data: dict[str, Any], start: str, end: str) -> dict[str, Any]:
    """月次レポート用 Discord Embed を構築"""
    results = cost_data["ResultsByTime"][0]
    total = sum(
        float(group["Metrics"]["UnblendedCost"]["Amount"]) for group in results["Groups"]
    )

    services = [
        (group["Keys"][0], float(group["Metrics"]["UnblendedCost"]["Amount"]))
        for group in results["Groups"]
        if float(group["Metrics"]["UnblendedCost"]["Amount"]) > 0
    ]
    services.sort(key=lambda x: x[1], reverse=True)

    year, month, *_ = start.split("-")
    month_label = f"{year}年{int(month)}月"

    fields = [
        {"name": service, "value": f"`${amount:.4f}`", "inline": True}
        for service, amount in services[:MAX_SERVICE_DISPLAY]
    ]

    # end は半開区間なので表示上は1日前の日付にする
    end_display = (date.fromisoformat(end) - timedelta(days=1)).strftime("%Y-%m-%d")

    embed: dict[str, Any] = {
        "title": f"📋 {month_label} AWS利用料（月次確定）",
        "description": f"**合計: ${total:.4f} USD**",
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": f"集計期間: {start} 〜 {end_display}"},
    }

    return {"embeds": [embed]}


def post_monthly_cost_to_discord() -> None:
    """先月のAWS利用料を Cost Explorer から取得して Discord に送信"""
    start, end = get_last_month_period()
    cost_data = get_cost_and_usage(start, end)
    payload = build_discord_payload(cost_data, start, end)

    response = requests.post(AWS_COST_WEBHOOK_URL, json=payload, timeout=10)
    response.raise_for_status()
