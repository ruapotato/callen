# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
TranscriptionStream — per-channel audio-to-text pipeline.

Receives raw PCM bytes from an AudioTap, resamples, buffers into chunks,
and submits to ParakeetProcessor for transcription.
"""

import logging
import queue
import threading
import time

import numpy as np

from callen.audio.resampler import AudioResampler
from callen.audio.buffer import AudioChunkBuffer
from callen.transcription.parakeet import ParakeetProcessor

log = logging.getLogger(__name__)


class TranscriptionStream:
    """
    One stream per audio channel (caller or technician).

    AudioTap calls feed_audio() with raw PCM from the SIP thread.
    A worker thread resamples, buffers, and transcribes chunks.
    Results are delivered via the on_transcript callback.
    """

    def __init__(
        self,
        label: str,
        call_id: str,
        call_start_time: float,
        processor: ParakeetProcessor,
        chunk_seconds: float = 3.0,
        on_transcript=None,
    ):
        self.label = label
        self.call_id = call_id
        self._call_start = call_start_time
        self._processor = processor
        self._on_transcript = on_transcript

        self._resampler = AudioResampler(input_rate=8000, output_rate=16000)
        self._buffer = AudioChunkBuffer(chunk_seconds=chunk_seconds, sample_rate=16000)
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=200)
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._worker,
            name=f"stt-{self.label}-{self.call_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        log.info("Transcription stream started: %s for call %s", self.label, self.call_id[:8])

    def feed_audio(self, pcm_bytes: bytes):
        """Called from AudioTap callback (SIP thread). Must not block."""
        if self._running:
            try:
                self._queue.put_nowait(pcm_bytes)
            except queue.Full:
                pass  # Drop frames if worker can't keep up

    def stop(self):
        self._running = False
        self._queue.put(None)  # Sentinel to wake worker
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Transcription stream stopped: %s", self.label)

    def _worker(self):
        """Worker thread: resample, buffer, transcribe."""
        while self._running:
            try:
                pcm = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if pcm is None:
                break

            # Resample 8kHz → 16kHz float32
            resampled = self._resampler.process(pcm)

            # Buffer until chunk is full
            chunk = self._buffer.append(resampled)
            if chunk is not None:
                self._transcribe_chunk(chunk)

        # Flush remaining buffer
        remainder = self._buffer.flush()
        if remainder is not None and len(remainder) > 0:
            self._transcribe_chunk(remainder)

    def _transcribe_chunk(self, chunk: np.ndarray):
        timestamp = time.time() - self._call_start
        text = self._processor.transcribe_sync(chunk)

        if text and self._on_transcript:
            self._on_transcript({
                "call_id": self.call_id,
                "speaker": self.label,
                "text": text,
                "timestamp_offset": round(timestamp, 2),
            })
