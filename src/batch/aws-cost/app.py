import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import requests

AWS_COST_WEBHOOK_URL = os.environ["AWS_COST_WEBHOOK_URL"]

EMBED_COLOR = 0xFF9900
MAX_SERVICE_DISPLAY = 15


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """日次レポート（CloudWatch Metrics 使用・無料）"""
    try:
        post_daily_cost_to_discord()
        return {"statusCode": 200, "body": "Success"}
    except Exception as e:
        print(f"Error: {e}")
        return {"statusCode": 500, "body": f"Error: {str(e)}"}


def get_billing_period() -> tuple[datetime, datetime]:
    """昨日の開始・終了時刻を取得（UTC）"""
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
    return start, end


def get_total_cost(cw: Any, start: datetime, end: datetime) -> float:
    """合計コスト (EstimatedCharges) を取得"""
    response = cw.get_metric_statistics(
        MetricName="EstimatedCharges",
        Namespace="AWS/Billing",
        Period=86400,
        StartTime=start,
        EndTime=end,
        Statistics=["Maximum"],
        Dimensions=[{"Name": "Currency", "Value": "USD"}],
    )
    datapoints = response["Datapoints"]
    if not datapoints:
        return 0.0
    return max(dp["Maximum"] for dp in datapoints)


def get_service_names(cw: Any) -> list[str]:
    """請求データが存在するサービス名一覧を取得"""
    response = cw.list_metrics(
        Namespace="AWS/Billing",
        MetricName="EstimatedCharges",
        Dimensions=[{"Name": "ServiceName"}],
    )
    names = set()
    for metric in response["Metrics"]:
        for dim in metric["Dimensions"]:
            if dim["Name"] == "ServiceName":
                names.add(dim["Value"])
    return list(names)


def get_service_cost(cw: Any, service_name: str, start: datetime, end: datetime) -> float:
    """サービス別コストを取得"""
    response = cw.get_metric_statistics(
        MetricName="EstimatedCharges",
        Namespace="AWS/Billing",
        Period=86400,
        StartTime=start,
        EndTime=end,
        Statistics=["Maximum"],
        Dimensions=[
            {"Name": "Currency", "Value": "USD"},
            {"Name": "ServiceName", "Value": service_name},
        ],
    )
    datapoints = response["Datapoints"]
    if not datapoints:
        return 0.0
    return max(dp["Maximum"] for dp in datapoints)


def build_discord_payload(
    total: float,
    services: list[tuple[str, float]],
    report_date: datetime,
) -> dict[str, Any]:
    """日次レポート用 Discord Embed を構築"""
    month_label = f"{report_date.year}年{report_date.month}月"

    fields = [
        {"name": service, "value": f"`${amount:.4f}`", "inline": True}
        for service, amount in services[:MAX_SERVICE_DISPLAY]
    ]

    embed: dict[str, Any] = {
        "title": f"📊 {month_label} AWS利用料（日次）",
        "description": f"**月累計: ${total:.4f} USD**",
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": f"集計基準日: {report_date.strftime('%Y-%m-%d')} (月初からの累計) ※一部サービスは非対応"},
    }

    return {"embeds": [embed]}


def post_daily_cost_to_discord() -> None:
    """AWS利用料を CloudWatch Metrics から取得して Discord に送信"""
    cw = boto3.client("cloudwatch", region_name="us-east-1")
    start, end = get_billing_period()

    total = get_total_cost(cw, start, end)

    service_names = get_service_names(cw)
    services: list[tuple[str, float]] = []
    for name in service_names:
        cost = get_service_cost(cw, name, start, end)
        if cost > 0:
            services.append((name, cost))
    services.sort(key=lambda x: x[1], reverse=True)

    payload = build_discord_payload(total, services, start)

    response = requests.post(AWS_COST_WEBHOOK_URL, json=payload, timeout=10)
    response.raise_for_status()
