# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Outbound email — voicemail notifications, auto-replies, agent replies."""

import email.utils
import logging
import smtplib
import ssl
import threading
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

from callen.config import EmailConfig


def _smtp_ssl_context(host: str) -> ssl.SSLContext:
    """Build an SSL context tolerant of Proton Bridge's self-signed cert
    when talking to localhost. Strict verification for remote hosts."""
    is_local = host in ("127.0.0.1", "localhost", "::1")
    ctx = ssl.create_default_context()
    if is_local:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx

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
        server.starttls(context=_smtp_ssl_context(config.smtp_host))
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


LOCKOUT_SUBJECT = "[freesoftware.support] Your email account has been locked from AI processing"


def send_lockout_notice(
    config: EmailConfig,
    to_addr: str,
    support_phone: str,
    in_reply_to: str | None = None,
) -> str | None:
    """Notify a blocked sender that their account is locked out and how
    to reach a human to appeal. Returns the Message-ID on success, or
    None if skipped/failed.

    Runs synchronously — caller can wrap in a thread if desired.
    """
    if not config.enabled or not to_addr:
        return None
    # Don't bounce mail at ourselves or at mailer-daemons
    lower = to_addr.lower()
    if lower == (config.from_address or "").lower():
        return None
    if any(s in lower for s in ("mailer-daemon", "postmaster", "noreply", "no-reply")):
        return None

    body = (
        "Hello,\n\n"
        "Your email address has been locked out of automated processing at "
        "freesoftware.support. A message you sent contained content that our "
        "security filters flagged as a prompt-injection attempt, so the AI "
        "agent will no longer read messages from your address.\n\n"
        "If this was a mistake and you're a real person looking for help, "
        "please call us directly and ask to be unblocked:\n\n"
        f"    {support_phone or '(phone unavailable — please contact the operator)'}\n\n"
        "A human will answer and can remove the block. Until then, any "
        "further emails you send to this address will receive this same "
        "auto-reply and will NOT be read.\n\n"
        "— freesoftware.support\n"
    )
    try:
        return send_mail(
            config,
            to=to_addr,
            subject=LOCKOUT_SUBJECT,
            body_text=body,
            in_reply_to=in_reply_to,
        )
    except Exception:
        log.exception("Failed to send lockout notice to %s", to_addr)
        return None


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
