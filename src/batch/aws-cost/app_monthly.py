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


def get_two_months_period() -> tuple[str, str, str]:
    """先々月・先月の期間を取得 (Cost Explorer は半開区間: [start, end))
    Returns: (先々月開始, 先月開始, 今月開始)
    """
    today = date.today()
    first_of_this_month = today.replace(day=1)
    first_of_last_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
    first_of_two_months_ago = (first_of_last_month - timedelta(days=1)).replace(day=1)
    return (
        first_of_two_months_ago.strftime("%Y-%m-%d"),
        first_of_last_month.strftime("%Y-%m-%d"),
        first_of_this_month.strftime("%Y-%m-%d"),
    )


def get_cost_and_usage(start: str, end: str) -> dict[str, Any]:
    """AWS Cost Explorer からコストデータを取得（MONTHLY 粒度で複数月対応）"""
    ce = boto3.client("ce", region_name="us-east-1")
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    return response


def extract_service_costs(results_by_time: dict[str, Any]) -> dict[str, float]:
    """ResultsByTime の1要素からサービス別コストを dict で返す"""
    return {
        group["Keys"][0]: float(group["Metrics"]["UnblendedCost"]["Amount"])
        for group in results_by_time["Groups"]
    }


def build_discord_payload(cost_data: dict[str, Any], prev_start: str, last_start: str, end: str) -> dict[str, Any]:
    """月次レポート用 Discord Embed を構築（前月比較付き）"""
    results = cost_data["ResultsByTime"]
    # results[0] = 先々月, results[1] = 先月
    prev_costs = extract_service_costs(results[0])
    last_costs = extract_service_costs(results[1])

    prev_total = sum(prev_costs.values())
    last_total = sum(last_costs.values())
    total_diff = last_total - prev_total
    total_diff_str = f"+${total_diff:.4f}" if total_diff >= 0 else f"-${abs(total_diff):.4f}"

    # 先月コストで降順ソート（0円除く）
    services = sorted(
        [(name, amt) for name, amt in last_costs.items() if amt > 0],
        key=lambda x: x[1],
        reverse=True,
    )

    year, month, *_ = last_start.split("-")
    prev_year, prev_month, *_ = prev_start.split("-")
    month_label = f"{year}年{int(month)}月"
    prev_month_label = f"{prev_year}年{int(prev_month)}月"

    fields = []
    for service, amount in services[:MAX_SERVICE_DISPLAY]:
        prev_amount = prev_costs.get(service, 0.0)
        diff = amount - prev_amount
        if diff > 0.00005:
            diff_str = f" (▲ +${diff:.4f})"
        elif diff < -0.00005:
            diff_str = f" (▼ -${abs(diff):.4f})"
        else:
            diff_str = ""
        fields.append({"name": service, "value": f"`${amount:.4f}`{diff_str}", "inline": True})

    end_display = (date.fromisoformat(end) - timedelta(days=1)).strftime("%Y-%m-%d")

    embed: dict[str, Any] = {
        "title": f"📋 {month_label} AWS利用料（月次確定）",
        "description": (
            f"**合計: ${last_total:.4f} USD**　({total_diff_str} 対{prev_month_label}比)"
        ),
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": f"集計期間: {last_start} 〜 {end_display}　※▲増加 ▼減少"},
    }

    return {"embeds": [embed]}


def post_monthly_cost_to_discord() -> None:
    """先月のAWS利用料を Cost Explorer から取得して Discord に送信（前月比較付き）"""
    prev_start, last_start, end = get_two_months_period()
    cost_data = get_cost_and_usage(prev_start, end)
    payload = build_discord_payload(cost_data, prev_start, last_start, end)

    response = requests.post(AWS_COST_WEBHOOK_URL, json=payload, timeout=10)
    response.raise_for_status()
