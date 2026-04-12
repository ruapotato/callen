# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Conference bridge helpers — connect and disconnect audio legs.

pjsua2's conference bridge fans out: one source can transmit to many sinks.
During a bridged call, each leg's audio goes to:
  1. The other call (so both parties hear each other)
  2. A CallRecorder (split-channel recording)
  3. An AudioTap (for live transcription)
"""

import logging

import pjsua2 as pj

log = logging.getLogger(__name__)


def connect_calls(call_a_media: pj.AudioMedia, call_b_media: pj.AudioMedia):
    """Bidirectional audio bridge between two calls."""
    call_a_media.startTransmit(call_b_media)
    call_b_media.startTransmit(call_a_media)
    log.info("Calls bridged (bidirectional)")


def disconnect_calls(call_a_media: pj.AudioMedia, call_b_media: pj.AudioMedia):
    """Disconnect bidirectional bridge."""
    try:
        call_a_media.stopTransmit(call_b_media)
    except Exception:
        pass
    try:
        call_b_media.stopTransmit(call_a_media)
    except Exception:
        pass
    log.info("Calls unbridged")


def connect_to_recorder(source: pj.AudioMedia, recorder):
    """Route audio from source to a CallRecorder."""
    recorder.start(source)


def connect_to_tap(source: pj.AudioMedia, tap):
    """Route audio from source to an AudioTap for transcription."""
    source.startTransmit(tap)
    log.debug("Audio tap connected: %s", tap.label)


def disconnect_tap(source: pj.AudioMedia, tap):
    """Disconnect an audio tap."""
    try:
        source.stopTransmit(tap)
    except Exception:
        pass
