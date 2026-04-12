# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Outbound email — voicemail notifications, auto-replies, agent replies."""

import email.utils
import logging
import smtplib
import threading
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

from callen.config import EmailConfig

log = logging.getLogger(__name__)


def send_mail(
    config: EmailConfig,
    to: str,
    subject: str,
    body_text: str,
    in_reply_to: str | None = None,
    references: str | None = None,
    message_id: str | None = None,
    cc: str | None = None,
) -> str:
    """Send a plain-text email synchronously. Returns the Message-ID used.

    Raises on failure — caller decides whether to retry.
    """
    if not message_id:
        # Generate an RFC 5322 message id tied to our domain
        domain = config.from_address.split("@", 1)[-1] or "callen.local"
        message_id = f"<{uuid.uuid4()}@{domain}>"

    msg = MIMEMultipart("alternative")
    msg["From"] = config.from_address
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = email.utils.formatdate(localtime=True)
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    elif in_reply_to:
        msg["References"] = in_reply_to

    msg.attach(MIMEText(body_text, "plain"))

    recipients = [to]
    if cc:
        recipients.append(cc)

    if config.smtp_tls:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port)
        server.starttls()
    else:
        server = smtplib.SMTP(config.smtp_host, config.smtp_port)

    try:
        if config.smtp_user:
            server.login(config.smtp_user, config.smtp_password)
        server.sendmail(config.from_address, recipients, msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass

    log.info("Sent mail to %s (subject: %s)", to, subject)
    return message_id


def send_voicemail_notification(
    config: EmailConfig,
    caller_id: str,
    voicemail_path: str,
    transcript: str | None = None,
):
    """Notify the operator about a new voicemail — runs in a background thread."""
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

            wav = Path(voicemail_path)
            if wav.exists() and wav.stat().st_size < 10_000_000:
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
