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
    first_seen              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    pushed_at               TEXT,
    push_error              TEXT
);

CREATE INDEX IF NOT EXISTS calls_time_idx ON calls(time DESC);

CREATE TABLE IF NOT EXISTS transcripts (
    uuid        TEXT PRIMARY KEY REFERENCES calls(uuid),
    text        TEXT NOT NULL,
    source      TEXT NOT NULL,     -- 'native' | 'whisper' | 'faster-whisper'
    model       TEXT,              -- whisper model name, NULL for native
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    pushed_at   TEXT,
    push_error  TEXT
);
"""

# Idempotent migrations for DBs created before these columns existed.
MIGRATIONS = [
    "ALTER TABLE calls       ADD COLUMN pushed_at  TEXT",
    "ALTER TABLE calls       ADD COLUMN push_error TEXT",
    "ALTER TABLE transcripts ADD COLUMN pushed_at  TEXT",
    "ALTER TABLE transcripts ADD COLUMN push_error TEXT",
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
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
        "unpushed_calls": conn.execute(
            "SELECT COUNT(*) FROM calls WHERE pushed_at IS NULL"
        ).fetchone()[0],
        "unpushed_transcripts": conn.execute(
            "SELECT COUNT(*) FROM transcripts WHERE pushed_at IS NULL"
        ).fetchone()[0],
    }


def unpushed_calls(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT c.*, t.text AS transcript_text, t.source AS transcript_source,
                  t.model AS transcript_model, t.created_at AS transcript_created_at
             FROM calls c LEFT JOIN transcripts t USING (uuid)
            WHERE c.pushed_at IS NULL
         ORDER BY c.time ASC
            LIMIT ?""",
        (limit,),
    ))


def unpushed_transcripts(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Transcripts that arrived after their call was already pushed — push updates."""
    return list(conn.execute(
        """SELECT t.*, c.time AS call_time
             FROM transcripts t JOIN calls c USING (uuid)
            WHERE t.pushed_at IS NULL AND c.pushed_at IS NOT NULL
         ORDER BY c.time ASC
            LIMIT ?""",
        (limit,),
    ))


def mark_call_pushed(conn: sqlite3.Connection, uuids: list[str]) -> None:
    if not uuids: return
    with tx(conn):
        conn.executemany(
            "UPDATE calls SET pushed_at = CURRENT_TIMESTAMP, push_error = NULL WHERE uuid = ?",
            [(u,) for u in uuids],
        )


def mark_transcript_pushed(conn: sqlite3.Connection, uuids: list[str]) -> None:
    if not uuids: return
    with tx(conn):
        conn.executemany(
            "UPDATE transcripts SET pushed_at = CURRENT_TIMESTAMP, push_error = NULL WHERE uuid = ?",
            [(u,) for u in uuids],
        )


def mark_push_error(conn: sqlite3.Connection, table: str, uuid: str, err: str) -> None:
    assert table in ("calls", "transcripts")
    with tx(conn):
        conn.execute(f"UPDATE {table} SET push_error = ? WHERE uuid = ?", (err[:500], uuid))


def recordings_to_prune(conn: sqlite3.Connection, older_than_days: int) -> list[str]:
    """Return UUIDs whose calls are older than N days — MP3s for these can be deleted."""
    return [r[0] for r in conn.execute(
        """SELECT uuid FROM calls
            WHERE recording = 1
              AND time < datetime('now', ?)""",
        (f"-{older_than_days} days",),
    )]
