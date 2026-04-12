# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""SQLite database — contacts, incidents, calls, transcripts, emails, notes."""

import json
import logging
import sqlite3
import threading
import time

from callen.storage.models import (
    Contact, ContactPhone, ContactEmail,
    Incident, IncidentEntry,
    CallRecord, TranscriptSegment, Note, EmailMessage,
)

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 4

# --- Schema ---

SCHEMA_V1 = """
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
    call_id TEXT NOT NULL,
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
"""

SCHEMA_V2_NEW = """
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE TABLE IF NOT EXISTS contact_phones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    e164 TEXT NOT NULL UNIQUE,
    consented_at REAL,
    consent_source TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE INDEX IF NOT EXISTS idx_contact_phones_contact ON contact_phones(contact_id);

CREATE TABLE IF NOT EXISTS contact_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    address TEXT NOT NULL UNIQUE,
    consented_at REAL,
    consent_source TEXT,
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE INDEX IF NOT EXISTS idx_contact_emails_contact ON contact_emails(contact_id);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    contact_id TEXT REFERENCES contacts(id),
    subject TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT NOT NULL DEFAULT 'normal',
    labels TEXT DEFAULT '[]',
    channel TEXT DEFAULT 'phone',
    assigned_to TEXT DEFAULT '',
    created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
    updated_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
);

CREATE INDEX IF NOT EXISTS idx_incidents_contact ON incidents(contact_id);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_updated ON incidents(updated_at DESC);

CREATE TABLE IF NOT EXISTS incident_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    type TEXT NOT NULL,
    occurred_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
    author TEXT DEFAULT '',
    linked_call_id TEXT,
    linked_email_id INTEGER,
    payload TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_incident_entries_incident
    ON incident_entries(incident_id, occurred_at);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    incident_id TEXT REFERENCES incidents(id),
    direction TEXT NOT NULL DEFAULT 'in',
    from_addr TEXT DEFAULT '',
    to_addr TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    body_text TEXT DEFAULT '',
    body_html TEXT DEFAULT '',
    received_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
    in_reply_to TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    status_reason TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status);

CREATE INDEX IF NOT EXISTS idx_emails_incident ON emails(incident_id);

CREATE TABLE IF NOT EXISTS id_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO id_counters (name, value) VALUES ('contact', 0);
INSERT OR IGNORE INTO id_counters (name, value) VALUES ('incident', 0);
"""


