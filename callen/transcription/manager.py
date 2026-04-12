# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
TranscriptionManager — creates and manages stream pairs for bridged calls.
"""

import logging

from callen.transcription.parakeet import ParakeetProcessor
from callen.transcription.stream import TranscriptionStream
from callen.state.events import EventBus

log = logging.getLogger(__name__)


class TranscriptionManager:
    def __init__(self, processor: ParakeetProcessor, event_bus: EventBus,
                 chunk_seconds: float = 3.0):
        self._processor = processor
        self._event_bus = event_bus
        self._chunk_seconds = chunk_seconds
        self._streams: dict[str, tuple[TranscriptionStream, TranscriptionStream]] = {}

    def _on_transcript(self, data: dict):
        """Called by TranscriptionStream when a segment is transcribed."""
        self._event_bus.publish("transcript.update", data)

    def start_for_call(self, call_id: str, call_start_time: float, **_kwargs):
        """
        Create and start transcription streams for both channels.

        Returns (caller_feed, tech_feed) — callables that accept PCM bytes,
        suitable for use as AudioTap callbacks.
        """
        caller_stream = TranscriptionStream(
            label="caller",
            call_id=call_id,
            call_start_time=call_start_time,
            processor=self._processor,
            chunk_seconds=self._chunk_seconds,
            on_transcript=self._on_transcript,
        )
        tech_stream = TranscriptionStream(
            label="technician",
            call_id=call_id,
            call_start_time=call_start_time,
            processor=self._processor,
            chunk_seconds=self._chunk_seconds,
            on_transcript=self._on_transcript,
        )

        caller_stream.start()
        tech_stream.start()

        self._streams[call_id] = (caller_stream, tech_stream)
        log.info("Transcription started for call %s", call_id[:8])

        return caller_stream.feed_audio, tech_stream.feed_audio

    def stop_for_call(self, call_id: str):
        streams = self._streams.pop(call_id, None)
        if streams:
            streams[0].stop()
            streams[1].stop()
            log.info("Transcription stopped for call %s", call_id[:8])
