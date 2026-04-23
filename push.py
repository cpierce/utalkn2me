"""Outbound webhook pusher.

Reads unpushed rows from the SQLite outbox and POSTs them to PUSH_URL.
Runs as its own container service, or via `python push.py` directly.

Env:
  PUSH_URL        — required. https://… endpoint that accepts the payload.
  PUSH_TOKEN      — sent as `Authorization: Bearer …`. Optional.
  PUSH_SITE_ID    — tenant/site identifier included in every payload.
  PUSH_BATCH      — rows per request (default 10)
  PUSH_INTERVAL   — seconds between batches (default 5)
  PUSH_TIMEOUT    — HTTP timeout seconds (default 30)
  SQLITE_PATH     — default /data/calls.db
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

import requests

import db

SCHEMA_VERSION = 1
USER_AGENT = f"utalkn2me/{SCHEMA_VERSION} ({socket.gethostname()})"


def _payload_for_call(row) -> dict:
    raw = json.loads(row["raw"])
    body = {
        "schema_version": SCHEMA_VERSION,
        "event": "call.upserted",
        "site_id": os.environ.get("PUSH_SITE_ID", ""),
        "uuid": row["uuid"],
        "time": row["time"],
        "direction": row["direction"],
        "from": row["from_number"],
        "to": row["to_number"],
        "from_caller_name": row["from_caller_name"],
        "answered_by": row["answered_by"],
        "status": row["status"],
        "duration": row["duration"],
        "is_video_call": bool(row["is_video_call"]),
        "recording_available": bool(row["recording"]),
        "recording_filename": row["recording_filename"],
        "quality_score": row["quality_score"],
        "to_smart_attendant_title": row["to_smart_attendant_title"],
        "first_seen": row["first_seen"],
        "transcript": None,
        "raw": raw,
    }
    if row["transcript_text"]:
        body["transcript"] = {
            "text": row["transcript_text"],
            "source": row["transcript_source"],
            "model": row["transcript_model"],
            "created_at": row["transcript_created_at"],
        }
    return body


def _payload_for_transcript(row) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "transcript.upserted",
        "site_id": os.environ.get("PUSH_SITE_ID", ""),
        "uuid": row["uuid"],
        "call_time": row["call_time"],
        "transcript": {
            "text": row["text"],
            "source": row["source"],
            "model": row["model"],
            "created_at": row["created_at"],
        },
    }


def _post(url: str, token: str | None, body: dict, timeout: int) -> tuple[bool, str]:
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        return False, f"network: {e}"
    if 200 <= r.status_code < 300:
        return True, ""
    # Retry on 5xx + 429; mark error and skip on 4xx (bad data).
    return False, f"{r.status_code}: {r.text[:200]}"


def run_once(conn, *, url: str, token: str | None, batch: int, timeout: int) -> int:
    """Push one batch each of calls and transcripts. Returns rows acked."""
    acked = 0

    calls = db.unpushed_calls(conn, batch)
    ok_uuids = []
    for row in calls:
        ok, err = _post(url, token, _payload_for_call(row), timeout)
        if ok:
            ok_uuids.append(row["uuid"])
        else:
            is_client = err[:3].isdigit() and 400 <= int(err[:3]) < 500 and err[:3] != "429"
            db.mark_push_error(conn, "calls", row["uuid"], err)
            print(f"[push] calls  {row['uuid']}  FAIL ({err})", flush=True)
            if is_client:
                # 4xx — permanent. Mark pushed so we stop retrying.
                db.mark_call_pushed(conn, [row["uuid"]])
    db.mark_call_pushed(conn, ok_uuids)
    acked += len(ok_uuids)
    if ok_uuids:
        print(f"[push] calls  acked {len(ok_uuids)}", flush=True)

    txs = db.unpushed_transcripts(conn, batch)
    ok_uuids = []
    for row in txs:
        ok, err = _post(url, token, _payload_for_transcript(row), timeout)
        if ok:
            ok_uuids.append(row["uuid"])
        else:
            is_client = err[:3].isdigit() and 400 <= int(err[:3]) < 500 and err[:3] != "429"
            db.mark_push_error(conn, "transcripts", row["uuid"], err)
            print(f"[push] trans  {row['uuid']}  FAIL ({err})", flush=True)
            if is_client:
                db.mark_transcript_pushed(conn, [row["uuid"]])
    db.mark_transcript_pushed(conn, ok_uuids)
    acked += len(ok_uuids)
    if ok_uuids:
        print(f"[push] trans  acked {len(ok_uuids)}", flush=True)

    return acked


def main() -> int:
    url = os.environ.get("PUSH_URL")
    token = os.environ.get("PUSH_TOKEN")
    batch = int(os.environ.get("PUSH_BATCH", "10"))
    interval = int(os.environ.get("PUSH_INTERVAL", "5"))
    timeout = int(os.environ.get("PUSH_TIMEOUT", "30"))
    db_path = os.environ.get("SQLITE_PATH", "/data/calls.db")

    if not url:
        print("[push] PUSH_URL not set — pusher idling. "
              "Set it to enable webhook delivery.", flush=True)
        while True:
            time.sleep(60)

    print(f"[push] pushing to {url}  batch={batch}  interval={interval}s", flush=True)
    conn = db.connect(db_path)
    while True:
        try:
            run_once(conn, url=url, token=token, batch=batch, timeout=timeout)
        except Exception as e:
            print(f"[push] loop error: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main() or 0)
