#!/usr/bin/python3

# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

# This file defines the IVR call flow.
# Edit this file to customize how Callen handles incoming calls.
#
# Available functions (injected by the engine, no imports needed):
#   say(call, text, repeat=True)       - TTS to caller (loops if repeat=True)
#   play(call, wav_path)               - Play pre-recorded audio
#   dtmf(call, count=1, timeout=10)    - Wait for DTMF, returns str or None
#   bridge_to_operator(call)           - Connect caller to operator's phone
#   record_voicemail(call, prompt=None) - Record voicemail + email notification
#   hangup(call)                       - End the call
#   caller_id(call)                    - Get caller's phone number
#   operator_available()               - Check if operator is available
#   has_consented(call)                - True if this number already consented
#   has_website(call)                  - True if caller has a managed site
#   get_website_url(call)              - Returns the caller's site URL
#   log_event(call, event_type, detail) - Log an IVR flow event for analytics


def IVR(call):
    log_event(call, "incoming", caller_id(call))

    # Hard block: quarantined numbers get no interaction at all.
    if is_blocked(call):
        log_event(call, "blocked", caller_id(call))
        hangup(call)
        return

    # Recording consent: required before anything else.
    # Returning callers who already agreed skip the gate but hear a reminder.
    if has_consented(call):
        log_event(call, "consent_skipped", "returning caller")
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
        log_event(call, "consent_prompted")
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
            log_event(call, "hangup_during_consent", f"key={consent}")
            say(call, "No consent received. Goodbye.", repeat=False)
            hangup(call)
            return

        log_event(call, "consent_granted")
        call.consented_to_recording = True

    # Main menu: dynamically include option 3 if caller has a website.
    caller_has_site = has_website(call)

    if caller_has_site:
        log_event(call, "menu_played", "with_website_option")
        say(call, (
            "Press 1 to speak with a technician. "
            "Press 2 to leave a voicemail. "
            "Press 3 to request changes to your website."
        ))
    else:
        log_event(call, "menu_played", "standard")
        say(call, (
            "Press 1 to speak with a technician. "
            "Press 2 to leave a voicemail."
        ))

    while True:
        key = dtmf(call, count=1, timeout=10)

        if key == '1':
            log_event(call, "dtmf_1_technician")
            bridge_to_operator(call)
            return
        elif key == '2':
            log_event(call, "dtmf_2_voicemail")
            record_voicemail(call)
            return
        elif key == '3' and caller_has_site:
            log_event(call, "dtmf_3_website")
            _website_update_flow(call)
            return
        elif key is None:
            log_event(call, "hangup_during_menu")
            return
        else:
            say(call, "Invalid option.", repeat=False)
            if caller_has_site:
                say(call, "Press 1 for a technician, 2 for voicemail, or 3 for website changes.")
            else:
                say(call, "Press 1 for a technician, or 2 for voicemail.")


def _website_update_flow(call):
    """Record verbal website change instructions as a voicemail."""
    say(call, (
        "Please describe the changes you would like made to your website "
        "after the beep. Include as much detail as you can. "
        "For example: update my phone number, change the hours, "
        "or add a new section. "
        "If you would like to add or change images, email them to us at "
        "hello at free software dot support. "
        "When you are finished, press pound. "
        "We will get those changes implemented and send you an email "
        "when everything is updated."
    ), repeat=False)
    record_voicemail(
        call,
        prompt="Go ahead and describe your website changes now.",
    )
