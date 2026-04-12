# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""espeak-ng TTS engine — fast, low quality, no model required."""

import logging
import os
import subprocess

from callen.tts.base import TTSEngine

log = logging.getLogger(__name__)


class EspeakEngine(TTSEngine):
    name = "espeak"

    def __init__(self, voice: str | None = None, rate: int = 175):
        self._voice = voice
        self._rate = rate

    def synthesize(self, text: str, output_path: str) -> str:
        raw_path = output_path + ".raw.wav"
        cmd = ["espeak-ng", "-w", raw_path, "-s", str(self._rate)]
        if self._voice:
            cmd.extend(["-v", self._voice])
        cmd.append(text)

        subprocess.run(cmd, check=True, capture_output=True)

        # espeak-ng outputs 22050Hz — sox down-converts to 8kHz mono 16-bit
        subprocess.run(
            ["sox", raw_path, "-r", "8000", "-c", "1", "-b", "16", output_path],
            check=True, capture_output=True,
        )
        try:
            os.unlink(raw_path)
        except OSError:
            pass

        return output_path
