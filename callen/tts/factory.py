# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Pick and warm up a TTS engine based on config."""

import logging

from callen.tts.base import TTSEngine

log = logging.getLogger(__name__)

_cached_engine: TTSEngine | None = None


def get_tts_engine(config=None) -> TTSEngine:
    """Return a (cached) TTS engine based on config.

    config is a TTSConfig dataclass (see callen.config.TTSConfig).
    Falls back to espeak if the requested engine fails to load.
    """
    global _cached_engine
    if _cached_engine is not None:
        return _cached_engine

    engine_name = "kokoro"
    voice = None
    lang_code = "a"
    device = None

    if config is not None:
        engine_name = getattr(config, "engine", "kokoro")
        voice = getattr(config, "voice", None)
        lang_code = getattr(config, "lang_code", "a")
        device = getattr(config, "device", None) or None

    if engine_name == "kokoro":
        try:
            from callen.tts.kokoro import KokoroEngine
            engine = KokoroEngine(
                voice=voice or "af_heart",
                lang_code=lang_code,
                device=device,
            )
            engine.warmup()
            _cached_engine = engine
            return engine
        except Exception:
            log.exception("Kokoro TTS failed to load — falling back to espeak")

    from callen.tts.espeak import EspeakEngine
    engine = EspeakEngine(voice=voice)
    _cached_engine = engine
    return engine


def reset():
    """Clear the cached engine (for testing)."""
    global _cached_engine
    _cached_engine = None
