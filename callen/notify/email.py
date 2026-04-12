# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Voicemail email notification via SMTP."""

import logging
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

from callen.config import EmailConfig

log = logging.getLogger(__name__)


def send_voicemail_notification(
    config: EmailConfig,
    caller_id: str,
    voicemail_path: str,
    transcript: str | None = None,
):
    """Send voicemail notification email in a background thread."""
    if not config.enabled:
        return

    def _send():
        try:
            msg = MIMEMultipart()
            msg["From"] = config.from_address
            msg["To"] = config.to_address
            msg["Subject"] = f"Voicemail from {caller_id}"

            body = f"New voicemail from {caller_id}\n"
            if transcript:
                body += f"\nTranscript:\n{transcript}\n"
            body += f"\nRecording: {voicemail_path}\n"

            msg.attach(MIMEText(body, "plain"))

            # Attach the WAV file
            wav = Path(voicemail_path)
            if wav.exists() and wav.stat().st_size < 10_000_000:  # < 10MB
                part = MIMEBase("audio", "wav")
                part.set_payload(wav.read_bytes())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={wav.name}",
                )
                msg.attach(part)

            if config.smtp_tls:
                server = smtplib.SMTP(config.smtp_host, config.smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP(config.smtp_host, config.smtp_port)

            if config.smtp_user:
                server.login(config.smtp_user, config.smtp_password)

            server.sendmail(config.from_address, config.to_address, msg.as_string())
            server.quit()
            log.info("Voicemail notification sent to %s", config.to_address)

        except Exception:
            log.exception("Failed to send voicemail notification")

    threading.Thread(target=_send, name="email-notify", daemon=True).start()
