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

CURRENT_SCHEMA_VERSION = 12

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

        if self._current_version() < 5:
            self._migrate_to_v5()

        if self._current_version() < 6:
            self._migrate_to_v6()

        if self._current_version() < 7:
            self._migrate_to_v7()

        if self._current_version() < 8:
            self._migrate_to_v8()

        if self._current_version() < 9:
            self._migrate_to_v9()

        if self._current_version() < 10:
            self._migrate_to_v10()

        if self._current_version() < 11:
            self._migrate_to_v11()

        if self._current_version() < 12:
            self._migrate_to_v12()

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

    def _migrate_to_v12(self):
        """Add processes and process_runs tables for scheduled/on-demand scripts."""
        log.info("Migrating database schema v11 -> v12 (processes)")
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS processes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                script_path TEXT NOT NULL,
                cron_schedule TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
            );

            CREATE TABLE IF NOT EXISTS process_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                process_id TEXT NOT NULL REFERENCES processes(id),
                started_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                finished_at REAL,
                exit_code INTEGER,
                output TEXT DEFAULT '',
                triggered_by TEXT DEFAULT 'manual'
            );

            CREATE INDEX IF NOT EXISTS idx_process_runs_process ON process_runs(process_id);
            CREATE INDEX IF NOT EXISTS idx_process_runs_time ON process_runs(started_at);
            INSERT OR IGNORE INTO schema_version VALUES (12);
        """)
        # Seed the database backup process
        conn.execute("""
            INSERT OR IGNORE INTO processes (id, name, description, script_path, cron_schedule)
            VALUES ('db-backup', 'Database Backup',
                    'Backs up callen.db to backups/ with 30-day retention',
                    'scripts/db-backup.sh', '0 17 * * *')
        """)
        conn.commit()
        log.info("Migration v11 -> v12 complete")

    def _migrate_to_v11(self):
        """Add companies and machines tables for MSP billing."""
        log.info("Migrating database schema v10 -> v11 (companies + machines)")
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS companies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                plan TEXT NOT NULL DEFAULT 'hourly',
                rate_workstation REAL NOT NULL DEFAULT 30.0,
                rate_server REAL NOT NULL DEFAULT 100.0,
                rate_hourly REAL NOT NULL DEFAULT 75.0,
                nda_on_file INTEGER NOT NULL DEFAULT 0,
                billing_contact_id TEXT REFERENCES contacts(id),
                notes TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                updated_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
            );

            CREATE TABLE IF NOT EXISTS machines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id TEXT NOT NULL REFERENCES companies(id),
                hostname TEXT NOT NULL DEFAULT '',
                machine_type TEXT NOT NULL DEFAULT 'workstation',
                rustdesk_id TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
            );

            CREATE INDEX IF NOT EXISTS idx_machines_company ON machines(company_id);
            INSERT OR IGNORE INTO schema_version VALUES (11);
        """)
        # Add company_id to contacts so employees can belong to a company
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        if "company_id" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN company_id TEXT REFERENCES companies(id)")
        conn.commit()
        log.info("Migration v10 -> v11 complete")

    def _migrate_to_v10(self):
        """Add privacy_mode and nickname to contacts for recording privacy."""
        log.info("Migrating database schema v9 -> v10 (contact privacy)")
        conn = self._conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        if "privacy_mode" not in cols:
            conn.execute(
                "ALTER TABLE contacts ADD COLUMN privacy_mode INTEGER NOT NULL DEFAULT 0"
            )
        if "nickname" not in cols:
            conn.execute(
                "ALTER TABLE contacts ADD COLUMN nickname TEXT DEFAULT ''"
            )
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (10)")
        conn.commit()
        log.info("Migration v9 -> v10 complete")

    def _migrate_to_v9(self):
        """Add call_events table for IVR flow tracking."""
        log.info("Migrating database schema v8 -> v9 (call events)")
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                detail TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_call_events_call ON call_events(call_id);
            CREATE INDEX IF NOT EXISTS idx_call_events_type ON call_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_call_events_time ON call_events(occurred_at);
            INSERT OR IGNORE INTO schema_version VALUES (9);
        """)
        conn.commit()
        log.info("Migration v8 -> v9 complete")

    def _migrate_to_v8(self):
        """Add managed_sites table for the freesoft.page hosting service."""
        log.info("Migrating database schema v7 -> v8 (managed sites)")
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS managed_sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subdomain TEXT NOT NULL UNIQUE,
                contact_id TEXT NOT NULL REFERENCES contacts(id),
                repo_url TEXT DEFAULT '',
                fqdn TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL DEFAULT (unixepoch('subsec')),
                updated_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
            );
            CREATE INDEX IF NOT EXISTS idx_managed_sites_contact ON managed_sites(contact_id);
            CREATE INDEX IF NOT EXISTS idx_managed_sites_subdomain ON managed_sites(subdomain);
            INSERT OR IGNORE INTO schema_version VALUES (8);
        """)
        conn.commit()
        log.info("Migration v7 -> v8 complete")

    def _migrate_to_v7(self):
        """Add trust_level to contacts: 'unverified' (default), 'verified',
        or 'suspect'. Set automatically to 'suspect' when an inbound
        email from a known contact trips the injection scanner."""
        log.info("Migrating database schema v6 -> v7 (contact trust level)")
        conn = self._conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()}
        if "trust_level" not in cols:
            conn.execute(
                "ALTER TABLE contacts ADD COLUMN trust_level TEXT NOT NULL DEFAULT 'unverified'"
            )
        if "trust_updated_at" not in cols:
            conn.execute("ALTER TABLE contacts ADD COLUMN trust_updated_at REAL")
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (7)")
        conn.commit()
        log.info("Migration v6 -> v7 complete")

    def _migrate_to_v6(self):
        """Add blocked_at / blocked_reason columns to contact_phones and
        contact_emails so senders can be hard-quarantined before any
        LLM or agent sees their content."""
        log.info("Migrating database schema v5 -> v6 (bad actor blocking)")
        conn = self._conn()

        # contact_phones
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contact_phones)").fetchall()}
        if "blocked_at" not in cols:
            conn.execute("ALTER TABLE contact_phones ADD COLUMN blocked_at REAL")
        if "blocked_reason" not in cols:
            conn.execute("ALTER TABLE contact_phones ADD COLUMN blocked_reason TEXT DEFAULT ''")

        # contact_emails
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contact_emails)").fetchall()}
        if "blocked_at" not in cols:
            conn.execute("ALTER TABLE contact_emails ADD COLUMN blocked_at REAL")
        if "blocked_reason" not in cols:
            conn.execute("ALTER TABLE contact_emails ADD COLUMN blocked_reason TEXT DEFAULT ''")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_phones_blocked ON contact_phones(blocked_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_emails_blocked ON contact_emails(blocked_at)"
        )
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (6)")
        conn.commit()
        log.info("Migration v5 -> v6 complete")

    def _migrate_to_v5(self):
        """Add email_attachments table for OCR'd images and extracted PDFs."""
        log.info("Migrating database schema v4 -> v5 (email attachments)")
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS email_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL REFERENCES emails(id),
                filename TEXT DEFAULT '',
                content_type TEXT DEFAULT '',
                file_path TEXT DEFAULT '',
                size_bytes INTEGER DEFAULT 0,
                extracted_text TEXT DEFAULT '',
                extraction_method TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT (unixepoch('subsec'))
            );
            CREATE INDEX IF NOT EXISTS idx_email_attachments_email
                ON email_attachments(email_id);
            INSERT OR IGNORE INTO schema_version VALUES (5);
        """)
        conn.commit()
        log.info("Migration v4 -> v5 complete")

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

    def revoke_phone_consent(self, e164: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "UPDATE contact_phones SET consented_at = NULL, consent_source = NULL WHERE e164 = ?",
            (e164,),
        )
        conn.commit()
        return cur.rowcount > 0

    def record_email_consent(self, address: str, source: str = "manual"):
        self._conn().execute(
            """UPDATE contact_emails
               SET consented_at = COALESCE(consented_at, unixepoch('subsec')),
                   consent_source = COALESCE(consent_source, ?)
               WHERE address = ?""",
            (source, address.lower()),
        )
        self._conn().commit()

    def revoke_email_consent(self, address: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "UPDATE contact_emails SET consented_at = NULL, consent_source = NULL WHERE address = ?",
            (address.lower(),),
        )
        conn.commit()
        return cur.rowcount > 0

    def phone_has_consent(self, e164: str) -> bool:
        row = self._conn().execute(
            "SELECT consented_at FROM contact_phones WHERE e164 = ?", (e164,)
        ).fetchone()
        return bool(row and row["consented_at"])

    def email_is_blocked(self, address: str) -> tuple[bool, str]:
        """Check if an email address is quarantined. Returns (blocked, reason)."""
        row = self._conn().execute(
            "SELECT blocked_at, blocked_reason FROM contact_emails WHERE address = ?",
            (address.lower(),),
        ).fetchone()
        if row and row["blocked_at"]:
            return True, (row["blocked_reason"] or "blocked")
        return False, ""

    def phone_is_blocked(self, e164: str) -> tuple[bool, str]:
        row = self._conn().execute(
            "SELECT blocked_at, blocked_reason FROM contact_phones WHERE e164 = ?",
            (e164,),
        ).fetchone()
        if row and row["blocked_at"]:
            return True, (row["blocked_reason"] or "blocked")
        return False, ""

    def block_email(self, address: str, reason: str = "manual") -> bool:
        conn = self._conn()
        cur = conn.execute(
            """UPDATE contact_emails
               SET blocked_at = unixepoch('subsec'), blocked_reason = ?
               WHERE address = ?""",
            (reason, address.lower()),
        )
        conn.commit()
        return cur.rowcount > 0

    def unblock_email(self, address: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            """UPDATE contact_emails
               SET blocked_at = NULL, blocked_reason = ''
               WHERE address = ?""",
            (address.lower(),),
        )
        conn.commit()
        return cur.rowcount > 0

    def block_phone(self, e164: str, reason: str = "manual") -> bool:
        conn = self._conn()
        cur = conn.execute(
            """UPDATE contact_phones
               SET blocked_at = unixepoch('subsec'), blocked_reason = ?
               WHERE e164 = ?""",
            (reason, e164),
        )
        conn.commit()
        return cur.rowcount > 0

    def unblock_phone(self, e164: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            """UPDATE contact_phones
               SET blocked_at = NULL, blocked_reason = ''
               WHERE e164 = ?""",
            (e164,),
        )
        conn.commit()
        return cur.rowcount > 0

    def list_blocked(self) -> dict:
        """Return dict with 'emails' and 'phones' lists of blocked addresses."""
        conn = self._conn()
        emails = [dict(r) for r in conn.execute(
            """SELECT address, contact_id, blocked_at, blocked_reason
               FROM contact_emails
               WHERE blocked_at IS NOT NULL
               ORDER BY blocked_at DESC"""
        ).fetchall()]
        phones = [dict(r) for r in conn.execute(
            """SELECT e164, contact_id, blocked_at, blocked_reason
               FROM contact_phones
               WHERE blocked_at IS NOT NULL
               ORDER BY blocked_at DESC"""
        ).fetchall()]
        return {"emails": emails, "phones": phones}

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
                """SELECT e164, consented_at, consent_source,
                          blocked_at, blocked_reason
                   FROM contact_phones WHERE contact_id = ?""",
                (contact_id,),
            ).fetchall()
        ]
        c["emails"] = [
            dict(r) for r in conn.execute(
                """SELECT address, consented_at, consent_source,
                          blocked_at, blocked_reason
                   FROM contact_emails WHERE contact_id = ?""",
                (contact_id,),
            ).fetchall()
        ]
        # Resolve company name if linked
        if c.get("company_id"):
            cmp = conn.execute(
                "SELECT name FROM companies WHERE id = ?", (c["company_id"],)
            ).fetchone()
            c["company_name"] = cmp["name"] if cmp else ""
        else:
            c["company_name"] = ""
        return c

    def list_contacts(self, limit: int = 100, offset: int = 0) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            """SELECT c.*,
                     (SELECT GROUP_CONCAT(e164) FROM contact_phones WHERE contact_id = c.id) AS phones,
                     (SELECT GROUP_CONCAT(address) FROM contact_emails WHERE contact_id = c.id) AS emails,
                     cmp.name AS company_name
               FROM contacts c
               LEFT JOIN companies cmp ON cmp.id = c.company_id
               ORDER BY c.created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_contact(self, contact_id: str, display_name: str | None = None,
                       notes: str | None = None, privacy_mode: bool | None = None,
                       nickname: str | None = None):
        sets = []
        args = []
        if display_name is not None:
            sets.append("display_name = ?")
            args.append(display_name)
        if notes is not None:
            sets.append("notes = ?")
            args.append(notes)
        if privacy_mode is not None:
            sets.append("privacy_mode = ?")
            args.append(1 if privacy_mode else 0)
        if nickname is not None:
            sets.append("nickname = ?")
            args.append(nickname)
        if not sets:
            return
        args.append(contact_id)
        self._conn().execute(
            f"UPDATE contacts SET {', '.join(sets)} WHERE id = ?", args,
        )
        self._conn().commit()

    def remove_contact_phone(self, contact_id: str, e164: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM contact_phones WHERE contact_id = ? AND e164 = ?",
            (contact_id, e164),
        )
        conn.commit()
        return cur.rowcount > 0

    def remove_contact_email(self, contact_id: str, address: str) -> bool:
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM contact_emails WHERE contact_id = ? AND address = ?",
            (contact_id, address.lower()),
        )
        conn.commit()
        return cur.rowcount > 0

    def rename_contact_phone(self, contact_id: str, old_e164: str, new_e164: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT contact_id FROM contact_phones WHERE e164 = ?", (new_e164,),
        ).fetchone()
        if row and row["contact_id"] != contact_id:
            return False
        cur = conn.execute(
            "UPDATE contact_phones SET e164 = ? WHERE contact_id = ? AND e164 = ?",
            (new_e164, contact_id, old_e164),
        )
        conn.commit()
        return cur.rowcount > 0

    def rename_contact_email(self, contact_id: str, old_addr: str, new_addr: str) -> bool:
        conn = self._conn()
        new_addr = new_addr.lower()
        row = conn.execute(
            "SELECT contact_id FROM contact_emails WHERE address = ?", (new_addr,),
        ).fetchone()
        if row and row["contact_id"] != contact_id:
            return False
        cur = conn.execute(
            "UPDATE contact_emails SET address = ? WHERE contact_id = ? AND address = ?",
            (new_addr, contact_id, old_addr.lower()),
        )
        conn.commit()
        return cur.rowcount > 0

    def delete_contact(self, contact_id: str, cascade: bool = False) -> dict:
        """Delete a contact. With cascade=True also deletes every incident
        attached to the contact (entries, todos, linked call/email rows
        detached). Without cascade, fails if any incident is still linked.
        Returns a summary dict of what was removed."""
        conn = self._conn()
        if not conn.execute(
            "SELECT 1 FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone():
            return {"error": "not found"}

        inc_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM incidents WHERE contact_id = ?", (contact_id,)
            ).fetchall()
        ]
        if inc_ids and not cascade:
            return {"error": "contact has incidents", "incidents": inc_ids}

        removed_incidents = 0
        for inc_id in inc_ids:
            self.delete_incident(inc_id)
            removed_incidents += 1

        # Clean up managed sites owned by this contact
        conn.execute("DELETE FROM managed_sites WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM contact_phones WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM contact_emails WHERE contact_id = ?", (contact_id,))
        conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        conn.commit()
        return {"deleted": contact_id, "removed_incidents": removed_incidents}

    def reassign_incident(self, incident_id: str, new_contact_id: str) -> bool:
        """Move an incident (and its calls + emails) to a different contact."""
        conn = self._conn()
        if not conn.execute(
            "SELECT 1 FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone():
            return False
        if not conn.execute(
            "SELECT 1 FROM contacts WHERE id = ?", (new_contact_id,)
        ).fetchone():
            return False
        conn.execute(
            "UPDATE incidents SET contact_id = ?, updated_at = ? WHERE id = ?",
            (new_contact_id, time.time(), incident_id),
        )
        conn.commit()
        self.add_incident_entry(
            incident_id, "note", author="system",
            payload={"text": f"Reassigned to contact {new_contact_id}"},
        )
        return True

    def set_contact_trust(self, contact_id: str, trust_level: str) -> bool:
        """Set a contact's trust_level. Valid values: unverified, verified, suspect."""
        if trust_level not in ("unverified", "verified", "suspect"):
            return False
        conn = self._conn()
        cur = conn.execute(
            "UPDATE contacts SET trust_level = ?, trust_updated_at = ? WHERE id = ?",
            (trust_level, time.time(), contact_id),
        )
        conn.commit()
        return cur.rowcount > 0

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
                       c.privacy_mode AS contact_privacy,
                       c.nickname AS contact_nickname,
                       (SELECT e164 FROM contact_phones
                          WHERE contact_id = i.contact_id
                          ORDER BY id LIMIT 1) AS contact_phone,
                       (SELECT address FROM contact_emails
                          WHERE contact_id = i.contact_id
                          ORDER BY id LIMIT 1) AS contact_email
                FROM incidents i
                LEFT JOIN contacts c ON c.id = i.contact_id
                {wclause}
                ORDER BY
                    CASE i.status
                        WHEN 'open' THEN 0
                        WHEN 'in_progress' THEN 1
                        WHEN 'waiting' THEN 2
                        WHEN 'resolved' THEN 3
                        WHEN 'closed' THEN 4
                        ELSE 5
                    END,
                    CASE i.priority
                        WHEN 'urgent' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'normal' THEN 2
                        WHEN 'low' THEN 3
                        ELSE 4
                    END,
                    i.updated_at DESC
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
        # Auto-complete all open todos when a ticket is closed or resolved
        if status in ("closed", "resolved"):
            conn.execute(
                "UPDATE incident_todos SET done = 1, completed_at = ? "
                "WHERE incident_id = ? AND done = 0",
                (time.time(), incident_id),
            )
        conn.commit()
        return True

    def delete_incident(self, incident_id: str) -> bool:
        """Hard-delete an incident and its entries/todos. Linked calls
        and emails are detached (incident_id set to NULL) so the audit
        trail isn't destroyed."""
        conn = self._conn()
        if not conn.execute(
            "SELECT 1 FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone():
            return False
        conn.execute("DELETE FROM incident_entries WHERE incident_id = ?", (incident_id,))
        conn.execute("DELETE FROM incident_todos WHERE incident_id = ?", (incident_id,))
        conn.execute("UPDATE calls SET incident_id = NULL WHERE incident_id = ?", (incident_id,))
        conn.execute("UPDATE emails SET incident_id = NULL WHERE incident_id = ?", (incident_id,))
        conn.execute("DELETE FROM incidents WHERE id = ?", (incident_id,))
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

    def list_all_todos(self, done: bool | None = False, limit: int = 500) -> list[dict]:
        """Aggregate todos across every incident. Joins incident + contact
        info so the dashboard can render a cross-ticket checklist view.

        done=False returns only open todos (default)
        done=True  returns only completed todos
        done=None  returns both
        """
        where = []
        args: list = []
        if done is False:
            where.append("t.done = 0")
        elif done is True:
            where.append("t.done = 1")
        wclause = "WHERE " + " AND ".join(where) if where else ""
        args.append(limit)
        rows = self._conn().execute(
            f"""SELECT t.id, t.incident_id, t.text, t.done, t.author,
                       t.created_at, t.completed_at,
                       i.subject AS incident_subject,
                       i.status AS incident_status,
                       i.priority AS incident_priority,
                       i.contact_id,
                       c.display_name AS contact_name
                FROM incident_todos t
                JOIN incidents i ON i.id = t.incident_id
                LEFT JOIN contacts c ON c.id = i.contact_id
                {wclause}
                ORDER BY
                  CASE i.priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high'   THEN 1
                    WHEN 'normal' THEN 2
                    WHEN 'low'    THEN 3
                    ELSE 4
                  END ASC,
                  t.incident_id ASC,
                  t.position ASC,
                  t.id ASC
                LIMIT ?""",
            args,
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

    # --- Companies + machines (MSP billing) ---

    def _new_company_id(self) -> str:
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM companies ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            num = int(row["id"].split("-")[1]) + 1
        else:
            num = 1
        return f"CMP-{num:04d}"

    def create_company(
        self, name: str, plan: str = "hourly",
        billing_contact_id: str | None = None,
        rate_workstation: float = 30.0,
        rate_server: float = 100.0,
        rate_hourly: float = 75.0,
    ) -> dict:
        company_id = self._new_company_id()
        now = time.time()
        self._conn().execute(
            """INSERT INTO companies
               (id, name, plan, billing_contact_id, rate_workstation,
                rate_server, rate_hourly, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (company_id, name, plan, billing_contact_id,
             rate_workstation, rate_server, rate_hourly, now, now),
        )
        self._conn().commit()
        return self.get_company(company_id)

    def get_company(self, company_id: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if not row:
            return None
        c = dict(row)
        c["machines"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM machines WHERE company_id = ? AND active = 1 ORDER BY hostname",
                (company_id,),
            ).fetchall()
        ]
        c["contacts"] = [
            dict(r) for r in conn.execute(
                "SELECT id, display_name, nickname, privacy_mode FROM contacts WHERE company_id = ?",
                (company_id,),
            ).fetchall()
        ]
        ws = sum(1 for m in c["machines"] if m["machine_type"] == "workstation")
        srv = sum(1 for m in c["machines"] if m["machine_type"] == "server")
        c["workstation_count"] = ws
        c["server_count"] = srv
        c["monthly_bill"] = ws * c["rate_workstation"] + srv * c["rate_server"]
        return c

    def list_companies(self, limit: int = 100) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM companies ORDER BY name LIMIT ?", (limit,),
        ).fetchall()
        result = []
        for row in rows:
            c = dict(row)
            machines = self._conn().execute(
                "SELECT machine_type, COUNT(*) n FROM machines WHERE company_id = ? AND active = 1 GROUP BY machine_type",
                (c["id"],),
            ).fetchall()
            ws = sum(r["n"] for r in machines if r["machine_type"] == "workstation")
            srv = sum(r["n"] for r in machines if r["machine_type"] == "server")
            c["workstation_count"] = ws
            c["server_count"] = srv
            c["monthly_bill"] = ws * c["rate_workstation"] + srv * c["rate_server"]
            result.append(c)
        return result

    def update_company(self, company_id: str, **kwargs) -> bool:
        allowed = {"name", "plan", "billing_contact_id", "rate_workstation",
                    "rate_server", "rate_hourly", "nda_on_file", "notes"}
        sets = []
        args = []
        for k, v in kwargs.items():
            if k in allowed and v is not None:
                sets.append(f"{k} = ?")
                args.append(v)
        if not sets:
            return False
        sets.append("updated_at = ?")
        args.append(time.time())
        args.append(company_id)
        cur = self._conn().execute(
            f"UPDATE companies SET {', '.join(sets)} WHERE id = ?", args,
        )
        self._conn().commit()
        return cur.rowcount > 0

    def delete_company(self, company_id: str) -> bool:
        conn = self._conn()
        conn.execute("DELETE FROM machines WHERE company_id = ?", (company_id,))
        conn.execute("UPDATE contacts SET company_id = NULL WHERE company_id = ?", (company_id,))
        cur = conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        conn.commit()
        return cur.rowcount > 0

    def add_machine(
        self, company_id: str, hostname: str,
        machine_type: str = "workstation", rustdesk_id: str = "",
        notes: str = "",
    ) -> dict:
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO machines
               (company_id, hostname, machine_type, rustdesk_id, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (company_id, hostname, machine_type, rustdesk_id, notes),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM machines WHERE id = ?", (cur.lastrowid,)
        ).fetchone())

    def remove_machine(self, machine_id: int) -> bool:
        cur = self._conn().execute(
            "UPDATE machines SET active = 0 WHERE id = ?", (machine_id,),
        )
        self._conn().commit()
        return cur.rowcount > 0

    def assign_contact_to_company(self, contact_id: str, company_id: str) -> bool:
        cur = self._conn().execute(
            "UPDATE contacts SET company_id = ? WHERE id = ?",
            (company_id, contact_id),
        )
        self._conn().commit()
        return cur.rowcount > 0

    # --- Processes (scheduled/on-demand scripts) ---

    def list_processes(self) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM processes ORDER BY name"
        ).fetchall()
        result = []
        for r in rows:
            p = dict(r)
            last = self._conn().execute(
                "SELECT * FROM process_runs WHERE process_id = ? ORDER BY started_at DESC LIMIT 1",
                (p["id"],),
            ).fetchone()
            p["last_run"] = dict(last) if last else None
            result.append(p)
        return result

    def get_process(self, process_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM processes WHERE id = ?", (process_id,),
        ).fetchone()
        if not row:
            return None
        p = dict(row)
        p["runs"] = [
            dict(r) for r in self._conn().execute(
                "SELECT * FROM process_runs WHERE process_id = ? ORDER BY started_at DESC LIMIT 20",
                (process_id,),
            ).fetchall()
        ]
        return p

    def create_process(self, process_id: str, name: str, script_path: str,
                       description: str = "", cron_schedule: str = "") -> dict:
        self._conn().execute(
            """INSERT INTO processes (id, name, description, script_path, cron_schedule)
               VALUES (?, ?, ?, ?, ?)""",
            (process_id, name, description, script_path, cron_schedule),
        )
        self._conn().commit()
        return self.get_process(process_id)

    def log_process_run(self, process_id: str, exit_code: int, output: str,
                        started_at: float, triggered_by: str = "manual") -> int:
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO process_runs
               (process_id, started_at, finished_at, exit_code, output, triggered_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (process_id, started_at, time.time(), exit_code, output, triggered_by),
        )
        conn.commit()
        return cur.lastrowid

    def get_scheduled_processes(self) -> list[dict]:
        """Processes with a cron_schedule that are enabled."""
        rows = self._conn().execute(
            "SELECT * FROM processes WHERE cron_schedule != '' AND enabled = 1"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Call events (IVR flow tracking) ---

    def log_call_event(self, call_id: str, event_type: str, detail: str = ""):
        self._conn().execute(
            "INSERT INTO call_events (call_id, event_type, occurred_at, detail) VALUES (?, ?, ?, ?)",
            (call_id, event_type, time.time(), detail),
        )
        self._conn().commit()

    def get_call_events(self, call_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT * FROM call_events WHERE call_id = ? ORDER BY occurred_at",
            (call_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def call_stats(self, since: float | None = None) -> dict:
        """Aggregate IVR funnel stats. If since is given, only count events
        after that unix timestamp."""
        conn = self._conn()
        where = ""
        args: list = []
        if since:
            where = "WHERE occurred_at >= ?"
            args = [since]
        rows = conn.execute(
            f"SELECT event_type, COUNT(*) n FROM call_events {where} GROUP BY event_type",
            args,
        ).fetchall()
        counts = {r["event_type"]: r["n"] for r in rows}
        total = counts.get("incoming", 0)
        return {
            "total_calls": total,
            "consent_prompted": counts.get("consent_prompted", 0),
            "consent_granted": counts.get("consent_granted", 0),
            "consent_skipped": counts.get("consent_skipped", 0),
            "hangup_during_consent": counts.get("hangup_during_consent", 0),
            "menu_played": counts.get("menu_played", 0),
            "dtmf_1_technician": counts.get("dtmf_1_technician", 0),
            "dtmf_2_voicemail": counts.get("dtmf_2_voicemail", 0),
            "dtmf_3_website": counts.get("dtmf_3_website", 0),
            "hangup_during_menu": counts.get("hangup_during_menu", 0),
            "bridged": counts.get("bridge_started", 0),
            "blocked": counts.get("blocked", 0),
        }

    # --- Managed sites ---

    def create_managed_site(
        self, subdomain: str, contact_id: str,
        repo_url: str = "", fqdn: str = "",
    ) -> dict:
        conn = self._conn()
        if not conn.execute(
            "SELECT 1 FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone():
            raise ValueError(f"contact {contact_id} not found")
        now = time.time()
        conn.execute(
            """INSERT INTO managed_sites
               (subdomain, contact_id, repo_url, fqdn, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (subdomain, contact_id, repo_url, fqdn, now, now),
        )
        conn.commit()
        return self.get_site_by_subdomain(subdomain)

    def get_site_by_subdomain(self, subdomain: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM managed_sites WHERE subdomain = ? AND status = 'active'",
            (subdomain,),
        ).fetchone()
        return dict(row) if row else None

    def get_sites_by_contact(self, contact_id: str) -> list[dict]:
        rows = self._conn().execute(
            """SELECT * FROM managed_sites
               WHERE contact_id = ? AND status = 'active'
               ORDER BY created_at DESC""",
            (contact_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_managed_sites(
        self, limit: int = 100, offset: int = 0, status: str | None = None,
    ) -> list[dict]:
        where = ["ms.status = 'active'"]
        args: list = []
        if status:
            where = ["ms.status = ?"]
            args.append(status)
        wclause = " AND ".join(where)
        args.extend([limit, offset])
        rows = self._conn().execute(
            f"""SELECT ms.*, c.display_name AS contact_name
                FROM managed_sites ms
                LEFT JOIN contacts c ON c.id = ms.contact_id
                WHERE {wclause}
                ORDER BY ms.created_at DESC
                LIMIT ? OFFSET ?""",
            args,
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_managed_site(self, subdomain: str) -> bool:
        """Hard-delete the managed_sites row. The repo and DNS are already
        gone by the time this is called — a soft-delete ghost row just
        creates FK conflicts when deleting the contact later."""
        cur = self._conn().execute(
            "DELETE FROM managed_sites WHERE subdomain = ?",
            (subdomain,),
        )
        self._conn().commit()
        return cur.rowcount > 0

    def verify_site_ownership(self, contact_id: str, subdomain: str) -> bool:
        row = self._conn().execute(
            "SELECT 1 FROM managed_sites WHERE contact_id = ? AND subdomain = ?",
            (contact_id, subdomain),
        ).fetchone()
        return row is not None

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

    # --- Email attachments ---

    def save_email_attachment(
        self,
        email_id: int,
        filename: str,
        content_type: str,
        file_path: str,
        size_bytes: int,
        extracted_text: str = "",
        extraction_method: str = "",
    ) -> int:
        conn = self._conn()
        cur = conn.execute(
            """INSERT INTO email_attachments
               (email_id, filename, content_type, file_path, size_bytes,
                extracted_text, extraction_method)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (email_id, filename, content_type, file_path, size_bytes,
             extracted_text, extraction_method),
        )
        conn.commit()
        return cur.lastrowid

    def list_email_attachments(self, email_id: int) -> list[dict]:
        rows = self._conn().execute(
            """SELECT id, filename, content_type, file_path, size_bytes,
                      extracted_text, extraction_method, created_at
               FROM email_attachments WHERE email_id = ? ORDER BY id""",
            (email_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_email_attachment(self, attachment_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM email_attachments WHERE id = ?", (attachment_id,),
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
