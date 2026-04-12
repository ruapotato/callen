# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Technician-first outbound bridging.

Flow:
  1. Callen dials the operator's cell phone.
  2. Operator answers. Callen plays a confirmation prompt:
     "Connecting outbound call to <contact> at <number>. Press 1 to proceed,
      any other key or hang up to cancel."
  3. On DTMF "1", Callen dials the contact.
  4. On contact answer, Callen plays a disclosure
     "This call is being recorded." (full consent gate if the contact
     has never consented before).
  5. Bridge both legs, start split-channel recording and transcription.
  6. Link both calls to the incident in the timeline.

This module runs outside the normal inbound IVR thread. It uses the same
SIPCommandQueue to talk to pjsua2 from worker threads.
"""

import logging
import os
import time
import threading
from datetime import datetime

import pjsua2 as pj

from callen.sip.call import CallenCall, CallState
from callen.sip.dtmf import collect_dtmf
from callen.sip import bridge as br
from callen.storage.models import CallRecord
from callen.ivr import api

log = logging.getLogger(__name__)

# Set by app.py at startup
_cmd_queue = None
_config = None
_event_bus = None
_operator_state = None
_db = None
_sip_account = None
_call_registry = None


def configure(cmd_queue, config, event_bus, operator_state, db, sip_account, call_registry):
    global _cmd_queue, _config, _event_bus, _operator_state, _db, _sip_account, _call_registry
    _cmd_queue = cmd_queue
    _config = config
    _event_bus = event_bus
    _operator_state = operator_state
    _db = db
    _sip_account = sip_account
    _call_registry = call_registry


def _sip(fn, *args, **kwargs):
    return _cmd_queue.submit(fn, *args, **kwargs).result(timeout=30)


def _place_outbound(destination: str, label: str) -> CallenCall | None:
    """Place an outbound SIP call. Returns the call object (not yet answered)."""
    def _do():
        call = CallenCall(_sip_account)
        call.caller_id = label
        prm = pj.CallOpParam(True)
        try:
            call.makeCall(destination, prm)
            return call
        except Exception:
            log.exception("makeCall failed for %s", destination)
            return None
    return _sip(_do)


def originate(incident_id: str, destination: str, display_name: str = ""):
    """Kick off a technician-first outbound call.

    Runs in a dedicated thread so the caller (CLI or REST endpoint) doesn't
    block. Publishes events on the event bus so the dashboard can track it.
    """
    thread = threading.Thread(
        target=_run_originate,
        args=(incident_id, destination, display_name),
        name=f"out-{incident_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _run_originate(incident_id: str, destination: str, display_name: str):
    """Worker: the full outbound flow."""
    try:
        ep = pj.Endpoint.instance()
        if not ep.libIsThreadRegistered():
            ep.libRegisterThread(threading.current_thread().name)
    except Exception:
        log.exception("Failed to register outbound thread with pjlib")

    log.info("[%s] Originating outbound to %s (%s)", incident_id, destination, display_name or "-")

    if not _operator_state.is_available:
        log.warning("[%s] Operator not available — refusing to originate", incident_id)
        _db.add_incident_entry(
            incident_id, "note", author="system",
            payload={"text": f"Outbound call refused: operator not available"},
        )
        return

    # Strip +, assume E.164-ish
    clean_dest = destination.lstrip("+")
    dest_uri = f"sip:{clean_dest}@{_config.sip.domain}"
    cell = _config.operator.cell_phone.lstrip("+")
    tech_uri = f"sip:{cell}@{_config.sip.domain}"

    # Mark operator busy so inbound callers get the busy voicemail
    _operator_state.auto_busy()

    _db.add_incident_entry(
        incident_id, "note", author="system",
        payload={"text": f"Outbound call initiated to {display_name or clean_dest}"},
    )

    tech_call = None
    contact_call = None

    try:
        # --- Step 1: dial the technician's cell ---
        log.info("[%s] Calling operator at %s", incident_id, tech_uri)
        tech_call = _place_outbound(tech_uri, label="operator")
        if tech_call is None:
            _db.add_incident_entry(
                incident_id, "note", author="system",
                payload={"text": "Outbound cancelled: could not place call to operator"},
            )
            return

        if _call_registry is not None:
            _call_registry.add(tech_call)

        # Wait up to 30s for the tech to actually answer (CONFIRMED)
        deadline = time.time() + 30
        while time.time() < deadline:
            if tech_call.state == CallState.ACTIVE:
                break
            if tech_call.state == CallState.DISCONNECTED:
                break
            time.sleep(0.2)

        if tech_call.state != CallState.ACTIVE:
            log.info("[%s] Operator didn't answer the outbound-request call", incident_id)
            try:
                _sip(tech_call.hangup, pj.CallOpParam())
            except Exception:
                pass
            _db.add_incident_entry(
                incident_id, "note", author="system",
                payload={"text": "Outbound cancelled: operator did not answer"},
            )
            return

        # --- Step 2: confirmation prompt ---
        confirm_text = (
            f"Callen here. Connecting you to {display_name or 'the contact'} "
            f"at {_spell(clean_dest)}. "
            f"Press 1 to proceed, or hang up to cancel."
        )
        api.say(tech_call, confirm_text, repeat=False)
        key = collect_dtmf(tech_call, count=1, timeout=20)

        if key != '1':
            log.info("[%s] Operator did not confirm (key=%s)", incident_id, key)
            api.say(tech_call, "Cancelled. Goodbye.", repeat=False)
            try:
                _sip(tech_call.hangup, pj.CallOpParam())
            except Exception:
                pass
            _db.add_incident_entry(
                incident_id, "note", author="system",
                payload={"text": "Outbound cancelled by operator"},
            )
            return

        # --- Step 3: dial the contact ---
        api.say(tech_call, "Connecting now. Please hold.", repeat=False)

        log.info("[%s] Calling contact at %s", incident_id, dest_uri)
        contact_call = _place_outbound(dest_uri, label=clean_dest)
        if contact_call is None:
            api.say(tech_call, "Could not place the call. Goodbye.", repeat=False)
            try:
                _sip(tech_call.hangup, pj.CallOpParam())
            except Exception:
                pass
            _db.add_incident_entry(
                incident_id, "note", author="system",
                payload={"text": f"Outbound failed: makeCall to {clean_dest} returned error"},
            )
            return

        contact_call.incident_id = incident_id
        contact_call.direction = "outbound"
        if _call_registry is not None:
            _call_registry.add(contact_call)

        # Wait for the contact to actually answer — same ring timeout as inbound
        deadline = time.time() + 22
        while time.time() < deadline:
            if contact_call.state == CallState.ACTIVE:
                break
            if contact_call.state == CallState.DISCONNECTED:
                break
            if tech_call.state == CallState.DISCONNECTED:
                break
            time.sleep(0.2)

        if contact_call.state != CallState.ACTIVE:
            api.say(tech_call, "The contact did not answer.", repeat=False)
            try:
                _sip(contact_call.hangup, pj.CallOpParam())
            except Exception:
                pass
            _db.add_incident_entry(
                incident_id, "note", author="system",
                payload={"text": f"Outbound: contact {clean_dest} did not answer"},
            )
            return

        # --- Step 4: disclosure to the contact ---
        # Very brief — they already picked up, we just inform them
        api.say(contact_call,
                "This call is being recorded. Please continue.",
                repeat=False)

        # Insert a stub row in the calls table so transcript_segments.FK to
        # calls(id) is satisfied once the transcription workers fire. This
        # is the outbound equivalent of the inbound on_call_incoming handler.
        try:
            stub = CallRecord(
                id=contact_call.uuid,
                caller_id=clean_dest,
                direction="outbound",
                state="active",
                started_at=contact_call.started_at,
                answered_at=contact_call.answered_at or time.time(),
                consented=True,
                incident_id=incident_id,
            )
            _db.save_call(stub)
        except Exception:
            log.exception("[%s] Failed to save outbound call stub", incident_id)

        # --- Step 5: bridge + record + transcribe ---
        caller_media = contact_call.get_audio_media()
        tech_media = tech_call.get_audio_media()

        if not (caller_media and tech_media):
            log.error("[%s] Missing media on bridge setup", incident_id)
            return

        _sip(br.connect_calls, caller_media, tech_media)
        log.info("[%s] Outbound call bridged", incident_id)

        _db.add_incident_entry(
            incident_id, "call", linked_call_id=contact_call.uuid,
            payload={"direction": "outbound", "caller_id": clean_dest,
                     "initiated_by": "cli"},
        )

        # Stash recording paths on the contact_call so _save_call_record picks them up
        api._start_recording(contact_call, caller_media, tech_media)
        api._start_transcription(contact_call, caller_media, tech_media)

        # Wait for either side to hang up
        while (contact_call.state != CallState.DISCONNECTED and
               tech_call.state != CallState.DISCONNECTED):
            time.sleep(0.5)

        api._stop_recording(contact_call)
        api._stop_transcription(contact_call, caller_media, tech_media)
        _sip(br.disconnect_calls, caller_media, tech_media)

        # Trigger the same bridge-completed event path as inbound bridges
        # so the auto-agent review runs on outbound calls too.
        _event_bus.publish("call.bridge_completed", {
            "incident_id": incident_id,
            "call_id": contact_call.uuid,
            "caller_id": clean_dest,
        })

        # Propagate hangup to the other leg
        if contact_call.state == CallState.DISCONNECTED and tech_call.state != CallState.DISCONNECTED:
            try:
                _sip(tech_call.hangup, pj.CallOpParam())
            except Exception:
                pass
        elif tech_call.state == CallState.DISCONNECTED and contact_call.state != CallState.DISCONNECTED:
            try:
                _sip(contact_call.hangup, pj.CallOpParam())
            except Exception:
                pass

        log.info("[%s] Outbound call complete", incident_id)

    except Exception:
        log.exception("[%s] Outbound flow error", incident_id)
    finally:
        _operator_state.auto_available()
        for c in (tech_call, contact_call):
            if c and c.state != CallState.DISCONNECTED:
                try:
                    _sip(c.hangup, pj.CallOpParam())
                except Exception:
                    pass
        # Critical: remove both legs from the call registry so they don't
        # linger as zombie entries in the dashboard's live panel. Without
        # this, every outbound call left stale rows visible until restart.
        if _call_registry is not None:
            for c in (tech_call, contact_call):
                if c is not None:
                    try:
                        _call_registry.remove(c.uuid)
                    except Exception:
                        pass
        if contact_call:
            _event_bus.publish("call.ended", {"call_id": contact_call.uuid})


def _spell(number: str) -> str:
    """Speak a phone number digit-by-digit for clearer TTS."""
    if not number:
        return ""
    return " ".join(number)
