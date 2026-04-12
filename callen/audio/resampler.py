# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Audio resampling — converts G.711 decoded 8kHz PCM to 16kHz float32 for Parakeet."""

import numpy as np
import resampy


class AudioResampler:
    """Converts 8kHz int16 PCM (from SIP/G.711) to 16kHz float32 for Parakeet STT."""

    def __init__(self, input_rate: int = 8000, output_rate: int = 16000):
        self.input_rate = input_rate
        self.output_rate = output_rate

    def process(self, pcm_bytes: bytes) -> np.ndarray:
        """
        Convert raw PCM16 bytes at input_rate to float32 numpy array at output_rate.

        Args:
            pcm_bytes: Raw 16-bit signed integer PCM audio bytes

        Returns:
            Float32 numpy array normalized to [-1.0, 1.0] at output_rate
        """
        # Decode bytes to int16 array
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)

        if len(samples) == 0:
            return np.array([], dtype=np.float32)

        # Convert to float32 normalized [-1, 1]
        audio = samples.astype(np.float32) / 32768.0

        # Resample if rates differ
        if self.input_rate != self.output_rate:
            audio = resampy.resample(audio, self.input_rate, self.output_rate)

        return audio
