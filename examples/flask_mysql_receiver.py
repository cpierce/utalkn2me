"""Flask receiver → MySQL.

Accepts utalkn2me webhooks, upserts into a `calls` table keyed on uuid.
Keep a JSON column for the raw payload so you can cherry-pick later.

pip install flask pymysql
python flask_mysql_receiver.py

Env:
  EXPECTED_TOKEN  — bearer token the pusher must send
  MYSQL_URL       — e.g. mysql+pymysql://user:pw@host/dbname
                    (or set MYSQL_HOST/USER/PASS/DB individually)

MySQL schema:

    CREATE TABLE calls (
        uuid              CHAR(36) PRIMARY KEY,
        call_time         DATETIME,
        direction         VARCHAR(8),
        from_number       VARCHAR(32),
        to_number         VARCHAR(32),
        from_caller_name  VARCHAR(128),
        duration          INT,
        status            VARCHAR(32),
        transcript_text   MEDIUMTEXT,
        raw               JSON,
        received_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
        INDEX (call_time)
    );
"""
from __future__ import annotations

import json
import os

import pymysql
from flask import Flask, jsonify, request

app = Flask(__name__)
EXPECTED = os.environ.get("EXPECTED_TOKEN", "")

DB = dict(
    host=os.environ.get("MYSQL_HOST", "localhost"),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASS"],
    database=os.environ["MYSQL_DB"],
    autocommit=True,
    cursorclass=pymysql.cursors.DictCursor,
)


def _upsert_call(p: dict) -> None:
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO calls (uuid, call_time, direction, from_number,
                                   to_number, from_caller_name, duration,
                                   status, transcript_text, raw)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    call_time = VALUES(call_time),
                    direction = VALUES(direction),
                    from_number = VALUES(from_number),
                    to_number = VALUES(to_number),
                    from_caller_name = VALUES(from_caller_name),
                    duration = VALUES(duration),
                    status = VALUES(status),
                    transcript_text = COALESCE(VALUES(transcript_text), transcript_text),
                    raw = VALUES(raw)
                """,
                (
                    p["uuid"],
                    p.get("time"),
                    p.get("direction"),
                    p.get("from"),
                    p.get("to"),
                    p.get("from_caller_name"),
                    p.get("duration"),
                    p.get("status"),
                    (p.get("transcript") or {}).get("text"),
                    json.dumps(p),
                ),
            )
    finally:
        conn.close()


def _upsert_transcript(p: dict) -> None:
    conn = pymysql.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE calls SET transcript_text = %s WHERE uuid = %s",
                ((p.get("transcript") or {}).get("text"), p["uuid"]),
            )
    finally:
        conn.close()


@app.post("/ingest/utalkn2me")
def ingest():
    auth = request.headers.get("Authorization", "")
    if EXPECTED and auth != f"Bearer {EXPECTED}":
        return jsonify(error="unauthorized"), 401
    payload = request.get_json(silent=True) or {}
    evt = payload.get("event")
    if evt == "call.upserted":
        _upsert_call(payload)
    elif evt == "transcript.upserted":
        _upsert_transcript(payload)
    else:
        return jsonify(error=f"unknown event {evt}"), 400
    return jsonify(ok=True, uuid=payload.get("uuid"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 9000)))
