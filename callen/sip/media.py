# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Audio media — TTS generation, WAV playback into calls, recording.
Uses SWIG pjsua2 AudioMediaPlayer, AudioMediaRecorder, AudioMediaPort.
"""

import logging
import os
import subprocess
import tempfile

import pjsua2 as pj

log = logging.getLogger(__name__)


def generate_tts_wav(text: str, output_path: str | None = None) -> str:
    """Generate 8kHz mono 16-bit WAV from text via the configured TTS engine.

    Routes through callen.tts.get_tts_engine() which returns a cached,
    pre-warmed engine (Kokoro by default, espeak as fallback).
    """
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="tts_")
        os.close(fd)

    from callen.tts import get_tts_engine
    engine = get_tts_engine()
    return engine.synthesize(text, output_path)


class PromptPlayer:
    """Plays a WAV file into a call via AudioMediaPlayer."""

    def __init__(self):
        self._player: pj.AudioMediaPlayer | None = None
        self._target: pj.AudioMedia | None = None

    def play(self, wav_path: str, target: pj.AudioMedia):
        """Play wav_path into target AudioMedia (the call's audio)."""
        self.stop()
        self._player = pj.AudioMediaPlayer()
        self._player.createPlayer(wav_path, pj.PJMEDIA_FILE_NO_LOOP)
        self._player.startTransmit(target)
        self._target = target
        log.debug("Playing: %s", wav_path)

    def play_loop(self, wav_path: str, target: pj.AudioMedia):
        """Play wav_path in a loop."""
        self.stop()
        self._player = pj.AudioMediaPlayer()
        self._player.createPlayer(wav_path, 0)  # 0 = loop
        self._player.startTransmit(target)
        self._target = target

    def stop(self):
        if self._player and self._target:
            try:
                self._player.stopTransmit(self._target)
            except Exception:
                pass
        self._player = None
        self._target = None

    def cleanup(self):
        self.stop()


class CallRecorder:
    """Records one audio channel to WAV via AudioMediaRecorder.

    pjsua2's AudioMediaRecorder only flushes the WAV header and final
    buffer when the underlying C++ object is destroyed. stopTransmit()
    alone leaves the file header incomplete, so we drop the Python
    reference in stop() to trigger pjsua2's destructor (via SWIG).
    """

    def __init__(self, file_path: str):
        self._path = file_path
        self._recorder = pj.AudioMediaRecorder()
        os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
        self._recorder.createRecorder(file_path)
        self._source: pj.AudioMedia | None = None
        log.info("Recorder created: %s", file_path)

    def start(self, source: pj.AudioMedia):
        source.startTransmit(self._recorder)
        self._source = source
        log.info("Recording started: %s", self._path)

    def stop(self):
        if self._source:
            try:
                self._source.stopTransmit(self._recorder)
            except Exception:
                pass
            self._source = None
        # Release the underlying pjsua2 recorder so the WAV is finalized.
        # Without this the file header is incomplete until GC runs,
        # which can take indefinitely long and breaks post-transcription.
        if self._recorder is not None:
            try:
                del self._recorder
            except Exception:
                pass
            self._recorder = None
        log.info("Recording stopped: %s", self._path)

    @property
    def path(self) -> str:
        return self._path

    def cleanup(self):
        self.stop()


class AudioTap(pj.AudioMediaPort):
    """Captures raw PCM frames for transcription via onFrameReceived."""

    def __init__(self, label: str, on_audio):
        super().__init__()
        self.label = label
        self._on_audio = on_audio
        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.id = pj.PJMEDIA_FORMAT_L16
        fmt.clockRate = 8000
        fmt.channelCount = 1
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = 20000
        # 8000 samples/sec * 16 bits * 1 channel = 128000 bps
        fmt.avgBps = 128000
        fmt.maxBps = 128000
        self.createPort("tap-" + label, fmt)

    def onFrameReceived(self, frame):
        try:
            self._on_audio(bytes(frame.buf))
        except Exception:
            pass

    def cleanup(self):
        pass


def check_audio_tools() -> list[str]:
    missing = []
    for tool in ["espeak-ng", "sox"]:
        try:
            subprocess.run([tool, "--version"], capture_output=True, check=False)
        except FileNotFoundError:
            missing.append(tool)
    return missing
