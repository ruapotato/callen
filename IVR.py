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
#   record_voicemail(call)             — Record voicemail + email notification
#   hangup(call)                       — End the call
#   caller_id(call)                    — Get caller's phone number
#   operator_available()               — Check if operator is available


def IVR(call):
    # Recording consent — required before anything else.
    # Calls to freesoftware.support are recorded and may be published.
    say(call, (
        "Welcome to free software dot support. "
        "This call is recorded and may be published as educational content. "
        "Press 1 to consent and continue, or hang up now."
    ))

    consent = dtmf(call, count=1, timeout=15)

    if consent != '1':
        say(call, "No consent received. Goodbye.", repeat=False)
        hangup(call)
        return

    # Mark consent on the call object
    call.consented_to_recording = True

    # Main menu
    say(call, (
        "Thank you. "
        "Press 1 to speak with a technician. "
        "Press 2 to leave a voicemail."
    ))

    while True:
        key = dtmf(call, count=1, timeout=10)

        if key == '1':
            # bridge_to_operator() handles the busy-routing-to-voicemail itself,
            # so we just hand the call off and exit when it returns.
            bridge_to_operator(call)
            return
        elif key == '2':
            record_voicemail(call)
            return
        elif key is None:
            # Timeout or disconnect — end the call
            return
        else:
            say(call, "Invalid option.", repeat=False)
            say(call, "Press 1 for a technician, or 2 for voicemail.")
