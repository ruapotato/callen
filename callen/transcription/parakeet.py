# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3
#
# Parakeet STT processor — adapted from Voice-Command project
# (~/Voice-Command/speech/whisper_processor.py)

"""
ParakeetProcessor — loads NVIDIA Parakeet-TDT 0.6B V2 via NeMo for
speech-to-text transcription. Thread-safe via a processing lock.

Audio input: 16kHz mono float32 numpy array.
Output: text string with punctuation and capitalization.
"""

import logging
import os
import tempfile
import threading
import warnings

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

# Suppress noisy NeMo/Lightning warnings
logging.getLogger("nemo_toolkit").setLevel(logging.ERROR)
logging.getLogger("nemo").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=UserWarning, module="pytorch_lightning.*")
warnings.filterwarnings("ignore", category=FutureWarning)


class ParakeetProcessor:
    """
    Loads and runs NVIDIA Parakeet-TDT 0.6B V2 for speech-to-text.

    Thread-safe: concurrent calls to transcribe_sync() are serialized
    via a lock (GPU is single-consumer).
    """

    def __init__(self, model_id: str = "nvidia/parakeet-tdt-0.6b-v2", device: str = "auto"):
        self._model_id = model_id
        self._lock = threading.Lock()
        self._model = None

        if device == "auto":
            import torch
            self._device = "cuda:0" if torch.cuda.is_available() else "cpu"
        else:
            self._device = device

    def setup(self):
        """Load the model. Call once at startup (downloads on first run, ~2GB)."""
        import torch
        import nemo.collections.asr as nemo_asr

        log.info("Loading Parakeet model %s on %s...", self._model_id, self._device)
        self._model = nemo_asr.models.ASRModel.from_pretrained(model_name=self._model_id)
        self._model.to(self._device)
        self._model.eval()
        log.info("Parakeet model loaded successfully")

    def _preprocess(self, audio: np.ndarray) -> np.ndarray:
        """Ensure audio is 1D float32 normalized to [-1, 1]."""
        if audio is None or audio.size == 0:
            return np.array([], dtype=np.float32)

        # Ensure 1D (mono)
        if audio.ndim > 1:
            audio = np.mean(audio, axis=-1)

        # Convert to float32, normalize integers
        if audio.dtype != np.float32:
            if np.issubdtype(audio.dtype, np.integer):
                max_val = np.iinfo(audio.dtype).max
                audio = audio.astype(np.float32) / max_val
            else:
                audio = audio.astype(np.float32)

        # Clamp to [-1, 1]
        abs_max = np.abs(audio).max()
        if abs_max > 1.0:
            audio /= abs_max

        return audio

    def transcribe_sync(self, audio: np.ndarray, sample_rate: int = 16000) -> str | None:
        """
        Synchronous transcription. Blocks until complete.

        Args:
            audio: float32 numpy array at sample_rate Hz
            sample_rate: sample rate of the audio (default 16000)

        Returns:
            Transcribed text with punctuation, or None on failure/silence.
        """
        if self._model is None:
            log.error("Model not loaded — call setup() first")
            return None

        if audio is None or audio.size == 0:
            return None

        audio = self._preprocess(audio)
        if audio.size == 0:
            return None

        # Minimum audio length check (~0.5 seconds)
        min_samples = int(0.5 * sample_rate)
        if audio.size < min_samples:
            log.debug("Audio too short (%d samples), skipping", audio.size)
            return None

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                sf.write(f.name, audio, sample_rate)
                temp_path = f.name

            with self._lock:
                results = self._model.transcribe([temp_path])

            if not results or not isinstance(results, list) or len(results) == 0:
                return None

            first = results[0]
            if isinstance(first, str):
                text = first
            elif hasattr(first, "text"):
                text = first.text
            else:
                text = str(first)

            text = text.strip()
            if not text:
                return None

            log.debug("Transcribed: %s", text[:80])
            return text

        except Exception:
            log.exception("Transcription error")
            return None
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
