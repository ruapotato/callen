# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
CallenCall — wraps pjsua2 Call. SWIG API (getInfo(), C-style constants).
"""

import enum
import logging
import queue
import threading
import time
import uuid

import pjsua2 as pj

log = logging.getLogger(__name__)


class CallState(enum.Enum):
    RINGING = "ringing"
    ACTIVE = "active"
    BRIDGED = "bridged"
    DISCONNECTED = "disconnected"


class CallenCall(pj.Call):
    def __init__(self, account, call_id=pj.PJSUA_INVALID_ID):
        super().__init__(account, call_id)
        self.uuid = str(uuid.uuid4())
        self.caller_id = ""
        self.state = CallState.RINGING
        self.dtmf_queue: queue.Queue[str | None] = queue.Queue()
        self.media_ready = threading.Event()
        self.started_at = time.time()
        self.answered_at: float | None = None
        self.ended_at: float | None = None
        self.consented_to_recording = False
        self._on_state_change = None
        self._on_media_ready = None
        self._audio_media: pj.AudioMedia | None = None
        self.last_status_code: int = 0
        self.last_reason: str = ""
        # Sticky: set True the moment the bridge is established. Stays
        # True through DISCONNECTED so downstream handlers (incident
        # auto-close, call record row) can tell whether this call was
        # ever actually bridged.
        self.was_bridged: bool = False

    def set_callbacks(self, on_state_change=None, on_media_ready=None):
        self._on_state_change = on_state_change
        self._on_media_ready = on_media_ready

    def onCallState(self, prm):
        try:
            ci = self.getInfo()
            log.info("Call %s state: %s (%d)", self.uuid[:8], ci.stateText, ci.lastStatusCode)

            # Always capture the last SIP status so callers can report
            # why a call ended (e.g. 503 from the carrier vs. 487 user
            # cancel vs. 200 normal hangup).
            self.last_status_code = int(getattr(ci, "lastStatusCode", 0) or 0)
            self.last_reason = str(getattr(ci, "lastReason", "") or "")

            if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
                self.state = CallState.ACTIVE
                self.answered_at = time.time()
            elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self.state = CallState.DISCONNECTED
                self.ended_at = time.time()
                self.dtmf_queue.put(None)
                self.media_ready.set()

            if self._on_state_change:
                self._on_state_change(self)
        except Exception:
            log.exception("Error in onCallState")

    def onCallMediaState(self, prm):
        try:
            ci = self.getInfo()
            for i in range(len(ci.media)):
                if ci.media[i].type == pj.PJMEDIA_TYPE_AUDIO and \
                   ci.media[i].status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                    self._audio_media = self.getAudioMedia(i)
                    self.media_ready.set()
                    log.info("Call %s audio media active", self.uuid[:8])
                    if self._on_media_ready:
                        self._on_media_ready(self)
                    break
        except Exception:
            log.exception("Error in onCallMediaState")

    def onDtmfDigit(self, prm):
        digit = prm.digit
        log.info("Call %s DTMF: %s", self.uuid[:8], digit)
        self.dtmf_queue.put(digit)

    def get_audio_media(self) -> pj.AudioMedia | None:
        return self._audio_media

    def get_caller_id(self) -> str:
        try:
            ci = self.getInfo()
            uri = ci.remoteUri
            if "sip:" in uri:
                return uri.split("sip:")[1].split("@")[0].strip("<>+")
            return uri
        except Exception:
            return "unknown"

    @property
    def duration(self) -> float:
        end = self.ended_at or time.time()
        start = self.answered_at or self.started_at
        return end - start

    def cleanup(self):
        self._audio_media = None
