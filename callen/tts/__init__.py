# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Text-to-speech engines. Default is Kokoro (neural, higher quality).
Fallback is espeak-ng (fast, low quality)."""

from callen.tts.base import TTSEngine
from callen.tts.factory import get_tts_engine

__all__ = ["TTSEngine", "get_tts_engine"]
