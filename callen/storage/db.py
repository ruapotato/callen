# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""SQLite database for call history, transcripts, and notes."""

import logging
import sqlite3
import threading

from callen.storage.models import CallRecord, TranscriptSegment, Note

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    caller_id TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'inbound',
    state TEXT NOT NULL DEFAULT 'completed',
    started_at REAL NOT NULL,
    answered_at REAL,
    ended_at REAL,
    duration_seconds REAL DEFAULT 0,
    was_bridged INTEGER DEFAULT 0,
    consented INTEGER DEFAULT 0,
    caller_recording_path TEXT,
    tech_recording_path TEXT,
    voicemail_path TEXT
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(id),
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    timestamp_offset REAL NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE INDEX IF NOT EXISTS idx_transcript_call
    ON transcript_segments(call_id, timestamp_offset);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL REFERENCES calls(id),
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE INDEX IF NOT EXISTS idx_notes_call ON notes(call_id);

CREATE TABLE IF NOT EXISTS operator_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'available',
    updated_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

INSERT OR IGNORE INTO operator_state (id, status) VALUES (1, 'available');

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO schema_version VALUES (1);
"""


class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._path)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
        return self._local.conn

    def initialize(self):
        conn = self._conn()
        conn.executescript(SCHEMA)
        conn.commit()
        log.info("Database initialized: %s", self._path)

    def save_call(self, record: CallRecord):
        self._conn().execute(
            """INSERT OR REPLACE INTO calls
               (id, caller_id, direction, state, started_at, answered_at,
                ended_at, duration_seconds, was_bridged, consented,
                caller_recording_path, tech_recording_path, voicemail_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.id, record.caller_id, record.direction, record.state,
                record.started_at, record.answered_at, record.ended_at,
                record.duration_seconds, int(record.was_bridged),
                int(record.consented),
                record.caller_recording_path, record.tech_recording_path,
                record.voicemail_path,
            ),
        )
        self._conn().commit()

    def save_transcript_segment(self, call_id: str, speaker: str,
                                 text: str, timestamp_offset: float):
        self._conn().execute(
            """INSERT INTO transcript_segments (call_id, speaker, text, timestamp_offset)
               VALUES (?, ?, ?, ?)""",
            (call_id, speaker, text, timestamp_offset),
        )
        self._conn().commit()

    def save_note(self, call_id: str, text: str, author: str = "operator"):
        self._conn().execute(
            """INSERT INTO notes (call_id, author, text) VALUES (?, ?, ?)""",
            (call_id, author, text),
        )
        self._conn().commit()

    def get_call(self, call_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM calls WHERE id = ?", (call_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_call_history(self, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM calls ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_transcript(self, call_id: str) -> list[dict]:
        rows = self._conn().execute(
            """SELECT * FROM transcript_segments
               WHERE call_id = ? ORDER BY timestamp_offset""",
            (call_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_notes(self, call_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM notes WHERE call_id = ? ORDER BY created_at",
            (call_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_operator_status(self) -> str:
        row = self._conn().execute(
            "SELECT status FROM operator_state WHERE id = 1"
        ).fetchone()
        return row["status"] if row else "available"

    def set_operator_status(self, status: str):
        self._conn().execute(
            "UPDATE operator_state SET status = ?, updated_at = unixepoch('subsec') WHERE id = 1",
            (status,),
        )
        self._conn().commit()
