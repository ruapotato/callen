# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Data classes for persisted records."""

from dataclasses import dataclass, field


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


@dataclass
class TranscriptSegment:
    id: int = 0
    call_id: str = ""
    speaker: str = ""  # "caller" or "technician"
    text: str = ""
    timestamp_offset: float = 0.0
    created_at: float = 0.0


@dataclass
class Note:
    id: int = 0
    call_id: str = ""
    author: str = ""
    text: str = ""
    created_at: float = 0.0
