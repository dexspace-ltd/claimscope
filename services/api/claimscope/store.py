"""DynamoDB single-table access. Key layout is in docs/ARCHITECTURE.md section 8."""

import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

_table = None


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def asmt_pk(assessment_id: str) -> str:
    return f"ASMT#{assessment_id}"


def put(item: dict) -> None:
    table().put_item(Item=item)


def get_meta(assessment_id: str) -> dict | None:
    return table().get_item(Key={"PK": asmt_pk(assessment_id), "SK": "META"}).get("Item")


def query_assessment(assessment_id: str) -> list[dict]:
    items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(asmt_pk(assessment_id))}
    while True:
        page = table().query(**kwargs)
        items.extend(page["Items"])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def set_status(
    assessment_id: str, status: str, expect: tuple[str, ...] = (), error: str | None = None, progress: str | None = None
) -> bool:
    """Move an assessment to `status`. With `expect`, only succeeds from one of those statuses."""
    # Aliases throughout: "status" and "error" are DynamoDB reserved words.
    names = {"#s": "status"}
    values = {":s": status, ":u": now()}
    update = "SET #s = :s, updated_at = :u"
    if error is not None:
        update += ", #e = :e"
        names["#e"] = "error"
        values[":e"] = error
    if progress is not None:
        update += ", #p = :p"
        names["#p"] = "progress"
        values[":p"] = progress
    condition = "attribute_exists(PK)"
    if expect:
        placeholders = []
        for i, s in enumerate(expect):
            values[f":x{i}"] = s
            placeholders.append(f":x{i}")
        condition += f" AND #s IN ({', '.join(placeholders)})"
    try:
        table().update_item(
            Key={"PK": asmt_pk(assessment_id), "SK": "META"},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except table().meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True
