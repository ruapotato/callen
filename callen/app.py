# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Top-level orchestrator — wires everything together and runs the system."""

import asyncio
import logging
import signal
import sys
import threading

import pjsua2 as pj

from callen.config import load_config
from callen.sip.commands import SIPCommandQueue
from callen.sip.endpoint import SIPEndpoint
from callen.sip.account import CallenAccount
from callen.sip.call import CallenCall, CallState
from callen.sip.media import check_audio_tools
from callen.ivr.engine import IVREngine
from callen.ivr import api
from callen.state.events import EventBus
from callen.state.operator import OperatorState
from callen.state.calls import CallRegistry
from callen.storage.db import Database
from callen.storage.models import CallRecord
from callen.web.server import create_app
from callen.web.websocket import setup_event_forwarding

log = logging.getLogger(__name__)


def main(config_path: str = "config.toml"):
    config = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.general.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)-30s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info("Starting Callen IVR system")

    missing = check_audio_tools()
    if missing:
        log.warning("Missing audio tools: %s — install with: sudo apt install %s",
                     ", ".join(missing), " ".join(missing))

    event_bus = EventBus()
    cmd_queue = SIPCommandQueue()
    operator_state = OperatorState(event_bus, config.operator.default_status)
    call_registry = CallRegistry()
    db = Database(config.general.db_path)
    db.initialize()

    def _save_call_record(call):
        record = CallRecord(
            id=call.uuid,
            caller_id=call.caller_id,
            started_at=call.started_at,
            answered_at=call.answered_at,
            ended_at=call.ended_at,
            duration_seconds=call.duration,
            consented=call.consented_to_recording,
            was_bridged=(call.state == CallState.BRIDGED),
            state="active" if call.ended_at is None else "completed",
            incident_id=getattr(call, "incident_id", None),
            caller_recording_path=getattr(call, "caller_recording_path", None),
            tech_recording_path=getattr(call, "tech_recording_path", None),
            voicemail_path=getattr(call, "voicemail_path", None),
        )
        try:
            db.save_call(record)
        except Exception:
            log.exception("Failed to save call record")

    def on_call_incoming(data):
        call = call_registry.get(data.get("call_id"))
        if not call:
            return

        # Look up or create a contact for this caller. The contact is the
        # persistent identity across multiple calls from the same number.
        try:
            contact_id, e164 = db.upsert_contact_by_phone(call.caller_id)
            call.contact_id = contact_id
            call.normalized_phone = e164

            # Remember if this phone has already consented — the IVR script
            # can skip the consent gate for returning callers.
            call.prior_consent = db.phone_has_consent(e164)

            # Create a fresh incident for this call
            incident_id = db.create_incident(
                contact_id=contact_id,
                subject=f"Call from {e164}",
                channel="phone",
                status="open",
            )
            call.incident_id = incident_id
            db.add_incident_entry(
                incident_id, "call", linked_call_id=call.uuid,
                payload={"direction": "inbound", "caller_id": e164},
            )
            log.info("Call %s -> contact %s, incident %s (prior_consent=%s)",
                     call.uuid[:8], contact_id, incident_id, call.prior_consent)
        except Exception:
            log.exception("Failed to bind call to contact/incident")

        # Insert a stub call row so transcript_segments FK can reference it
        _save_call_record(call)

    def on_call_ended(data):
        call = call_registry.get(data.get("call_id"))
        if call:
            _save_call_record(call)
            # If the caller consented during the IVR, persist it on the contact
            if getattr(call, "consented_to_recording", False) and getattr(call, "normalized_phone", ""):
                try:
                    db.record_phone_consent(call.normalized_phone, source="ivr")
                except Exception:
                    log.exception("Failed to record phone consent")

    event_bus.subscribe("call.incoming", on_call_incoming)
    event_bus.subscribe("call.ended", on_call_ended)

    # Transcription manager (loads Parakeet model)
    transcription_mgr = None
    if config.transcription.enabled:
        try:
            from callen.transcription.parakeet import ParakeetProcessor
            from callen.transcription.manager import TranscriptionManager
            log.info("Loading Parakeet model (this takes a moment on first run)...")
            processor = ParakeetProcessor(
                model_id=config.transcription.model,
                device=config.transcription.device,
            )
            processor.setup()
            transcription_mgr = TranscriptionManager(
                processor, event_bus, config.transcription.chunk_seconds,
            )
            log.info("Transcription enabled")
        except Exception:
            log.exception("Failed to load Parakeet — continuing without transcription")

    api._transcription_mgr = transcription_mgr

    # Save transcript segments to DB
    def on_transcript(data):
        try:
            db.save_transcript_segment(
                data["call_id"], data["speaker"],
                data["text"], data["timestamp_offset"],
            )
        except Exception:
            log.exception("Failed to save transcript segment")
    event_bus.subscribe("transcript.update", on_transcript)

    ivr_engine = IVREngine(
        config=config,
        cmd_queue=cmd_queue,
        operator_state=operator_state,
        event_bus=event_bus,
        call_registry=call_registry,
    )

    sip_endpoint = SIPEndpoint(config.sip, cmd_queue)

    def on_incoming_call(call: CallenCall):
        ivr_engine.handle_call(call)

    sip_account = CallenAccount(config.sip, on_incoming_call)

    def make_outbound_call(inbound_call, dst_uri):
        """Place outbound call to operator. MUST return quickly (called on SIP thread).
        Caller waits on outbound.media_ready themselves."""
        outbound = CallenCall(sip_account)
        outbound.caller_id = "operator"
        prm = pj.CallOpParam(True)  # Use default opts
        try:
            outbound.makeCall(dst_uri, prm)
            return outbound
        except Exception:
            log.exception("Outbound call failed: %s", dst_uri)
            return None

    api._make_outbound_call = make_outbound_call

    sip_endpoint.start()
    # Register account on the SIP thread (all pjlib calls must happen there)
    cmd_queue.submit(sip_account.register).result(timeout=10)

    # Start web server in its own thread with its own asyncio loop
    web_loop = asyncio.new_event_loop()
    web_app = create_app(config.web, call_registry, operator_state, event_bus, db)

    def run_web():
        asyncio.set_event_loop(web_loop)
        # Forward EventBus events into the web loop's WebSocket broadcast funcs
        setup_event_forwarding(event_bus, web_loop)
        try:
            web_loop.run_until_complete(
                web_app.run_task(
                    host=config.web.host,
                    port=config.web.port,
                    shutdown_trigger=lambda: shutdown_event_async(web_loop),
                )
            )
        except Exception:
            log.exception("Web server crashed")

    shutdown_event = threading.Event()

    async def shutdown_event_async(loop):
        """Awaited by Quart; resolves when shutdown is requested."""
        while not shutdown_event.is_set():
            await asyncio.sleep(0.5)

    web_thread = threading.Thread(target=run_web, name="web", daemon=True)
    web_thread.start()

    log.info("=" * 60)
    log.info("Callen IVR running — waiting for calls")
    log.info("Web dashboard: http://%s:%d", config.web.host, config.web.port)
    log.info("Press Ctrl+C to shut down")
    log.info("=" * 60)

    def shutdown(sig, frame):
        log.info("Shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    sip_endpoint.shutdown()
    log.info("Callen stopped")
