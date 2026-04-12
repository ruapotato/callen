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

# Set by IVREngine before spawning IVR threads
_cmd_queue: SIPCommandQueue | None = None
_operator_state = None
_event_bus = None
_config = None
_make_outbound_call = None


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
        say(call, "The operator is currently unavailable.", repeat=False)
        record_voicemail(call)
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
            say(call, "Could not reach the operator. Please leave a message.", repeat=False)
            _operator_state.auto_available()
            record_voicemail(call)
            return

        outbound_call.media_ready.wait(timeout=60)
        if outbound_call.state == CallState.DISCONNECTED:
            say(call, "The operator did not answer. Please leave a message.", repeat=False)
            _operator_state.auto_available()
            record_voicemail(call)
            return

        caller_media = call.get_audio_media()
        tech_media = outbound_call.get_audio_media()

        if caller_media and tech_media:
            _sip(br.connect_calls, caller_media, tech_media)
            log.info("Call %s bridged to operator", call.uuid[:8])

            while (call.state != CallState.DISCONNECTED and
                   outbound_call.state != CallState.DISCONNECTED):
                time.sleep(0.5)

            _sip(br.disconnect_calls, caller_media, tech_media)

            # If the operator hung up first, end the caller's leg immediately
            if (outbound_call.state == CallState.DISCONNECTED and
                    call.state != CallState.DISCONNECTED):
                log.info("Operator hung up — terminating caller leg")
                try:
                    _sip(call.hangup, pj.CallOpParam())
                except Exception:
                    pass

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


def record_voicemail(call: CallenCall):
    """Record a voicemail message from the caller."""
    from callen.sip.media import CallRecorder

    if call.state == CallState.DISCONNECTED:
        return

    say(call, "Please leave your message after the beep. Press pound when finished.", repeat=False)
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

    _event_bus.publish("voicemail.recording", {"call_id": call.uuid})

    max_dur = _config.voicemail.max_duration
    collect_dtmf(call, count=1, timeout=max_dur)

    _sip(recorder.stop)

    _event_bus.publish("voicemail.received", {
        "call_id": call.uuid,
        "caller_id": call.caller_id,
        "path": vm_path,
    })

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
