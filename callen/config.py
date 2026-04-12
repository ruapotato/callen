# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

import sys
import logging
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

log = logging.getLogger(__name__)


@dataclass
class SIPConfig:
    registrar: str = "sip:sip.voip.ms"
    username: str = ""
    password: str = ""
    domain: str = "sip.voip.ms"
    port: int = 5060


@dataclass
class OperatorConfig:
    name: str = "Operator"
    cell_phone: str = ""
    default_status: str = "available"


@dataclass
class RecordingConfig:
    enabled: bool = True
    directory: str = "./calls"
    split_channels: bool = True


@dataclass
class VoicemailConfig:
    directory: str = "./voicemail"
    max_duration: int = 120


@dataclass
class TranscriptionConfig:
    enabled: bool = True
    model: str = "nvidia/parakeet-tdt-0.6b-v2"
    device: str = "auto"
    chunk_seconds: float = 3.0


@dataclass
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    from_address: str = ""
    to_address: str = ""


@dataclass
class GeneralConfig:
    db_path: str = "./callen.db"
    ivr_script: str = "./IVR.py"
    log_level: str = "INFO"


@dataclass
class CallenConfig:
    sip: SIPConfig = field(default_factory=SIPConfig)
    operator: OperatorConfig = field(default_factory=OperatorConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    voicemail: VoicemailConfig = field(default_factory=VoicemailConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    web: WebConfig = field(default_factory=WebConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    general: GeneralConfig = field(default_factory=GeneralConfig)


def _apply_section(dc, data: dict):
    """Apply a dict of values onto a dataclass instance, ignoring unknown keys."""
    for key, value in data.items():
        if hasattr(dc, key):
            setattr(dc, key, value)
        else:
            log.warning("Unknown config key: %s", key)


def load_config(path: str = "config.toml") -> CallenConfig:
    """Load configuration from a TOML file. Exits if file not found."""
    config_path = Path(path)
    if not config_path.exists():
        print(f"Error: {path} not found.", file=sys.stderr)
        print(f"Copy config.toml.example to config.toml and fill in your credentials.", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    cfg = CallenConfig()
    section_map = {
        "sip": cfg.sip,
        "operator": cfg.operator,
        "recording": cfg.recording,
        "voicemail": cfg.voicemail,
        "transcription": cfg.transcription,
        "web": cfg.web,
        "email": cfg.email,
        "general": cfg.general,
    }

    for section_name, dc in section_map.items():
        if section_name in raw:
            _apply_section(dc, raw[section_name])

    if not cfg.sip.username or not cfg.sip.password:
        print("Error: SIP username and password must be set in config.toml", file=sys.stderr)
        sys.exit(1)

    return cfg
