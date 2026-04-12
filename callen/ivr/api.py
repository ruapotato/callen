# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
IVR scripting API — clean functions injected into IVR scripts.
Now with real audio playback via AudioMediaPlayer (SWIG pjsua2).
"""

import logging
import os
import time
from datetime import datetime

import pjsua2 as pj

from callen.sip.call import CallenCall, CallState
from callen.sip.commands import SIPCommandQueue
from callen.sip.dtmf import collect_dtmf

log = logging.getLogger(__name__)

# Set by IVREngine / app before spawning IVR threads
_cmd_queue: SIPCommandQueue | None = None
_operator_state = None
_event_bus = None
_config = None
_db = None
_make_outbound_call = None
_transcription_mgr = None
_active_taps: dict[str, list] = {}  # call_id -> [caller_tap, tech_tap]

BUSY_VOICEMAIL_PROMPT = (
    "Sorry, all technicians are currently busy. "
    "Please leave your name, your phone number, and a brief message after the beep. "
    "We will get back to you as soon as possible. "
    "Press pound when you are finished."
)

NO_ANSWER_VOICEMAIL_PROMPT = (
    "Sorry, the technician is not available right now. "
    "Please leave your name, your phone number, and a brief message after the beep. "
    "We will get back to you as soon as possible. "
    "Press pound when you are finished."
)

# Hang up the outbound leg if the operator hasn't answered within this many
# seconds. Tuned to fire BEFORE typical cell carrier voicemail (20-25s) so
# the caller lands in Callen's voicemail instead of the cell carrier's.
RING_TIMEOUT = 18.0


def _sip(fn, *args, **kwargs):
    """Submit to SIP thread and block for result."""
    return _cmd_queue.submit(fn, *args, **kwargs).result(timeout=30)


def say(call: CallenCall, text: str, repeat: bool = True):
    """
    Speak text to the caller via TTS. Plays audio into the call.

    Stops immediately when the caller presses a DTMF key — keys are NOT
    consumed (they remain in the queue for dtmf() to read).
    """
    from callen.sip.media import generate_tts_wav, PromptPlayer

    if call.state == CallState.DISCONNECTED:
        return

    log.info("SAY [%s]: %s", call.uuid[:8], text)
    wav_path = generate_tts_wav(text)

    try:
        file_size = os.path.getsize(wav_path)
        duration = max(1.0, (file_size - 44) / 16000.0)
    except OSError:
        duration = 2.0

    try:
        while True:
            if call.state == CallState.DISCONNECTED:
                break

            # Stop if there's already a pending DTMF (caller pressed key during gap)
            if not call.dtmf_queue.empty():
                break

            media = call.get_audio_media()
            if not media:
                log.warning("No audio media for say()")
                break

            player = _sip(PromptPlayer)
            _sip(player.play, wav_path, media)

            # Poll for DTMF or playback completion every 50ms
            playback_end = time.time() + duration + 0.2
            interrupted = False
            while time.time() < playback_end:
                if call.state == CallState.DISCONNECTED:
                    interrupted = True
                    break
                if not call.dtmf_queue.empty():
                    interrupted = True
                    break
                time.sleep(0.05)

            _sip(player.stop)

            if interrupted or not repeat:
                break

            # Brief gap before repeat, but break early on DTMF
            gap_end = time.time() + 1.5
            while time.time() < gap_end:
                if call.state == CallState.DISCONNECTED:
                    break
                if not call.dtmf_queue.empty():
                    break
                time.sleep(0.05)
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass


def play(call: CallenCall, wav_path: str):
    """Play a pre-recorded WAV file to the caller."""
    from callen.sip.media import PromptPlayer

    if call.state == CallState.DISCONNECTED:
        return

    media = call.get_audio_media()
    if not media:
        return

    player = _sip(PromptPlayer)
    _sip(player.play, wav_path, media)

    try:
        file_size = os.path.getsize(wav_path)
        duration = max(1.0, (file_size - 44) / 16000.0)
    except OSError:
        duration = 2.0

    time.sleep(duration + 0.3)
    _sip(player.stop)


def dtmf(call: CallenCall, count: int = 1, timeout: float = 10.0) -> str | None:
    """Wait for DTMF digits. Returns digit string or None on timeout."""
    return collect_dtmf(call, count=count, timeout=timeout)


def bridge_to_operator(call: CallenCall):
    """Bridge the caller to the operator's cell phone."""
    from callen.sip import bridge as br

    if not _operator_state.is_available:
        record_voicemail(call, prompt=BUSY_VOICEMAIL_PROMPT)
        return

    say(call, "Connecting you now. Please hold.", repeat=False)

    _operator_state.auto_busy()
    call.state = CallState.BRIDGED
    _event_bus.publish("call.bridged", {"call_id": call.uuid})

    outbound_call = None
    try:
        # Strip leading + for VoIP.ms — they want bare E.164 digits
        cell = _config.operator.cell_phone.lstrip("+")
        dst_uri = f"sip:{cell}@{_config.sip.domain}"
        log.info("Bridging %s to operator at %s", call.uuid[:8], dst_uri)

        outbound_call = _sip(_make_outbound_call, call, dst_uri)

        if outbound_call is None:
            _operator_state.auto_available()
            record_voicemail(call, prompt=BUSY_VOICEMAIL_PROMPT)
            return

        # Wait for the operator to actually pick up — CallState.ACTIVE means
        # the call reached CONFIRMED (200 OK from the answering UA).
        # Use a tight timeout (RING_TIMEOUT) so we hang up before the cell
        # carrier's voicemail picks up — we want callers in OUR voicemail,
        # not on the operator's cell carrier voicemail.
        ring_deadline = time.time() + RING_TIMEOUT
        while time.time() < ring_deadline:
            if outbound_call.state == CallState.ACTIVE:
                break
            if outbound_call.state == CallState.DISCONNECTED:
                break
            if call.state == CallState.DISCONNECTED:
                break
            time.sleep(0.2)

        if outbound_call.state != CallState.ACTIVE:
            log.info("Operator did not answer within %ds — routing to voicemail",
                     RING_TIMEOUT)
            try:
                _sip(outbound_call.hangup, pj.CallOpParam())
            except Exception:
                pass
            _operator_state.auto_available()
            if call.state != CallState.DISCONNECTED:
                record_voicemail(call, prompt=NO_ANSWER_VOICEMAIL_PROMPT)
            return

        caller_media = call.get_audio_media()
        tech_media = outbound_call.get_audio_media()

        if caller_media and tech_media:
            _sip(br.connect_calls, caller_media, tech_media)
            log.info("Call %s bridged to operator", call.uuid[:8])

            # Split-channel recording
            _start_recording(call, caller_media, tech_media)

            # Start live transcription on both legs
            _start_transcription(call, caller_media, tech_media)

            while (call.state != CallState.DISCONNECTED and
                   outbound_call.state != CallState.DISCONNECTED):
                time.sleep(0.5)

            _stop_recording(call)
            _stop_transcription(call, caller_media, tech_media)
            _sip(br.disconnect_calls, caller_media, tech_media)

            # Whichever leg dropped first, terminate the other one immediately
            if (outbound_call.state == CallState.DISCONNECTED and
                    call.state != CallState.DISCONNECTED):
                log.info("Operator hung up — terminating caller leg")
                try:
                    _sip(call.hangup, pj.CallOpParam())
                except Exception:
                    pass
            elif (call.state == CallState.DISCONNECTED and
                    outbound_call.state != CallState.DISCONNECTED):
                log.info("Caller hung up — terminating operator leg")
                try:
                    _sip(outbound_call.hangup, pj.CallOpParam())
                except Exception:
                    pass

            # Signal that a bridged call just finished. The app-level
            # subscriber kicks off an autonomous agent review of the
            # transcript so the ticket gets subject/todos/notes updated.
            _event_bus.publish("call.bridge_completed", {
                "incident_id": getattr(call, "incident_id", None),
                "call_id": call.uuid,
                "caller_id": call.caller_id,
            })

    except Exception:
        log.exception("Bridge error for call %s", call.uuid[:8])
    finally:
        _operator_state.auto_available()
        if outbound_call and outbound_call.state != CallState.DISCONNECTED:
            try:
                _sip(outbound_call.hangup, pj.CallOpParam())
            except Exception:
                pass
        _event_bus.publish("call.ended", {"call_id": call.uuid})


