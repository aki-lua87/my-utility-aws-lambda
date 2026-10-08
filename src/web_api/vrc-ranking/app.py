"""Generic, sessionless VRChat record rankings. No payloads or keys are logged."""

import hashlib
import json
import logging
import os
import re
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError
from protocol import IDENTIFIER, WORLD, decode

logger = logging.getLogger(__name__)
serializer = TypeSerializer()
deserializer = TypeDeserializer()
_configuration = None
_configuration_at = 0
_ddb = None


class RequestError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def configuration():
    global _configuration, _configuration_at
    if _configuration is None or time.time() - _configuration_at > 60:
        parameter = os.environ["CONFIG_PARAMETER"]
        raw = boto3.client("ssm").get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
        value = json.loads(raw)
        if not isinstance(value.get("worlds"), dict):
            raise RuntimeError("Invalid ranking configuration")
        for world_id, world in value["worlds"].items():
            if not WORLD.fullmatch(world_id) or not world.get("boards") or not world.get("keys"):
                raise RuntimeError("Invalid ranking world configuration")
            for key_id, key_hex in world["keys"].items():
                if not IDENTIFIER.fullmatch(key_id) or len(bytes.fromhex(key_hex)) != 32:
                    raise RuntimeError("Invalid ranking key configuration")
            for board_id, board in world["boards"].items():
                if not IDENTIFIER.fullmatch(board_id) or board.get("order", "asc") not in ("asc", "desc"):
                    raise RuntimeError("Invalid ranking board configuration")
        _configuration, _configuration_at = value["worlds"], time.time()
    return _configuration


def db():
    global _ddb
    if _ddb is None:
        _ddb = boto3.client("dynamodb")
    return _ddb


def pack(item):
    return {k: serializer.serialize(v) for k, v in item.items()}


def unpack(item):
    return {k: deserializer.deserialize(v) for k, v in item.items()}


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        "body": json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=int),
    }


def board_partition(world_id, board_id):
    return f"WORLD#{world_id}#BOARD#{board_id}"


def records(world_id, world, board_id=None, limit=10):
    if board_id is not None and board_id not in world["boards"]:
        raise RequestError("unknown_board")
    boards = []
    for name, settings in world["boards"].items():
        if board_id is not None and name != board_id:
            continue
        result = db().query(
            TableName=os.environ["TABLE_NAME"],
            KeyConditionExpression="p_key = :pk AND begins_with(s_key, :prefix)",
            ExpressionAttributeValues=pack({":pk": board_partition(world_id, name), ":prefix": "S#"}),
            ScanIndexForward=settings.get("order", "asc") == "asc",
            ConsistentRead=True,
            Limit=limit,
        )
        rows = []
        for item in result.get("Items", []):
            row = unpack(item)
            rows.append({k: row[k] for k in ("record_id", "display_name", "score", "participant_count", "completed_at", "rules_version")})
        boards.append({"board_id": name, "order": settings.get("order", "asc"), "records": rows})
    return {"world_id": world_id, "boards": boards}


def validate_record(record, world):
    required = {"version", "world_id", "board_id", "record_id", "display_name", "score", "participant_count", "completed_at", "rules_version"}
    if set(record) != required or type(record["version"]) is not int or record["version"] != 1:
        raise RequestError("invalid_record")
    if not isinstance(record["board_id"], str) or record["board_id"] not in world["boards"]:
        raise RequestError("unknown_board")
    if not isinstance(record["record_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", record["record_id"]):
        raise RequestError("invalid_record")
    name = record["display_name"]
    if not isinstance(name, str) or not 1 <= len(name) <= 128 or any(ord(c) < 32 for c in name):
        raise RequestError("invalid_record")
    board = world["boards"][record["board_id"]]
    for field, low, high in (
        ("score", board.get("min_score", 0), board.get("max_score", 2147483647)),
        ("participant_count", board.get("min_participants", 1), board.get("max_participants", 80)),
        ("completed_at", 0, 4102444800),
        ("rules_version", 1, 2147483647),
    ):
        if type(record[field]) is not int or not low <= record[field] <= high:
            raise RequestError("invalid_record")
    if record["rules_version"] != board.get("rules_version", 1):
        raise RequestError("rules_version_mismatch")


def submit(payload, worlds):
    world_id, record, world = decode(payload, worlds)
    validate_record(record, world)
    if record["world_id"] != world_id:
        raise RequestError("invalid_record")
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    pk = board_partition(world_id, record["board_id"])
    guard_key = {"p_key": pk, "s_key": "ID#" + record["record_id"]}

    def existing():
        result = db().get_item(TableName=os.environ["TABLE_NAME"], Key=pack(guard_key), ConsistentRead=True)
        return unpack(result["Item"]) if "Item" in result else None

    def check_duplicate(item):
        if item["digest"] != digest:
            raise RequestError("record_id_conflict", 409)

    previous = existing()
    if previous is not None:
        check_duplicate(previous)
        status = "duplicate"
    else:
        now = int(time.time())
        if record["completed_at"] > now + 300 or now - record["completed_at"] > world.get("max_age_seconds", 604800):
            raise RequestError("record_expired")
        item = {**record, "p_key": pk, "s_key": f"S#{record['score']:020d}#{record['record_id']}"}
        guard = {**guard_key, "digest": digest}
        try:
            db().transact_write_items(
                TransactItems=[
                    {"Put": {"TableName": os.environ["TABLE_NAME"], "Item": pack(guard), "ConditionExpression": "attribute_not_exists(p_key)"}},
                    {"Put": {"TableName": os.environ["TABLE_NAME"], "Item": pack(item), "ConditionExpression": "attribute_not_exists(p_key)"}},
                ]
            )
            status = "saved"
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            previous = existing()
            if previous is None:
                raise
            check_duplicate(previous)
            status = "duplicate"
    return {"status": status, **records(world_id, world)}


def lambda_handler(event, context):
    try:
        method = event.get("httpMethod", event.get("requestContext", {}).get("http", {}).get("method"))
        if method != "GET":
            raise RequestError("method_not_allowed", 405)
        path = event.get("resource", event.get("rawPath", event.get("path", "")))
        query = event.get("queryStringParameters") or {}
        worlds = configuration()
        if path == "/vrc/ranking/submit":
            return response(200, submit(query.get("payload"), worlds))
        if path != "/vrc/ranking/records":
            raise RequestError("not_found", 404)
        world_id = query.get("world_id")
        if world_id not in worlds:
            raise RequestError("unknown_world")
        limit = int(query.get("limit", "10"))
        if not 1 <= limit <= 50:
            raise RequestError("invalid_limit")
        return response(200, {"status": "ok", **records(world_id, worlds[world_id], query.get("board_id"), limit)})
    except RequestError as error:
        return response(error.status, {"status": "error", "error": error.code})
    except (ValueError, UnicodeError, TypeError):
        return response(400, {"status": "error", "error": "invalid_request"})
    except Exception:
        # Do not log exceptions containing SSM configuration or encrypted query strings.
        logger.error("VRChat ranking request failed")
        return response(503, {"status": "error", "error": "temporarily_unavailable"})
