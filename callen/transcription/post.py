# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Post-call transcription — for calls where we don't tap the live audio
(e.g. voicemail). Reads a WAV from disk, resamples to 16 kHz, chunks via
VAD, sends each utterance to Parakeet, and writes transcript_segments
rows linked to the call record.

Runs in a background thread so it never blocks the SIP poll loop.
"""

import logging
import threading
import time
import wave

import numpy as np

try:
    import soundfile as sf
except ImportError:
    sf = None

try:
    import webrtcvad
except ImportError:
    webrtcvad = None

from callen.audio.resampler import AudioResampler
from callen.transcription.parakeet import ParakeetProcessor

log = logging.getLogger(__name__)


VAD_FRAME_MS = 20
SILENCE_MS = 700
MIN_UTTERANCE_MS = 500
MAX_UTTERANCE_SECONDS = 15.0


def _load_wav_once(path: str) -> tuple[np.ndarray, int]:
    """One attempt at loading a WAV. Raises or returns (samples, rate)."""
    if sf is not None:
        data, rate = sf.read(path, dtype="int16")
        if data.ndim > 1:
            data = data[:, 0]
        return data, rate
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16)
        if w.getnchannels() > 1:
            samples = samples.reshape(-1, w.getnchannels())[:, 0]
        return samples, rate


def _load_wav(path: str, max_retries: int = 10, retry_delay: float = 0.3) -> tuple[np.ndarray, int]:
    """Load a WAV with retries — pjsua2 finalizes the file header
    asynchronously after recorder.stop(), so a fresh voicemail can look
    empty for a fraction of a second before being fully readable."""
    last_err = None
    for attempt in range(max_retries):
        try:
            samples, rate = _load_wav_once(path)
            if samples.size > 0:
                return samples, rate
            last_err = "zero samples"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < max_retries - 1:
            time.sleep(retry_delay)
            log.debug("WAV load retry %d/%d for %s (%s)",
                      attempt + 1, max_retries, path, last_err)

    raise RuntimeError(f"Failed to load WAV after {max_retries} attempts: {last_err}")


def _vad_segments(
    samples_int16: np.ndarray,
    sample_rate: int,
) -> list[tuple[float, np.ndarray]]:
    """Slice samples_int16 into utterances at speech/silence boundaries.

    Returns a list of (offset_seconds, int16_samples) tuples.
    Requires webrtcvad at a supported rate (8/16/32 kHz). Falls back to a
    single full-length chunk if webrtcvad is unavailable.
    """
    if webrtcvad is None or sample_rate not in (8000, 16000, 32000):
        return [(0.0, samples_int16)]

    vad = webrtcvad.Vad(2)
    bytes_per_sample = 2
    frame_samples = int(sample_rate * VAD_FRAME_MS / 1000)
    frame_bytes = frame_samples * bytes_per_sample
    silence_frames = SILENCE_MS // VAD_FRAME_MS
    min_utt_frames = MIN_UTTERANCE_MS // VAD_FRAME_MS
    max_utt_frames = int(MAX_UTTERANCE_SECONDS * 1000 // VAD_FRAME_MS)

    raw = samples_int16.tobytes()
    total_frames = len(raw) // frame_bytes
    if total_frames == 0:
        return []

    segments: list[tuple[float, np.ndarray]] = []
    in_speech = False
    utt_frames: list[bytes] = []
    silence_run = 0
    utt_start_frame = 0
    pre_buffer: list[bytes] = []
    pre_frames = 5  # ~100 ms of pre-roll

    for i in range(total_frames):
        frame = raw[i * frame_bytes:(i + 1) * frame_bytes]
        try:
            is_speech = vad.is_speech(frame, sample_rate)
        except Exception:
            is_speech = False

        if not in_speech:
            pre_buffer.append(frame)
            if len(pre_buffer) > pre_frames:
                pre_buffer.pop(0)
            if is_speech:
                in_speech = True
                utt_frames = list(pre_buffer) + [frame]
                pre_buffer = []
                silence_run = 0
                utt_start_frame = max(0, i - len(utt_frames) + 1)
        else:
            utt_frames.append(frame)
            if is_speech:
                silence_run = 0
            else:
                silence_run += 1
            if silence_run >= silence_frames:
                if len(utt_frames) >= min_utt_frames:
                    _emit(segments, utt_frames, utt_start_frame, frame_samples, sample_rate)
                in_speech = False
                utt_frames = []
                silence_run = 0
            elif len(utt_frames) >= max_utt_frames:
                _emit(segments, utt_frames, utt_start_frame, frame_samples, sample_rate)
                in_speech = False
                utt_frames = []
                silence_run = 0

    if in_speech and len(utt_frames) >= min_utt_frames:
        _emit(segments, utt_frames, utt_start_frame, frame_samples, sample_rate)

    if not segments:
        # VAD didn't detect anything — transcribe the whole file as one chunk
        return [(0.0, samples_int16)]

    return segments


def _emit(segments, frames, start_frame, frame_samples, sample_rate):
    pcm = b"".join(frames)
    samples = np.frombuffer(pcm, dtype=np.int16).copy()
    offset = (start_frame * frame_samples) / sample_rate
    segments.append((offset, samples))


def transcribe_voicemail(
    wav_path: str,
    call_id: str,
    processor: ParakeetProcessor,
    db,
    event_bus=None,
    speaker: str = "caller",
):
    """Transcribe a voicemail WAV and save segments to the DB.

    Runs in a background daemon thread so the caller (the IVR api) returns
    immediately. Emits a voicemail.transcribed event on the bus when done.
    """
    def _worker():
        try:
            log.info("Post-transcribing voicemail %s", wav_path)
            samples, in_rate = _load_wav(wav_path)
            if samples.size == 0:
                log.warning("Empty voicemail: %s", wav_path)
                return

            segments = _vad_segments(samples, in_rate)
            if not segments:
                log.info("No speech detected in %s", wav_path)
                return

            # Resample each segment to 16 kHz for Parakeet
            resampler = AudioResampler(input_rate=in_rate, output_rate=16000)
            count = 0
            texts = []
            for offset, seg_int16 in segments:
                pcm_bytes = seg_int16.tobytes()
                audio_16k = resampler.process(pcm_bytes)
                if audio_16k.size == 0:
                    continue
                text = processor.transcribe_sync(audio_16k)
                if not text:
                    continue
                try:
                    db.save_transcript_segment(
                        call_id=call_id,
                        speaker=speaker,
                        text=text,
                        timestamp_offset=round(offset, 2),
                    )
                    count += 1
                    texts.append(text)
                except Exception:
                    log.exception("Failed to save voicemail segment")

            log.info("Voicemail transcription saved: %d segments on call %s",
                     count, call_id[:8])

            if event_bus and count > 0:
                event_bus.publish("voicemail.transcribed", {
                    "call_id": call_id,
                    "wav_path": wav_path,
                    "segment_count": count,
                    "text": " ".join(texts),
                })
        except Exception:
            log.exception("Voicemail transcription failed for %s", wav_path)

    threading.Thread(
        target=_worker,
        name=f"vm-stt-{call_id[:8]}",
        daemon=True,
    ).start()