def record_voicemail(call: CallenCall, prompt: str | None = None):
    """Record a voicemail message from the caller.

    If prompt is given, that text is spoken instead of the default. Useful for
    different contexts (busy vs caller-chosen voicemail).
    """
    from callen.sip.media import CallRecorder

    if call.state == CallState.DISCONNECTED:
        return

    if prompt is None:
        prompt = "Please leave your message after the beep. Press pound when finished."
    say(call, prompt, repeat=False)
    say(call, "Beep!", repeat=False)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    vm_dir = _config.voicemail.directory
    os.makedirs(vm_dir, exist_ok=True)
    vm_path = os.path.join(vm_dir, f"{timestamp}_{call.caller_id}.wav")

    media = call.get_audio_media()
    if not media:
        hangup(call)
        return

    recorder = _sip(CallRecorder, vm_path)
    _sip(recorder.start, media)

    # Stash on the call object so the DB save picks it up
    call.voicemail_path = vm_path

    _event_bus.publish("voicemail.recording", {"call_id": call.uuid})

    max_dur = _config.voicemail.max_duration
    collect_dtmf(call, count=1, timeout=max_dur)

    _sip(recorder.stop)

    _event_bus.publish("voicemail.received", {
        "call_id": call.uuid,
        "caller_id": call.caller_id,
        "path": vm_path,
    })

    # Kick off post-transcription in the background — we don't tap the
    # voicemail audio live (it's a single-channel recording), so we read
    # the WAV off disk after the fact and feed it through Parakeet.
    # This only runs if the transcription manager was set up at startup.
    if _transcription_mgr is not None:
        try:
            from callen.transcription.post import transcribe_voicemail
            from callen.storage.db import Database  # noqa: F401 (type hint only)

            transcribe_voicemail(
                wav_path=vm_path,
                call_id=call.uuid,
                processor=_transcription_mgr._processor,
                db=_db,
                event_bus=_event_bus,
                speaker="caller",
            )
        except Exception:
            log.exception("Failed to kick off voicemail transcription")

    say(call, "Thank you. Goodbye.", repeat=False)
    hangup(call)


