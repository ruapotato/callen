#!/usr/bin/python3

# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

# This file defines the IVR call flow.
# Edit this file to customize how Callen handles incoming calls.
#
# Available functions (injected by the engine — no imports needed):
#   say(call, text, repeat=True)       — TTS to caller (loops if repeat=True)
#   play(call, wav_path)               — Play pre-recorded audio
#   dtmf(call, count=1, timeout=10)    — Wait for DTMF, returns str or None
#   bridge_to_operator(call)           — Connect caller to operator's phone
#   record_voicemail(call, prompt=None) — Record voicemail + email notification
#   hangup(call)                       — End the call
#   caller_id(call)                    — Get caller's phone number
#   operator_available()               — Check if operator is available
#   has_consented(call)                — True if this number already consented


def IVR(call):
    # Hard block — if this number is on the quarantine list, hang up
    # immediately. No greeting, no menu, no agent exposure.
    if is_blocked(call):
        hangup(call)
        return

    # Recording consent — required before anything else.
    # Returning callers who already agreed on a prior call skip the gate
    # but are reminded that the call is still being recorded AND that the
    # liability disclaimer still applies.
    if has_consented(call):
        say(call, (
            "Welcome back to free software support. "
            "This is a recorded call. "
            "We have marked that you previously consented to being recorded. "
            "Please remember: we are not liable for any damage to equipment "
            "or loss of data during this support session. "
            "If you do not agree, please hang up now."
        ), repeat=False)
        call.consented_to_recording = True
    else:
        say(call, (
            "Welcome to free software support. "
            "This call is recorded and may be published as educational content. "
            "By continuing, you acknowledge that free software support and its "
            "technicians are not liable for any damage to equipment or loss of "
            "data that may occur during this support session. "
            "Press 1 to consent and continue, or hang up now."
        ))

        consent = dtmf(call, count=1, timeout=20)
        if consent != '1':
            say(call, "No consent received. Goodbye.", repeat=False)
            hangup(call)
            return

        call.consented_to_recording = True

    # Main menu
    say(call, (
        "Press 1 to speak with a technician. "
        "Press 2 to leave a voicemail."
    ))

    while True:
        key = dtmf(call, count=1, timeout=10)

        if key == '1':
            bridge_to_operator(call)
            return
        elif key == '2':
            record_voicemail(call)
            return
        elif key is None:
            return
        else:
            say(call, "Invalid option.", repeat=False)
            say(call, "Press 1 for a technician, or 2 for voicemail.")
