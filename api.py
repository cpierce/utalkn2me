"""Read-only HTTP API over the calls SQLite DB.

Run locally:   python api.py
Run in prod:   gunicorn -b 0.0.0.0:8000 api:app
"""
from __future__ import annotations

import json
import os
import sqlite3

from flask import Flask, Response, abort, jsonify, request, send_file

import db

DB_PATH = os.environ.get("SQLITE_PATH", "data/calls.db")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "data/recordings")

app = Flask(__name__)


def _conn() -> sqlite3.Connection:
    return db.connect(DB_PATH)


def _row_to_call(row: sqlite3.Row, *, include_raw: bool = False) -> dict:
    d = {
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
        "recording": bool(row["recording"]),
        "recording_filename": row["recording_filename"],
        "quality_score": row["quality_score"],
        "to_smart_attendant_title": row["to_smart_attendant_title"],
        "first_seen": row["first_seen"],
    }
    if include_raw:
        d["raw"] = json.loads(row["raw"])
    return d


@app.get("/health")
def health():
    conn = _conn()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return jsonify({"status": "ok"})


@app.get("/stats")
def stats():
    conn = _conn()
    try:
        return jsonify(db.stats(conn))
    finally:
        conn.close()


@app.get("/calls")
def list_calls():
    """Filters: ?limit=&offset=&direction=&status=&from_date=&to_date=&has_transcript=true|false&q="""
    limit = min(int(request.args.get("limit", 50)), 500)
    offset = int(request.args.get("offset", 0))

    where, params = [], []
    for col, arg in (
        ("direction", "direction"),
        ("status", "status"),
    ):
        v = request.args.get(arg)
        if v:
            where.append(f"c.{col} = ?")
            params.append(v)
    if v := request.args.get("from_date"):
        where.append("c.time >= ?"); params.append(v)
    if v := request.args.get("to_date"):
        where.append("c.time <= ?"); params.append(v)
    ht = request.args.get("has_transcript")
    if ht == "true":
        where.append("t.uuid IS NOT NULL")
    elif ht == "false":
        where.append("t.uuid IS NULL")
    if q := request.args.get("q"):
        where.append("(c.from_number LIKE ? OR c.to_number LIKE ? OR "
                     "c.from_caller_name LIKE ? OR t.text LIKE ?)")
        needle = f"%{q}%"
        params.extend([needle, needle, needle, needle])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = _conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM calls c LEFT JOIN transcripts t USING(uuid) {where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT c.*, t.text AS transcript
                  FROM calls c LEFT JOIN transcripts t USING(uuid)
                  {where_sql}
              ORDER BY c.time DESC
                 LIMIT ? OFFSET ?""",
            [*params, limit, offset],
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {**_row_to_call(r), "transcript": r["transcript"]}
            for r in rows
        ],
    })


@app.get("/calls/<uuid>")
def get_call(uuid: str):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM calls WHERE uuid = ?", (uuid,)).fetchone()
        if not row:
            abort(404, description="call not found")
        t = conn.execute(
            "SELECT text, source, model, created_at FROM transcripts WHERE uuid = ?",
            (uuid,),
        ).fetchone()
    finally:
        conn.close()
    body = _row_to_call(row, include_raw=True)
    body["transcript"] = dict(t) if t else None
    return jsonify(body)


@app.get("/calls/<uuid>/transcript")
def get_transcript(uuid: str):
    conn = _conn()
    try:
        t = conn.execute(
            "SELECT text, source, model, created_at FROM transcripts WHERE uuid = ?",
            (uuid,),
        ).fetchone()
    finally:
        conn.close()
    if not t:
        abort(404, description="no transcript for this call")
    if request.args.get("format") == "text":
        return Response(t["text"], mimetype="text/plain")
    return jsonify(dict(t))


@app.get("/calls/<uuid>/recording")
def get_recording(uuid: str):
    path = os.path.join(RECORDINGS_DIR, f"{uuid}.mp3")
    if not os.path.exists(path):
        abort(404, description="recording not cached locally")
    return send_file(path, mimetype="audio/mpeg", as_attachment=False)


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": str(e.description)}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
