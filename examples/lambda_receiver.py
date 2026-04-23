"""AWS Lambda receiver for utalkn2me webhooks.

Deploy behind API Gateway (HTTP API → Lambda proxy integration).
Writes every incoming call/transcript payload to DynamoDB.

Environment:
  EXPECTED_TOKEN   — Bearer token the pusher sends in Authorization header.
  DYNAMO_TABLE     — DynamoDB table name. Must have partition key `uuid` (S).

This is a working reference — adapt the persistence block for MySQL/Postgres
by dropping in your DB client of choice.

Deploy:
  zip lambda.zip lambda_receiver.py
  aws lambda create-function --function-name utalkn2me-ingest \\
    --runtime python3.12 --handler lambda_receiver.handler \\
    --zip-file fileb://lambda.zip \\
    --environment Variables={EXPECTED_TOKEN=...,DYNAMO_TABLE=utalkn2me}
"""
from __future__ import annotations

import json
import os
import time

import boto3

ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["DYNAMO_TABLE"])
EXPECTED = os.environ.get("EXPECTED_TOKEN", "")


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event, context):
    # API Gateway HTTP API (v2) payload shape
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if EXPECTED and auth != f"Bearer {EXPECTED}":
        return _resp(401, {"error": "unauthorized"})

    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "invalid json"})

    evt = payload.get("event")
    uuid = payload.get("uuid")
    if not uuid or not evt:
        return _resp(400, {"error": "missing uuid or event"})

    # Upsert by uuid. DynamoDB PutItem is naturally idempotent on PK.
    item = {
        "uuid": uuid,
        "event": evt,
        "schema_version": payload.get("schema_version", 1),
        "received_at": int(time.time()),
        **{k: v for k, v in payload.items() if k not in {"uuid", "event"}},
    }
    TABLE.put_item(Item=item)
    return _resp(200, {"uuid": uuid, "event": evt, "ok": True})
