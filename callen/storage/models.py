# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Data classes for persisted records."""

from dataclasses import dataclass, field


# --- Contacts ---

@dataclass
class Contact:
    id: str                              # CON-NNNN
    display_name: str = ""
    notes: str = ""
    created_at: float = 0.0
    # Populated by joins when needed
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)


@dataclass
class ContactPhone:
    id: int = 0
    contact_id: str = ""
    e164: str = ""
    consented_at: float | None = None
    consent_source: str | None = None     # "ivr" / "email" / "manual"
    created_at: float = 0.0


@dataclass
class ContactEmail:
    id: int = 0
    contact_id: str = ""
    address: str = ""
    consented_at: float | None = None
    consent_source: str | None = None
    created_at: float = 0.0


# --- Incidents ---

@dataclass
class Incident:
    id: str                              # INC-NNNN
    contact_id: str | None = None
    subject: str = ""
    status: str = "open"                 # open / in_progress / waiting / resolved / closed
    priority: str = "normal"             # low / normal / high / urgent
    labels: list[str] = field(default_factory=list)
    channel: str = "phone"               # phone / email / manual
    assigned_to: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class IncidentEntry:
    id: int = 0
    incident_id: str = ""
    type: str = ""                       # call / email / note / status_change / label_change / consent
    occurred_at: float = 0.0
    author: str = ""
    linked_call_id: str | None = None
    linked_email_id: int | None = None
    payload: dict = field(default_factory=dict)


# --- Calls (existing — unchanged fields plus incident_id) ---

@dataclass
class CallRecord:
    id: str
    caller_id: str
    direction: str = "inbound"
    state: str = "completed"
    started_at: float = 0.0
    answered_at: float | None = None
    ended_at: float | None = None
    duration_seconds: float = 0.0
    was_bridged: bool = False
    consented: bool = False
    caller_recording_path: str | None = None
    tech_recording_path: str | None = None
    voicemail_path: str | None = None
    incident_id: str | None = None


@dataclass
class TranscriptSegment:
    id: int = 0
    call_id: str = ""
    speaker: str = ""                    # "caller" or "technician"
    text: str = ""
    timestamp_offset: float = 0.0
    created_at: float = 0.0


@dataclass
class Note:
    id: int = 0
    call_id: str = ""                    # kept for legacy, new notes use incident_id via IncidentEntry
    author: str = ""
    text: str = ""
    created_at: float = 0.0


# --- Emails ---

@dataclass
class EmailMessage:
    id: int = 0
    message_id: str = ""                 # RFC 5322 Message-ID, unique
    incident_id: str | None = None
    direction: str = "in"                # in / out
    from_addr: str = ""
    to_addr: str = ""
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    received_at: float = 0.0
    in_reply_to: str = ""