def hangup(call: CallenCall):
    if call.state != CallState.DISCONNECTED:
        try:
            _sip(call.hangup, pj.CallOpParam())
        except Exception:
            pass
    _event_bus.publish("call.ended", {"call_id": call.uuid})


def caller_id(call: CallenCall) -> str:
    return call.caller_id


def operator_available() -> bool:
    return _operator_state.is_available


def has_consented(call: CallenCall) -> bool:
    """True if this caller's phone number has already consented to recording
    on a previous call. Lets the IVR script skip the consent gate for
    returning callers who already agreed once."""
    return bool(getattr(call, "prior_consent", False))


# --- Internal helpers (not exposed to IVR scripts) ---

_active_recorders: dict[str, list] = {}


def _start_recording(call, caller_media, tech_media):
    """Start split-channel recording for a bridged call.

    Creates two AudioMediaRecorder instances on the SIP thread: one listening
    to the caller's leg, one listening to the technician's. The conference
    bridge fans out each source to both the other leg and its recorder.
    """
    from callen.sip.media import CallRecorder

    if not _config.recording.enabled:
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rec_dir = _config.recording.directory
    os.makedirs(rec_dir, exist_ok=True)

    caller_path = os.path.join(
        rec_dir, f"{timestamp}_{call.caller_id}_caller.wav"
    )
    tech_path = os.path.join(
        rec_dir, f"{timestamp}_{call.caller_id}_tech.wav"
    )

    try:
        caller_rec = _sip(CallRecorder, caller_path)
        tech_rec = _sip(CallRecorder, tech_path)
        _sip(caller_rec.start, caller_media)
        _sip(tech_rec.start, tech_media)
        _active_recorders[call.uuid] = [caller_rec, tech_rec]
        call.caller_recording_path = caller_path
        call.tech_recording_path = tech_path
        log.info("Split-channel recording started: %s + %s", caller_path, tech_path)
    except Exception:
        log.exception("Failed to start recording for %s", call.uuid[:8])


def _stop_recording(call):
    """Stop the split-channel recorders for this call."""
    recs = _active_recorders.pop(call.uuid, None)
    if not recs:
        return
    for rec in recs:
        try:
            _sip(rec.stop)
        except Exception:
            pass


def _start_transcription(call, caller_media, tech_media):
    """Set up AudioTaps + transcription streams for a bridged call."""
    if _transcription_mgr is None:
        return

    from callen.sip.media import AudioTap
    from callen.sip import bridge as br

    # Create the transcription streams first — they return audio feed callbacks
    caller_feed, tech_feed = _transcription_mgr.start_for_call(
        call_id=call.uuid,
        call_start_time=call.answered_at or call.started_at,
    )

    # Create AudioTaps on the SIP thread (pjsua2 objects must be made there)
    def _make_taps():
        caller_tap = AudioTap("caller", caller_feed)
        tech_tap = AudioTap("technician", tech_feed)
        # Wire taps into the conference bridge — fan-out from each leg
        br.connect_to_tap(caller_media, caller_tap)
        br.connect_to_tap(tech_media, tech_tap)
        return [caller_tap, tech_tap]

    try:
        taps = _sip(_make_taps)
        _active_taps[call.uuid] = taps
        log.info("Live transcription started for call %s", call.uuid[:8])
    except Exception:
        log.exception("Failed to start transcription for %s", call.uuid[:8])


def _stop_transcription(call, caller_media, tech_media):
    """Tear down transcription streams and AudioTaps."""
    if _transcription_mgr is None:
        return

    from callen.sip import bridge as br

    taps = _active_taps.pop(call.uuid, [])
    if taps:
        def _disconnect():
            try: br.disconnect_tap(caller_media, taps[0])
            except Exception: pass
            try: br.disconnect_tap(tech_media, taps[1])
            except Exception: pass
        try:
            _sip(_disconnect)
        except Exception:
            pass

    try:
        _transcription_mgr.stop_for_call(call.uuid)
    except Exception:
        log.exception("Error stopping transcription for %s", call.uuid[:8])
