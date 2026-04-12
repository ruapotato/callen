# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
TranscriptionStream — per-channel audio-to-text pipeline with VAD segmentation.

Receives raw 8kHz PCM bytes from an AudioTap. Uses webrtcvad to detect speech
boundaries and emits utterances at natural pauses, with a hard cap so no
single utterance exceeds max_utterance_seconds. Each emitted utterance is
resampled to 16kHz and transcribed by Parakeet.

Pipeline (per channel):
  PCM frames (8kHz, 20ms) -> VAD classify -> utterance accumulator
                                                |
                              [silence threshold] OR [max length] -> emit
                                                |
                              resample 8k->16k -> Parakeet -> EventBus
"""

import logging
import queue
import threading
import time

import numpy as np
import webrtcvad

from callen.audio.resampler import AudioResampler
from callen.transcription.parakeet import ParakeetProcessor

log = logging.getLogger(__name__)

# Audio constants — pjsua2 AudioMediaPort delivers 8kHz/16-bit/mono frames
SAMPLE_RATE = 8000
BYTES_PER_SAMPLE = 2
FRAME_MS = 20  # webrtcvad accepts 10/20/30 ms frames
FRAME_BYTES = SAMPLE_RATE * BYTES_PER_SAMPLE * FRAME_MS // 1000  # 320 bytes


class TranscriptionStream:
    """
    One stream per audio channel (caller or technician).

    AudioTap calls feed_audio() with raw PCM bytes from the SIP thread.
    A worker thread runs VAD on every 20ms frame, accumulates voiced
    audio into utterances, and submits them to Parakeet at natural pauses
    (or when the hard length cap is reached).
    """

    def __init__(
        self,
        label: str,
        call_id: str,
        call_start_time: float,
        processor: ParakeetProcessor,
        chunk_seconds: float = 3.0,  # kept for compatibility, not used
        on_transcript=None,
        # VAD parameters
        vad_aggressiveness: int = 2,        # 0-3, higher = stricter speech
        silence_ms: int = 700,              # silence to consider an utterance done
        min_utterance_ms: int = 500,        # ignore very short bursts
        max_utterance_seconds: float = 15.0,  # hard cut even if still speaking
        leading_padding_ms: int = 200,      # bit of audio before first voiced frame
    ):
        self.label = label
        self.call_id = call_id
        self._call_start = call_start_time
        self._processor = processor
        self._on_transcript = on_transcript

        self._resampler = AudioResampler(input_rate=SAMPLE_RATE, output_rate=16000)
        self._vad = webrtcvad.Vad(vad_aggressiveness)

        self._silence_frames = max(1, silence_ms // FRAME_MS)
        self._min_utt_frames = max(1, min_utterance_ms // FRAME_MS)
        self._max_utt_frames = max(1, int(max_utterance_seconds * 1000 // FRAME_MS))
        self._padding_frames = max(0, leading_padding_ms // FRAME_MS)

        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=500)
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
        log.info("Transcription stream started: %s for call %s",
                 self.label, self.call_id[:8])

    def feed_audio(self, pcm_bytes: bytes):
        """Called from AudioTap callback (SIP thread). Must not block."""
        if self._running and pcm_bytes:
            try:
                self._queue.put_nowait(pcm_bytes)
            except queue.Full:
                pass

    def stop(self):
        self._running = False
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Transcription stream stopped: %s", self.label)

    # --- Worker pipeline ---

    def _worker(self):
        """Pull bytes from queue, slice into VAD frames, emit utterances."""
        # Bytes that didn't fill a complete frame last iteration
        leftover = b""

        # Pre-utterance ring buffer (so we capture audio just before speech starts)
        pre_buffer: list[bytes] = []

        # Active utterance state
        in_speech = False
        utt_frames: list[bytes] = []
        silence_run = 0
        utt_start_offset = 0.0

        while self._running:
            try:
                pcm = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if pcm is None:
                break

            data = leftover + pcm
            # Slice into 20ms frames
            n_frames = len(data) // FRAME_BYTES
            if n_frames == 0:
                leftover = data
                continue
            leftover = data[n_frames * FRAME_BYTES:]

            for i in range(n_frames):
                frame = data[i * FRAME_BYTES:(i + 1) * FRAME_BYTES]

                try:
                    is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
                except Exception:
                    is_speech = False

                if not in_speech:
                    # Maintain pre-roll
                    pre_buffer.append(frame)
                    if len(pre_buffer) > self._padding_frames:
                        pre_buffer.pop(0)

                    if is_speech:
                        in_speech = True
                        utt_frames = list(pre_buffer)
                        utt_frames.append(frame)
                        pre_buffer = []
                        silence_run = 0
                        utt_start_offset = time.time() - self._call_start
                else:
                    utt_frames.append(frame)
                    if is_speech:
                        silence_run = 0
                    else:
                        silence_run += 1

                    # Emit on natural pause
                    if silence_run >= self._silence_frames:
                        if len(utt_frames) >= self._min_utt_frames:
                            self._emit_utterance(utt_frames, utt_start_offset)
                        in_speech = False
                        utt_frames = []
                        silence_run = 0
                        continue

                    # Hard cap — force a cut even mid-speech
                    if len(utt_frames) >= self._max_utt_frames:
                        self._emit_utterance(utt_frames, utt_start_offset)
                        in_speech = False
                        utt_frames = []
                        silence_run = 0

        # Drain any remaining utterance on shutdown
        if in_speech and len(utt_frames) >= self._min_utt_frames:
            self._emit_utterance(utt_frames, utt_start_offset)

    def _emit_utterance(self, frames: list[bytes], offset: float):
        """Resample and submit one utterance to Parakeet.

        Applies an RMS energy gate before transcription — Parakeet
        cheerfully hallucinates "okay", "yeah", "um" on near-silence
        that barely tripped VAD, so we additionally require the
        utterance to have real acoustic energy.
        """
        pcm = b"".join(frames)
        try:
            samples_16k = self._resampler.process(pcm)
        except Exception:
            log.exception("[%s] resample failed", self.label)
            return

        if samples_16k.size == 0:
            return

        # Silence / low-energy gate. float32 PCM is in [-1, 1], so an
        # RMS under ~0.005 is effectively silent speech. Also require a
        # minimum peak so brief clicks/pops don't become "yeah".
        rms = float(np.sqrt(np.mean(samples_16k * samples_16k)))
        peak = float(np.max(np.abs(samples_16k)))
        if rms < 0.005 or peak < 0.03:
            log.debug("[%s @%.1fs] gated silence (rms=%.4f peak=%.4f)",
                      self.label, offset, rms, peak)
            return

        try:
            text = self._processor.transcribe_sync(samples_16k)
        except Exception:
            log.exception("[%s] transcription failed", self.label)
            return

        if not text:
            return

        log.info("[%s @%.1fs] %s", self.label, offset, text)

        if self._on_transcript:
            self._on_transcript({
                "call_id": self.call_id,
                "speaker": self.label,
                "text": text,
                "timestamp_offset": round(offset, 2),
            })
