# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Chunked audio buffer — accumulates samples and emits fixed-duration segments."""

import numpy as np


class AudioChunkBuffer:
    """
    Accumulates audio samples and emits chunks of a configurable duration.

    Feed resampled float32 audio via append(). When the buffer reaches
    chunk_seconds of audio, append() returns the chunk. Otherwise returns None.
    """

    def __init__(self, chunk_seconds: float = 3.0, sample_rate: int = 16000):
        self.chunk_samples = int(chunk_seconds * sample_rate)
        self.sample_rate = sample_rate
        self._buffer: list[np.ndarray] = []
        self._total_samples = 0

    def append(self, samples: np.ndarray) -> np.ndarray | None:
        """
        Append samples to the buffer.
        Returns a chunk when the buffer is full, otherwise None.
        """
        if len(samples) == 0:
            return None

        self._buffer.append(samples)
        self._total_samples += len(samples)

        if self._total_samples >= self.chunk_samples:
            return self._emit()

        return None

    def flush(self) -> np.ndarray | None:
        """Return any remaining buffered audio (for end-of-call)."""
        if self._total_samples == 0:
            return None
        return self._emit()

    def _emit(self) -> np.ndarray:
        chunk = np.concatenate(self._buffer)
        self._buffer = []
        self._total_samples = 0
        return chunk

    def reset(self):
        self._buffer = []
        self._total_samples = 0
