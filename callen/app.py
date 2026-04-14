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
from callen.ivr import api, outbound
from callen.state.events import EventBus
from callen.state.operator import OperatorState
from callen.state.calls import CallRegistry
from callen.storage.db import Database
from callen.storage.models import CallRecord
from callen.web.server import create_app
from callen.web.websocket import setup_event_forwarding
from callen.agent.runner import AgentRunner

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
            direction=getattr(call, "direction", "inbound"),
            started_at=call.started_at,
            answered_at=call.answered_at,
            ended_at=call.ended_at,
            duration_seconds=call.duration,
            consented=call.consented_to_recording,
            was_bridged=getattr(call, "was_bridged", False) or (call.state == CallState.BRIDGED),
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

            # Hard block check — if this number is on the block list, flag
            # the call object so the IVR script can hang up immediately
            # without the operator or agent ever seeing it. Still creates
            # a contact / incident stub so we have an audit trail.
            try:
                blocked, block_reason = db.phone_is_blocked(e164)
                call.is_blocked = blocked
                call.block_reason = block_reason
                if blocked:
                    log.warning("BLOCKED caller %s: %s", e164, block_reason)
            except Exception:
                call.is_blocked = False
                call.block_reason = ""

            # Remember if this phone has already consented — the IVR script
            # can skip the consent gate for returning callers. Anonymous /
            # caller-ID-withheld callers ALWAYS hit the full consent gate,
            # since we have no way to verify who is on the other end.
            raw_cid = (call.caller_id or "").strip().lower()
            is_anon = (
                not e164
                or len(e164) < 7  # not a real phone number
                or any(s in raw_cid for s in (
                    "anonymous", "unknown", "restricted",
                    "withheld", "private", "unavailable",
                ))
            )
            call.is_anonymous = is_anon
            call.prior_consent = False if is_anon else db.phone_has_consent(e164)

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
            # NOTE: we do NOT auto-close bridged calls. The right
            # signal for "should this be an open ticket" is the CONTENT
            # of the call (real tech issue → keep open; test call /
            # marketing / unrelated → close), not whether a human
            # picked up. That decision is made by the agent's
            # post-call autonomous review in the system prompt.

    event_bus.subscribe("call.incoming", on_call_incoming)
    event_bus.subscribe("call.ended", on_call_ended)

    # TTS engine — warmed up at startup so the first SAY is fast
    try:
        from callen.tts import get_tts_engine
        tts_engine = get_tts_engine(config.tts)
        log.info("TTS engine ready: %s", tts_engine.name)
    except Exception:
        log.exception("TTS engine failed to load — calls will have no audio")

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
    api._db = db

    # Outbound module (technician-first bridging)
    outbound.configure(
        cmd_queue=cmd_queue,
        config=config,
        event_bus=event_bus,
        operator_state=operator_state,
        db=db,
        sip_account=sip_account,
        call_registry=call_registry,
    )

    sip_endpoint.start()
    # Register account on the SIP thread (all pjlib calls must happen there)
    cmd_queue.submit(sip_account.register).result(timeout=10)

    # IMAP poller for hello@ inbound mail
    imap_poller = None
    if config.email.imap_enabled:
        try:
            from callen.notify.imap_poller import IMAPPoller
            imap_poller = IMAPPoller(
                config.email, db, event_bus,
                support_phone=config.operator.support_phone,
            )
            imap_poller.start()
        except Exception:
            log.exception("Failed to start IMAP poller")

    # Start web server in its own thread with its own asyncio loop
    web_loop = asyncio.new_event_loop()
    agent_runner = AgentRunner(db=db)
    web_app = create_app(
        config.web, call_registry, operator_state, event_bus, db,
        agent_runner=agent_runner,
    )

    # --- Auto-agent: review voicemails as soon as they're transcribed ---
    def on_voicemail_transcribed(data):
        """Fire a claude headless run to review the voicemail transcript
        and update the incident with a proper subject + summary note.
        Runs autonomously so it doesn't clobber the operator's interactive
        conversation state."""
        call_id = data.get("call_id")
        if not call_id:
            return
        call = db.get_call(call_id)
        if not call or not call.get("incident_id"):
            return
        incident_id = call["incident_id"]
        caller_id = call.get("caller_id") or "unknown"
        transcript_preview = (data.get("text") or "")[:400]

        prompt = (
            f"Autonomous review: a voicemail was just received and transcribed "
            f"for {incident_id} (from {caller_id}).\n\n"
            f"Transcript preview: {transcript_preview}\n\n"
            f"Do the following using the ./tools/* commands:\n"
            f"1. Run ./tools/get-transcript --incident {incident_id} --text to see the full transcript\n"
            f"2. Decide on a clear, descriptive subject for {incident_id} that reflects what "
            f"the caller actually said, and set it via ./tools/update-incident {incident_id} --subject \"...\"\n"
            f"3. Add an internal note summarizing the voicemail in 2-3 sentences via "
            f"./tools/note-incident {incident_id} \"...\"\n"
            f"4. If the caller stated their name, set it on the contact via "
            f"./tools/update-contact (look up the contact id via ./tools/get-incident {incident_id})\n"
            f"5. If the voicemail sounds urgent, set priority high via ./tools/update-incident\n\n"
            f"Respond with one sentence describing what you changed."
        )

        async def _start():
            try:
                await agent_runner.start(
                    prompt,
                    context={"incident_id": incident_id, "auto": True, "trigger": "voicemail"},
                    autonomous=True,
                )
            except Exception:
                log.exception("Failed to start auto-agent for voicemail")

        try:
            asyncio.run_coroutine_threadsafe(_start(), web_loop)
        except Exception:
            log.exception("Failed to schedule voicemail auto-review")

    event_bus.subscribe("voicemail.transcribed", on_voicemail_transcribed)

    # --- Auto-agent: review bridged calls as soon as they complete ---
    def on_bridge_completed(data):
        incident_id = data.get("incident_id")
        if not incident_id:
            return

        # Give the transcription worker threads a moment to flush their
        # final segments to the DB before the agent reads them.
        import threading
        def _wait_and_fire():
            import time as _time
            _time.sleep(2.0)
            prompt = (
                f"Autonomous review: a bridged phone call just finished on {incident_id}.\n\n"
                f"Do the following using the ./tools/* commands:\n"
                f"1. ./tools/get-transcript --incident {incident_id} --text to read what was said\n"
                f"2. Update the incident subject via ./tools/update-incident to accurately describe "
                f"   the issue the caller raised, based on the actual conversation\n"
                f"3. Add a concise summary note (2-4 sentences) via ./tools/note-incident covering:\n"
                f"   - who the caller is and what they want\n"
                f"   - what the technician agreed to do\n"
                f"   - any key details (addresses, times, model numbers, etc.)\n"
                f"4. For every concrete action item the technician agreed to, add a todo\n"
                f"   via ./tools/add-todo {incident_id} \"...\"\n"
                f"   Example: if the tech said 'I'll come by in 15 minutes to install it',\n"
                f"   add 'Drive to <address> and install graphics card' as a todo.\n"
                f"5. If the caller gave their name in a clear self-introduction at the start,\n"
                f"   set it on the contact via ./tools/update-contact (look up the contact id\n"
                f"   via ./tools/get-incident). Do NOT update the name based on a trailing\n"
                f"   fragment — see transcript-quality rules.\n"
                f"6. If the call indicates urgency, bump priority to high.\n\n"
                f"Respond with one sentence describing what you changed and how many todos\n"
                f"you created."
            )

            async def _start():
                try:
                    await agent_runner.start(
                        prompt,
                        context={
                            "incident_id": incident_id,
                            "trigger": "call.bridge_completed",
                            "auto": True,
                        },
                        autonomous=True,
                    )
                except Exception:
                    log.exception("Failed to start auto-agent for bridged call")

            try:
                asyncio.run_coroutine_threadsafe(_start(), web_loop)
            except Exception:
                log.exception("Failed to schedule bridged-call auto-review")

        threading.Thread(
            target=_wait_and_fire,
            name=f"bridge-review-{incident_id}",
            daemon=True,
        ).start()

    event_bus.subscribe("call.bridge_completed", on_bridge_completed)

    # --- Preflight classifier (local LLM defense in depth) ---
    from callen.security.preflight import PreflightClassifier
    preflight = PreflightClassifier(
        enabled=config.preflight.enabled,
        url=config.preflight.url,
        model=config.preflight.model,
        timeout=config.preflight.timeout,
    )
    if preflight.enabled:
        log.info("Preflight email classifier: %s via %s",
                 preflight.model, preflight.url)

    # --- Auto-agent: review inbound email as soon as it's stored ---
    def on_email_received(data):
        """Triage a new inbound email with:
          1. Local LLM preflight (Mistral via Ollama) — screens for
             prompt injection / marketing / legitimacy BEFORE the
             email can reach the Claude agent.
          2. If the preflight passes, fire the autonomous Claude agent
             to do the substantive review and reply.
        """
        email_id = data.get("email_id")
        if not email_id:
            return

        try:
            em = db.get_email(email_id)
        except Exception:
            log.exception("Failed to load email %s for auto-review", email_id)
            return
        if not em:
            return

        status = em.get("status", "pending")
        if status not in ("pending", "attached"):
            log.info("Skipping auto-review for email %d (status=%s)", email_id, status)
            return

        # --- Step 1: local LLM preflight ---
        # The deterministic regex scanner already flagged obvious
        # injections before we got here (those emails have
        # status='flagged' and won't reach this point). Now we add an
        # intent-aware second layer: a small local model sees the
        # email and returns structured booleans. The Claude agent
        # only gets emails that pass this gate.
        if preflight.enabled:
            try:
                classification = preflight.classify_email(
                    from_addr=em.get("from_addr", ""),
                    subject=em.get("subject", ""),
                    body_text=em.get("body_text", ""),
                )
                verdict, reason = preflight.recommendation(classification)
                log.info(
                    "Preflight %d: verdict=%s reason=%s model=%s",
                    email_id, verdict, reason, classification.get("model", "?"),
                )

                if verdict == "flag":
                    db.set_email_status(
                        email_id, "flagged",
                        f"preflight: {reason}",
                    )
                    if em.get("incident_id"):
                        db.add_incident_entry(
                            em["incident_id"], "note", author="preflight",
                            payload={"text": f"Email {email_id} flagged by preflight: {reason}"},
                        )
                    # Same response as the regex scanner: create a
                    # warning ticket, mark contact suspect, hard-block
                    # the sender, and send the lockout notice.
                    from callen.notify.email_processor import apply_injection_response
                    from_addr_flag = em.get("from_addr", "")
                    contact_for_flag = None
                    try:
                        if from_addr_flag:
                            contact_for_flag = db.upsert_contact_by_email(from_addr_flag)
                    except Exception:
                        pass
                    apply_injection_response(
                        db, config.email,
                        email_id=email_id,
                        from_addr=from_addr_flag,
                        contact_id=contact_for_flag,
                        injection_reason=reason,
                        support_phone=config.operator.support_phone,
                        source="preflight classifier",
                    )
                    return  # Claude agent never sees it

                if verdict == "reject":
                    db.set_email_status(
                        email_id, "rejected",
                        f"preflight: {reason}",
                    )
                    return  # Auto-rejected, no agent run

                if verdict == "skip":
                    log.info("Preflight unavailable — passing email %d through", email_id)

                # verdict == "pass" or "skip" -> fall through to agent run
            except Exception:
                log.exception("Preflight classifier errored — flagging defensively")
                db.set_email_status(email_id, "flagged", "preflight error")
                return

        incident_id = em.get("incident_id")
        from_addr = em.get("from_addr", "")
        subject = em.get("subject", "")

        if incident_id:
            # Threaded reply — review the thread, decide next action
            prompt = (
                f"Autonomous review: a new email arrived on thread {incident_id}.\n\n"
                f"Email {email_id}: from {from_addr}, subject '{subject}'.\n\n"
                f"Apply the email handling rules from the system prompt:\n"
                f"1. Read the full incident thread with ./tools/get-incident {incident_id}\n"
                f"2. Read this specific email with ./tools/get-email {email_id}\n"
                f"3. Check the contact's email consent via the get-incident output\n"
                f"4. Decide the next action:\n"
                f"   - If the reply contains affirmative consent, record it via\n"
                f"     ./tools/contact-consent and then proceed\n"
                f"   - If it's a clarifying answer to a previous question, update\n"
                f"     the incident (subject, notes, todos) accordingly\n"
                f"   - If it introduces a new topic, update the subject/notes\n"
                f"   - If it's vague, send a clarifying reply via\n"
                f"     ./tools/send-email {incident_id}\n"
                f"   - Create todos ONLY when there's enough info for a concrete\n"
                f"     human-actionable item\n"
                f"5. Never include sensitive information in outgoing replies\n\n"
                f"Respond with one sentence describing what you changed."
            )
        else:
            # New unthreaded email — decide if it's worth processing
            prompt = (
                f"Autonomous triage: a new inbound email arrived (#{email_id}) from\n"
                f"{from_addr}, subject '{subject}'.\n\n"
                f"Follow the email handling rules:\n"
                f"1. Read the full email body with ./tools/get-email {email_id}\n"
                f"2. If it's an OTP/verification code, password reset, login code,\n"
                f"   receipt, newsletter, shipping notice, or similar automated/\n"
                f"   transactional email, reject it immediately via\n"
                f"   ./tools/reject-email {email_id} --reason \"...\"\n"
                f"3. If it looks like a legitimate support request, create an\n"
                f"   incident for it via ./tools/assign-email {email_id}\n"
                f"   --create-incident --subject \"short description\"\n"
                f"4. Check the contact's consent state. If they have not consented\n"
                f"   via email yet, send a consent-request reply explaining this\n"
                f"   is a recorded community support service and ask them to\n"
                f"   reply with 'I consent'. Do NOT answer their technical\n"
                f"   question yet and do NOT create a human todo until consent\n"
                f"   is on file.\n"
                f"5. If consent is already on file and the request is concrete,\n"
                f"   add todos via ./tools/add-todo. If it's vague, send a\n"
                f"   clarifying reply.\n"
                f"6. Never include sensitive info in outgoing email.\n\n"
                f"Respond with one sentence describing what you did."
            )

        async def _start():
            try:
                await agent_runner.start(
                    prompt,
                    context={
                        "email_id": email_id,
                        "incident_id": incident_id,
                        "trigger": "email.received",
                        "auto": True,
                    },
                    autonomous=True,
                )
            except Exception:
                log.exception("Failed to start auto-agent for email")

        try:
            asyncio.run_coroutine_threadsafe(_start(), web_loop)
        except Exception:
            log.exception("Failed to schedule email auto-review")

    event_bus.subscribe("email.received", on_email_received)

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
