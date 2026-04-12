# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Abstract TTS engine interface.

All engines produce an 8kHz mono 16-bit PCM WAV file at a given path,
because that's what pjsua2's AudioMediaPlayer can feed directly into
a call's conference bridge without further resampling.
"""

from abc import ABC, abstractmethod


class TTSEngine(ABC):
    """Base class for text-to-speech engines."""

    name: str = "abstract"

    @abstractmethod
    def synthesize(self, text: str, output_path: str) -> str:
        """Generate an 8kHz mono 16-bit WAV at output_path.

        Returns output_path on success, raises on failure.
        """

    def warmup(self):
        """Optional: load heavy models / weights into memory at startup
        so the first synthesize() call doesn't pay a cold-start cost.
        Default no-op."""
        pass