def normalize_phone(raw: str) -> str:
    """Normalize a phone number to a bare-digits canonical form.

    - Strips all non-digit characters
    - If the result is 10 digits, assumes US/CA and prepends '1'
    - Otherwise returns the digits as-is

    Returns empty string if no digits found.
    """
    if not raw:
        return ""
    digits = "".join(c for c in str(raw) if c.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        digits = "1" + digits
    return digits


class Database:
    def __init__(self, db_path: str):
        self._path = db_path
        self._local = threading.local()
        self._id_lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._path, timeout=10.0)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    # --- Schema lifecycle ---

    def initialize(self):
        conn = self._conn()
        conn.executescript(SCHEMA_V1)
        conn.commit()

        version = self._current_version()
        if version < 1:
            conn.execute("INSERT OR IGNORE INTO schema_version VALUES (1)")
            conn.commit()
            version = 1

        if version < 2:
            self._migrate_to_v2()

        if self._current_version() < 3:
            self._migrate_to_v3()

        if self._current_version() < 4:
            self._migrate_to_v4()

        log.info("Database ready (schema v%d): %s", self._current_version(), self._path)

    def _current_version(self) -> int:
        row = self._conn().execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        if row is None or row[0] is None:
            return 0
        return row[0]

    def _migrate_to_v2(self):
        """Add contacts/incidents/emails tables and migrate existing calls."""
        log.info("Migrating database schema v1 -> v2 (contacts + incidents)")
        conn = self._conn()
        conn.executescript(SCHEMA_V2_NEW)

        # Add incident_id to calls (if not present)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(calls)").fetchall()}
        if "incident_id" not in cols:
            conn.execute("ALTER TABLE calls ADD COLUMN incident_id TEXT")

        conn.commit()

        # Migrate existing calls: one contact per unique caller_id, one incident per call
        existing = conn.execute(
            "SELECT id, caller_id, consented, started_at FROM calls WHERE incident_id IS NULL"
        ).fetchall()

        for row in existing:
            raw_caller = row["caller_id"] or ""
            e164 = normalize_phone(raw_caller)
            if not e164:
                e164 = raw_caller or "unknown"

            contact_id = self._upsert_contact_by_phone(
                e164,
                display_name="",
                consent=bool(row["consented"]),
                consent_source="ivr",
                now=row["started_at"],
                commit=False,
            )
            incident_id = self._new_incident_id_unlocked()
            conn.execute(
                """INSERT INTO incidents
                   (id, contact_id, subject, status, priority, labels,
                    channel, created_at, updated_at)
                   VALUES (?, ?, ?, 'closed', 'normal', '[]', 'phone', ?, ?)""",
                (incident_id, contact_id,
                 f"Call from {e164}", row["started_at"], row["started_at"]),
            )
            conn.execute(
                "UPDATE calls SET incident_id = ? WHERE id = ?",
                (incident_id, row["id"]),
            )
            conn.execute(
                """INSERT INTO incident_entries
                   (incident_id, type, occurred_at, linked_call_id, payload)
                   VALUES (?, 'call', ?, ?, ?)""",
                (incident_id, row["started_at"], row["id"], "{}"),
            )

        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (2)")
        conn.commit()
        log.info("Migration v1 -> v2 complete (%d calls migrated)", len(existing))

    def _migrate_to_v4(self):
        """Add incident_todos table — structured checklist per incident."""
        log.info("Migrating database schema v3 -> v4 (incident todos)")
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS incident_todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL REFERENCES incidents(id),
                text TEXT NOT NULL,
                done INTEGER NOT NULL DEFAULT 0,
                author TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                completed_at REAL,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_incident_todos_incident
                ON incident_todos(incident_id, position);
            INSERT OR IGNORE INTO schema_version VALUES (4);
        """)
        conn.commit()
        log.info("Migration v3 -> v4 complete")

    def _migrate_to_v3(self):
        """Add email status + status_reason columns for the triage workflow."""
        log.info("Migrating database schema v2 -> v3 (email status tracking)")
        conn = self._conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)").fetchall()}
        if "status" not in cols:
            conn.execute("ALTER TABLE emails ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        if "status_reason" not in cols:
            conn.execute("ALTER TABLE emails ADD COLUMN status_reason TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_status ON emails(status)")
        # Backfill: any existing email with an incident is 'attached'
        conn.execute(
            "UPDATE emails SET status = 'attached' WHERE incident_id IS NOT NULL"
        )
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (3)")
        conn.commit()
        log.info("Migration v2 -> v3 complete")

    # --- ID generation ---

    def _next_counter(self, name: str) -> int:
        with self._id_lock:
            conn = self._conn()
            conn.execute(
                "UPDATE id_counters SET value = value + 1 WHERE name = ?", (name,)
            )
            row = conn.execute(
                "SELECT value FROM id_counters WHERE name = ?", (name,)
            ).fetchone()
            conn.commit()
            return int(row[0])

    def _next_counter_unlocked(self, name: str) -> int:
        """Same as _next_counter but without acquiring the id_lock.
        Use only during migrations where the lock is not held by callers."""
        conn = self._conn()
        conn.execute(
            "UPDATE id_counters SET value = value + 1 WHERE name = ?", (name,)
        )
        row = conn.execute(
            "SELECT value FROM id_counters WHERE name = ?", (name,)
        ).fetchone()
        return int(row[0])

    def new_contact_id(self) -> str:
        return f"CON-{self._next_counter('contact'):04d}"

    def new_incident_id(self) -> str:
        return f"INC-{self._next_counter('incident'):04d}"

    def _new_incident_id_unlocked(self) -> str:
        return f"INC-{self._next_counter_unlocked('incident'):04d}"

    def _new_contact_id_unlocked(self) -> str:
        return f"CON-{self._next_counter_unlocked('contact'):04d}"

    # --- Contacts ---

    def _upsert_contact_by_phone(
        self, e164: str, display_name: str = "",
        consent: bool = False, consent_source: str = "",
        now: float | None = None, commit: bool = True,
    ) -> str:
        """Find-or-create a contact by phone number. Returns contact_id.

        If the phone already exists, returns its contact_id and optionally
        records consent. If not, creates a new contact with that phone.
        """
        conn = self._conn()
        now = now if now is not None else time.time()

        row = conn.execute(
            "SELECT contact_id FROM contact_phones WHERE e164 = ?", (e164,)
        ).fetchone()

        if row:
            contact_id = row["contact_id"]
            if consent:
                conn.execute(
                    """UPDATE contact_phones
                       SET consented_at = COALESCE(consented_at, ?),
                           consent_source = COALESCE(consent_source, ?)
                       WHERE e164 = ?""",
                    (now, consent_source, e164),
                )
            if commit:
                conn.commit()
            return contact_id

        contact_id = self._new_contact_id_unlocked()
        conn.execute(
            "INSERT INTO contacts (id, display_name, created_at) VALUES (?, ?, ?)",
            (contact_id, display_name, now),
        )
        conn.execute(
            """INSERT INTO contact_phones
               (contact_id, e164, consented_at, consent_source, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (contact_id, e164,
             now if consent else None,
             consent_source if consent else None,
             now),
        )
        if commit:
            conn.commit()
        return contact_id

    def upsert_contact_by_phone(self, raw_phone: str, display_name: str = "") -> tuple[str, str]:
        """Find-or-create a contact by phone number.

        Returns (contact_id, normalized_e164).
        """
        e164 = normalize_phone(raw_phone) or raw_phone
        with self._id_lock:
            contact_id = self._upsert_contact_by_phone(e164, display_name)
        return contact_id, e164

    def upsert_contact_by_email(self, address: str, display_name: str = "") -> str:
        """Find-or-create a contact by email address. Returns contact_id."""
        conn = self._conn()
        address = address.strip().lower()
        row = conn.execute(
            "SELECT contact_id FROM contact_emails WHERE address = ?", (address,)
        ).fetchone()
        if row:
            return row["contact_id"]

        with self._id_lock:
            contact_id = self._new_contact_id_unlocked()
            conn.execute(
                "INSERT INTO contacts (id, display_name, created_at) VALUES (?, ?, unixepoch('subsec'))",
                (contact_id, display_name),
            )
            conn.execute(
                """INSERT INTO contact_emails (contact_id, address, created_at)
                   VALUES (?, ?, unixepoch('subsec'))""",
                (contact_id, address),
            )
            conn.commit()
        return contact_id

    def record_phone_consent(self, e164: str, source: str = "ivr"):
        self._conn().execute(
            """UPDATE contact_phones
               SET consented_at = COALESCE(consented_at, unixepoch('subsec')),
                   consent_source = COALESCE(consent_source, ?)
               WHERE e164 = ?""",
            (source, e164),
        )
        self._conn().commit()

    def phone_has_consent(self, e164: str) -> bool:
        row = self._conn().execute(
            "SELECT consented_at FROM contact_phones WHERE e164 = ?", (e164,)
        ).fetchone()
        return bool(row and row["consented_at"])

    def get_contact(self, contact_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        if not row:
            return None
        c = dict(row)
        c["phones"] = [
            dict(r) for r in conn.execute(
                "SELECT e164, consented_at, consent_source FROM contact_phones WHERE contact_id = ?",
                (contact_id,),
            ).fetchall()
        ]
        c["emails"] = [
            dict(r) for r in conn.execute(
                "SELECT address, consented_at, consent_source FROM contact_emails WHERE contact_id = ?",
                (contact_id,),
            ).fetchall()
        ]
        return c

    def list_contacts(self, limit: int = 100, offset: int = 0) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            """SELECT c.*,
                     (SELECT GROUP_CONCAT(e164) FROM contact_phones WHERE contact_id = c.id) AS phones,
                     (SELECT GROUP_CONCAT(address) FROM contact_emails WHERE contact_id = c.id) AS emails
               FROM contacts c ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_contact(self, contact_id: str, display_name: str | None = None,
                       notes: str | None = None):
        sets = []
        args = []
        if display_name is not None:
            sets.append("display_name = ?")
            args.append(display_name)
        if notes is not None:
            sets.append("notes = ?")
            args.append(notes)
        if not sets:
            return
        args.append(contact_id)
        self._conn().execute(
            f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?", args,
        )
        self._conn().commit()

    # --- Incidents ---

    def create_incident(
        self,
        contact_id: str | None = None,
        subject: str = "",
        channel: str = "phone",
        status: str = "open",
        priority: str = "normal",
    ) -> str:
        incident_id = self.new_incident_id()
        now = time.time()
        self._conn().execute(
            """INSERT INTO incidents
               (id, contact_id, subject, status, priority, labels, channel,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, '[]', ?, ?, ?)""",
            (incident_id, contact_id, subject, status, priority, channel, now, now),
        )
        self._conn().commit()
        return incident_id

    def get_incident(self, incident_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["labels"] = json.loads(d.get("labels") or "[]")
        except json.JSONDecodeError:
            d["labels"] = []
        return d

    def list_incidents(
        self, status: str | None = None, contact_id: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        conn = self._conn()
        where = ["1=1"]
        args: list = []
        if status:
            where.append("i.status = ?")
            args.append(status)
        if contact_id:
            where.append("i.contact_id = ?")
            args.append(contact_id)
        wclause = "WHERE " + " AND ".join(where)
        args.extend([limit, offset])
        # Left-join contact display name and first phone/email so the UI
        # can show "Name" / "Issue" / "meta" rows without a follow-up fetch.
        rows = conn.execute(
            f"""SELECT i.*,
                       c.display_name AS contact_name,
                       (SELECT e164 FROM contact_phones
                          WHERE contact_id = i.contact_id
                          ORDER BY id LIMIT 1) AS contact_phone,
                       (SELECT address FROM contact_emails
                          WHERE contact_id = i.contact_id
                          ORDER BY id LIMIT 1) AS contact_email
                FROM incidents i
                LEFT JOIN contacts c ON c.id = i.contact_id
                {wclause}
                ORDER BY i.updated_at DESC
                LIMIT ? OFFSET ?""",
            args,
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["labels"] = json.loads(d.get("labels") or "[]")
            except json.JSONDecodeError:
                d["labels"] = []
            result.append(d)
        return result

    def update_incident(
        self,
        incident_id: str,
        status: str | None = None,
        priority: str | None = None,
        subject: str | None = None,
        assigned_to: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> bool:
        conn = self._conn()
        cur = self.get_incident(incident_id)
        if not cur:
            return False
        sets = ["updated_at = ?"]
        args: list = [time.time()]
        if status is not None and status != cur["status"]:
            sets.append("status = ?")
            args.append(status)
            self.add_incident_entry(
                incident_id, "status_change",
                payload={"from": cur["status"], "to": status},
            )
        if priority is not None and priority != cur["priority"]:
            sets.append("priority = ?")
            args.append(priority)
        if subject is not None:
            sets.append("subject = ?")
            args.append(subject)
        if assigned_to is not None:
            sets.append("assigned_to = ?")
            args.append(assigned_to)

        labels = list(cur.get("labels") or [])
        if remove_labels:
            labels = [l for l in labels if l not in remove_labels]
        if add_labels:
            for l in add_labels:
                if l not in labels:
                    labels.append(l)
        if add_labels or remove_labels:
            sets.append("labels = ?")
            args.append(json.dumps(labels))
            self.add_incident_entry(
                incident_id, "label_change",
                payload={"labels": labels},
            )

        args.append(incident_id)
        conn.execute(
            f"UPDATE incidents SET {', '.join(sets)} WHERE id = ?", args,
        )
        conn.commit()
        return True

    def add_incident_entry(
        self,
        incident_id: str,
        entry_type: str,
        author: str = "",
        linked_call_id: str | None = None,
        linked_email_id: int | None = None,
        payload: dict | None = None,
        occurred_at: float | None = None,
    ) -> int:
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO incident_entries
               (incident_id, type, occurred_at, author, linked_call_id,
                linked_email_id, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                incident_id, entry_type,
                occurred_at if occurred_at is not None else time.time(),
                author, linked_call_id, linked_email_id,
                json.dumps(payload or {}),
            ),
        )
        conn.execute(
            "UPDATE incidents SET updated_at = unixepoch('subsec') WHERE id = ?",
            (incident_id,),
        )
        conn.commit()
        return cur.lastrowid

    def list_incident_entries(self, incident_id: str) -> list[dict]:
        rows = self._conn().execute(
            """SELECT * FROM incident_entries WHERE incident_id = ?
               ORDER BY occurred_at ASC, id ASC""",
            (incident_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except json.JSONDecodeError:
                d["payload"] = {}
            result.append(d)
        return result

    # --- Calls ---

    def save_call(self, record: CallRecord):
        conn = self._conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO calls
                   (id, caller_id, direction, state, started_at, answered_at,
                    ended_at, duration_seconds, was_bridged, consented,
                    caller_recording_path, tech_recording_path, voicemail_path,
                    incident_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id, record.caller_id, record.direction, record.state,
                    record.started_at, record.answered_at, record.ended_at,
                    record.duration_seconds, int(record.was_bridged),
                    int(record.consented),
                    record.caller_recording_path, record.tech_recording_path,
                    record.voicemail_path, record.incident_id,
                ),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def save_transcript_segment(self, call_id: str, speaker: str,
                                 text: str, timestamp_offset: float):
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO transcript_segments (call_id, speaker, text, timestamp_offset)
                   VALUES (?, ?, ?, ?)""",
                (call_id, speaker, text, timestamp_offset),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

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

    def get_transcript_for_incident(self, incident_id: str) -> list[dict]:
        """Aggregate transcripts from all calls linked to the incident."""
        rows = self._conn().execute(
            """SELECT ts.* FROM transcript_segments ts
               JOIN calls c ON c.id = ts.call_id
               WHERE c.incident_id = ?
               ORDER BY c.started_at, ts.timestamp_offset""",
            (incident_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_calls_for_incident(self, incident_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM calls WHERE incident_id = ? ORDER BY started_at",
            (incident_id,),
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

    # --- Todos ---

    def list_todos(self, incident_id: str) -> list[dict]:
        rows = self._conn().execute(
            """SELECT * FROM incident_todos
               WHERE incident_id = ?
               ORDER BY done ASC, position ASC, id ASC""",
            (incident_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_todo(self, incident_id: str, text: str, author: str = "operator") -> int:
        conn = self._conn()
        # Append to the end: position = (max existing) + 1
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM incident_todos WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        next_pos = (row[0] or 0) + 1
        cur = conn.execute(
            """INSERT INTO incident_todos (incident_id, text, author, position)
               VALUES (?, ?, ?, ?)""",
            (incident_id, text, author, next_pos),
        )
        conn.commit()
        return cur.lastrowid

    def update_todo(
        self, todo_id: int,
        text: str | None = None,
        done: bool | None = None,
    ) -> bool:
        conn = self._conn()
        sets = []
        args: list = []
        if text is not None:
            sets.append("text = ?")
            args.append(text)
        if done is not None:
            sets.append("done = ?")
            args.append(1 if done else 0)
            sets.append("completed_at = ?")
            args.append(time.time() if done else None)
        if not sets:
            return False
        args.append(todo_id)
        cur = conn.execute(
            f"UPDATE incident_todos SET {', '.join(sets)} WHERE id = ?",
            args,
        )
        conn.commit()
        return cur.rowcount > 0

    def delete_todo(self, todo_id: int) -> bool:
        conn = self._conn()
        cur = conn.execute("DELETE FROM incident_todos WHERE id = ?", (todo_id,))
        conn.commit()
        return cur.rowcount > 0

    def get_todo(self, todo_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM incident_todos WHERE id = ?", (todo_id,)
        ).fetchone()
        return dict(row) if row else None

    # --- Emails ---

    def save_email(self, msg: EmailMessage, status: str = None,
                   status_reason: str = "") -> int:
        conn = self._conn()
        # Default the status based on whether the email is already attached
        if status is None:
            status = "attached" if msg.incident_id else "pending"
        cur = conn.execute(
            """INSERT INTO emails
               (message_id, incident_id, direction, from_addr, to_addr,
                subject, body_text, body_html, received_at, in_reply_to,
                status, status_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.message_id, msg.incident_id, msg.direction,
                msg.from_addr, msg.to_addr, msg.subject,
                msg.body_text, msg.body_html,
                msg.received_at or time.time(),
                msg.in_reply_to,
                status, status_reason,
            ),
        )
        conn.commit()
        return cur.lastrowid

    def get_email(self, email_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM emails WHERE id = ?", (email_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_email_by_message_id(self, message_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_incident_by_email_reference(self, in_reply_to: str) -> str | None:
        row = self._conn().execute(
            "SELECT incident_id FROM emails WHERE message_id = ?",
            (in_reply_to,),
        ).fetchone()
        return row["incident_id"] if row else None

    def list_emails_by_status(self, status: str, limit: int = 100) -> list[dict]:
        """Emails in a given status. Status values: pending, flagged, rejected, attached."""
        rows = self._conn().execute(
            """SELECT id, message_id, from_addr, to_addr, subject,
                      substr(body_text, 1, 200) AS preview,
                      received_at, in_reply_to, status, status_reason,
                      incident_id
               FROM emails
               WHERE direction = 'in' AND status = ?
               ORDER BY received_at DESC
               LIMIT ?""",
            (status, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_pending_emails(self, limit: int = 100) -> list[dict]:
        """Inbound emails waiting for agent triage (status='pending')."""
        return self.list_emails_by_status("pending", limit)

    def set_email_status(self, email_id: int, status: str, reason: str = "") -> bool:
        conn = self._conn()
        cur = conn.execute(
            "UPDATE emails SET status = ?, status_reason = ? WHERE id = ?",
            (status, reason, email_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def list_emails_for_incident(self, incident_id: str) -> list[dict]:
        rows = self._conn().execute(
            """SELECT * FROM emails WHERE incident_id = ?
               ORDER BY received_at ASC""",
            (incident_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def attach_email_to_incident(self, email_id: int, incident_id: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            """UPDATE emails
               SET incident_id = ?, status = 'attached', status_reason = ''
               WHERE id = ? AND incident_id IS NULL""",
            (incident_id, email_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def delete_email(self, email_id: int) -> bool:
        conn = self._conn()
        cur = conn.execute("DELETE FROM emails WHERE id = ?", (email_id,))
        conn.commit()
        return cur.rowcount > 0
