"""SQLite storage for UniFi Talk calls and transcripts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    uuid                    TEXT PRIMARY KEY,
    time                    TEXT NOT NULL,
    direction               TEXT,
    from_number             TEXT,
    to_number               TEXT,
    from_caller_name        TEXT,
    answered_by             TEXT,
    status                  TEXT,
    duration                INTEGER,
    is_video_call           INTEGER,
    recording               INTEGER,
    recording_filename      TEXT,
    quality_score           INTEGER,
    to_smart_attendant_title TEXT,
    raw                     TEXT NOT NULL,
    first_seen              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS calls_time_idx ON calls(time DESC);

CREATE TABLE IF NOT EXISTS transcripts (
    uuid        TEXT PRIMARY KEY REFERENCES calls(uuid),
    text        TEXT NOT NULL,
    source      TEXT NOT NULL,     -- 'native' | 'whisper' | 'faster-whisper'
    model       TEXT,              -- whisper model name, NULL for native
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_calls(conn: sqlite3.Connection, records: Iterable[dict]) -> int:
    """Insert new call records. Returns count of newly-inserted rows."""
    inserted = 0
    with tx(conn):
        for r in records:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO calls (
                    uuid, time, direction, from_number, to_number,
                    from_caller_name, answered_by, status, duration,
                    is_video_call, recording, recording_filename,
                    quality_score, to_smart_attendant_title, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r.get("uuid"),
                    r.get("time"),
                    r.get("direction"),
                    r.get("from"),
                    r.get("to"),
                    r.get("from_caller_name"),
                    r.get("answered_by"),
                    r.get("status"),
                    r.get("duration"),
                    1 if r.get("is_video_call") else 0,
                    1 if r.get("recording") else 0,
                    r.get("recording_filename"),
                    r.get("quality_score"),
                    r.get("to_smart_attendant_title"),
                    json.dumps(r, default=str),
                ),
            )
            inserted += cur.rowcount
    return inserted


def calls_needing_transcript(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Calls with a recording but no transcript yet."""
    return list(
        conn.execute(
            """
            SELECT c.*
              FROM calls c
         LEFT JOIN transcripts t ON t.uuid = c.uuid
             WHERE c.recording = 1
               AND t.uuid IS NULL
          ORDER BY c.time DESC
            """
        )
    )


def save_transcript(
    conn: sqlite3.Connection,
    uuid: str,
    text: str,
    source: str,
    model: str | None = None,
) -> None:
    with tx(conn):
        conn.execute(
            """
            INSERT OR REPLACE INTO transcripts (uuid, text, source, model)
            VALUES (?, ?, ?, ?)
            """,
            (uuid, text, source, model),
        )


def stats(conn: sqlite3.Connection) -> dict:
    return {
        "calls": conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
        "with_recording": conn.execute(
            "SELECT COUNT(*) FROM calls WHERE recording = 1"
        ).fetchone()[0],
        "transcripts": conn.execute(
            "SELECT COUNT(*) FROM transcripts"
        ).fetchone()[0],
        "pending": len(calls_needing_transcript(conn)),
    }
