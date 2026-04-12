# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Kokoro TTS engine — neural, high quality.

Uses the hexgrad/Kokoro-82M model via the `kokoro` pip package. The
KPipeline is loaded ONCE at warmup time and kept in memory for the
lifetime of the Callen process so every synthesize() call is fast.

Pipeline produces 24 kHz float32 mono. We resample to 8 kHz int16 for
pjsua2 compatibility.
"""

import logging
import threading
import warnings

import numpy as np

from callen.tts.base import TTSEngine

log = logging.getLogger(__name__)

# Kokoro's imports emit a bunch of deprecation noise we don't care about
warnings.filterwarnings("ignore", category=UserWarning, module="torch.nn")
warnings.filterwarnings("ignore", category=FutureWarning, module="torch.nn")


class KokoroEngine(TTSEngine):
    name = "kokoro"

    # hexgrad/Kokoro-82M default sample rate
    _SAMPLE_RATE = 24000

    def __init__(
        self,
        voice: str = "af_heart",
        lang_code: str = "a",
        device: str | None = None,
        repo_id: str = "hexgrad/Kokoro-82M",
    ):
        self._voice = voice
        self._lang_code = lang_code
        self._device = device
        self._repo_id = repo_id
        self._pipeline = None
        self._lock = threading.Lock()

    def warmup(self):
        """Load the Kokoro model and keep it in memory."""
        if self._pipeline is not None:
            return
        log.info("Loading Kokoro TTS model (%s)...", self._repo_id)
        from kokoro import KPipeline

        kwargs = {"lang_code": self._lang_code, "repo_id": self._repo_id}
        if self._device:
            kwargs["device"] = self._device
        self._pipeline = KPipeline(**kwargs)
        log.info("Kokoro model ready (voice=%s)", self._voice)

    def synthesize(self, text: str, output_path: str) -> str:
        if self._pipeline is None:
            self.warmup()

        import resampy
        import soundfile as sf

        # Kokoro's pipeline splits long text into chunks and yields audio
        # per chunk. Concatenate them into a single audio stream.
        chunks = []
        with self._lock:  # KPipeline is not thread-safe
            for _gs, _ps, audio in self._pipeline(text, voice=self._voice):
                if audio is None:
                    continue
                if hasattr(audio, "cpu"):
                    audio = audio.cpu().numpy()
                chunks.append(np.asarray(audio, dtype=np.float32))

        if not chunks:
            raise RuntimeError("Kokoro produced no audio")

        full = np.concatenate(chunks)

        # Resample 24 kHz float32 -> 8 kHz float32
        resampled = resampy.resample(full, self._SAMPLE_RATE, 8000)

        # Clip and convert to int16
        resampled = np.clip(resampled, -1.0, 1.0)
        int16 = (resampled * 32767.0).astype(np.int16)

        sf.write(output_path, int16, 8000, subtype="PCM_16")
        return output_path
