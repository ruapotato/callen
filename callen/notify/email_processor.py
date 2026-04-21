# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Inbound email processing — parse, thread deterministically, and queue
everything else for AI-agent triage.

Design:
  - Deterministic steps only:
      1. Parse the RFC 5322 message
      2. Drop duplicates (same Message-ID)
      3. Drop our own outgoing messages + obvious auto-replies/bounces
      4. If In-Reply-To / References matches an email we stored, route to
         the same incident as that email.
      5. Otherwise, leave incident_id=NULL in the emails table (pending
         triage) — an agent will decide whether it's a real support request
         and route it via the assign-email / reject-email tools.

  - NO incidents are created automatically for new threads. This prevents
    marketing/newsletter spam from filling the ticket queue.
  - NO auto-reply is sent on receipt. The consent handshake only fires
    when an agent explicitly creates an incident from a pending email.
  - Contacts ARE auto-created / updated so the agent has a pre-normalized
    contact to attach to. Contact creation alone doesn't imply a ticket.
"""

import email
import email.utils
import html.parser
import logging
import re
import time
from email.message import EmailMessage

from callen.config import EmailConfig
from callen.storage.models import EmailMessage as EmailRecord
from callen.notify.attachments import (
    extract_attachments,
    append_extracted_text_to_body,
)
from callen.notify.email import send_lockout_notice, LOCKOUT_SUBJECT

log = logging.getLogger(__name__)

# Matches INC-0042 anywhere in the subject, with or without brackets.
INCIDENT_ID_PATTERN = re.compile(r"\b(INC-\d{4,})\b")


# Prompt-injection heuristics. Hits don't block anything automatically —
# they move the email to 'flagged' status with a reason so the operator
# can review and either mark-safe or reject.
PROMPT_INJECTION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|messages?|rules?)", re.I),
     "ignore previous instructions"),
    (re.compile(r"forget\s+(?:all|everything|your)\s+(?:instructions?|prompts?|rules?|prior)", re.I),
     "forget instructions"),
    (re.compile(r"forget\s+(?:all\s+)?(?:previous|prior|above)\s+(?:prompts?|instructions?|rules?|messages?)", re.I),
     "forget previous prompts"),
    (re.compile(r"(?:send|give|tell|share|reveal|forward|read)\s+(?:me\s+)?(?:the\s+|my\s+|any\s+|our\s+|all\s+)?(?:one[-\s]?time\s+)?(?:password|passcode|code|otp|2fa|token|secret|credential)", re.I),
     "requesting credentials / OTP"),
    (re.compile(r"(?:send|give|tell|share|reveal|forward)\s+(?:me\s+)?(?:the\s+|my\s+|any\s+)?(?:login|verification|recovery|reset)\s+(?:code|password|link|key)", re.I),
     "requesting login / reset data"),
    (re.compile(r"(?:received|stored|saved|captured)\s+on\s+this\s+account", re.I),
     "asking for data stored on this account"),
    (re.compile(r"forget\s+(?:all\s+)?(?:previous|prior|above)\s+(?:prompts?|instructions?|rules?|messages?)", re.I),
     "forget previous prompts"),
    (re.compile(r"(?:send|give|tell|share|reveal|forward)\s+(?:me\s+)?(?:the\s+|my\s+|any\s+)?(?:one[-\s]?time\s+)?(?:password|passcode|code|otp|2fa|token|secret|credential)", re.I),
     "requesting credentials / OTP"),
    (re.compile(r"(?:send|give|tell|share|reveal|forward)\s+(?:me\s+)?(?:the\s+|my\s+|any\s+)?(?:login|verification|recovery|reset)", re.I),
     "requesting login data"),
    (re.compile(r"disregard\s+(?:all|everything|previous|prior|above)", re.I),
     "disregard previous"),
    (re.compile(r"you\s+are\s+(?:now\s+)?(?:a|an)?\s*(?:DAN|jailbroken|unrestricted|uncensored)", re.I),
     "DAN/jailbreak persona"),
    (re.compile(r"(?:system|developer)\s+prompt", re.I),
     "system prompt reference"),
    (re.compile(r"override\b.{0,20}\b(instructions?|rules?|prompt|safety)\b", re.I),
     "override instructions"),
    (re.compile(r"(?:^|\n)\s*new\s+instructions?\s*:", re.I),
     "new instructions marker"),
    (re.compile(r"prompt\s+injection", re.I),
     "literal prompt injection reference"),
    (re.compile(r"<\|im_start\|>|<\|endoftext\|>|<\|system\|>", re.I),
     "model special tokens"),
    (re.compile(r"reveal\s+(?:your|the)\s+(?:prompt|instructions|system)", re.I),
     "reveal prompt"),
    (re.compile(r"execute\s+(?:the\s+)?following\s+(?:command|code|script)", re.I),
     "execute following code"),
    (re.compile(r"curl\s+[^\s]+\s*\|\s*(?:bash|sh)", re.I),
     "curl pipe shell"),
]


def _scan_prompt_injection(text: str) -> tuple[bool, str]:
    """Check body text against injection patterns. Returns (matched, reason)."""
    if not text:
        return False, ""
    for pattern, label in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            return True, label
    return False, ""


class _HTMLStripper(html.parser.HTMLParser):
    """Minimal HTML-to-text for messages with no text/plain part."""
    def __init__(self):
        super().__init__()
        self.chunks = []

    def handle_data(self, data):
        self.chunks.append(data)

    @classmethod
    def strip(cls, html_text: str) -> str:
        p = cls()
        try:
            p.feed(html_text)
        except Exception:
            return html_text
        return "".join(p.chunks)


def _extract_bodies(msg: email.message.Message) -> tuple[str, str]:
    """Walk a message and extract (text_body, html_body). Prefers text/plain."""
    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                continue
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain" and not text_body:
                text_body = decoded
            elif ctype == "text/html" and not html_body:
                html_body = decoded
    else:
        ctype = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            decoded = msg.get_payload() or ""
        if ctype == "text/html":
            html_body = decoded
        else:
            text_body = decoded

    if not text_body and html_body:
        text_body = _HTMLStripper.strip(html_body).strip()

    return text_body, html_body


def _parse_address(header_value: str) -> tuple[str, str]:
    """Return (display_name, email_address). Address is lower-cased."""
    if not header_value:
        return "", ""
    display, addr = email.utils.parseaddr(header_value)
    return display or "", (addr or "").lower()


def _extract_formspree_fields(html_body: str) -> str:
    """Parse Formspree's styled HTML email into clean field: value lines.

    Returns a human-readable text version, or empty string on failure.
    """
    try:
        class _Extractor(html.parser.HTMLParser):
            def __init__(self):
                super().__init__()
                self.texts = []
                self._skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ('style', 'script'):
                    self._skip = True
            def handle_endtag(self, tag):
                if tag in ('style', 'script'):
                    self._skip = False
            def handle_data(self, data):
                if not self._skip:
                    t = data.strip()
                    if t:
                        self.texts.append(t)

        ext = _Extractor()
        ext.feed(html_body)

        # Formspree puts field names as standalone text nodes, followed by
        # the value as the next text node. Collect sequential pairs where
        # the "name" looks like a form field (lowercase, underscores).
        pairs = []
        i = 0
        while i < len(ext.texts) - 1:
            candidate = ext.texts[i]
            if re.match(r'^[a-z][a-z0-9_]{1,30}$', candidate):
                value = ext.texts[i + 1].strip()
                pairs.append((candidate, value))
                i += 2
            else:
                i += 1

        if not pairs:
            return ""

        lines = ["[Form submission via Formspree]", ""]
        for name, value in pairs:
            label = name.replace("_", " ").title()
            lines.append(f"{label}: {value}")
        return "\n".join(lines)
    except Exception:
        log.exception("Failed to parse Formspree HTML")
        return ""


def _looks_like_bulk(msg: email.message.Message) -> bool:
    """Cheap heuristic for marketing / list mail — the agent can still triage
    borderline cases, this just keeps us from re-sending consent requests
    on every newsletter."""
    # Whitelisted form relay services — they often set List-Unsubscribe
    # or Precedence headers but carry real user submissions.
    from_addr = (_parse_address(msg.get("From", ""))[1] or "").lower()
    form_relay_domains = {"formspree.io"}
    sender_domain = from_addr.split("@", 1)[-1] if "@" in from_addr else ""
    if sender_domain in form_relay_domains:
        return False

    for hdr in ("List-Unsubscribe", "List-Id", "Precedence", "X-Mailer",
                "Auto-Submitted"):
        v = msg.get(hdr, "")
        if hdr == "Precedence" and v.lower() in ("bulk", "list", "junk"):
            return True
        if hdr == "Auto-Submitted" and v.lower() not in ("", "no"):
            return True
        if hdr == "List-Unsubscribe" and v:
            return True
        if hdr == "List-Id" and v:
            return True
    return False


def apply_injection_response(
    db,
    config: EmailConfig,
    *,
    email_id: int,
    from_addr: str,
    contact_id: str | None,
    injection_reason: str,
    support_phone: str = "",
    source: str = "scanner",
) -> None:
    """Side effects for an email classified as prompt injection.

    Shared by the deterministic regex scanner and the downstream Mistral
    preflight classifier so both layers apply the same response:
      - create a security warning ticket (sender-only, no body)
      - mark the contact 'suspect'
      - hard-block the sender email
      - send the lockout notice
    """
    try:
        warn_id = db.create_incident(
            contact_id=contact_id,
            subject=f"Security: prompt-injection attempt from {from_addr}",
            channel="email",
            status="open",
            priority="high",
        )
        db.update_incident(warn_id, add_labels=["security", "injection"])
        db.add_incident_entry(
            warn_id, "note", author="system",
            payload={
                "text": (
                    f"Automated security alert. An inbound email from "
                    f"{from_addr} tripped the {source} "
                    f"({injection_reason}). The email body is NOT "
                    f"reproduced here — see email id {email_id} in the "
                    f"flagged queue if review is needed."
                ),
            },
        )
    except Exception:
        log.exception("Failed to create security warning ticket for %s", from_addr)

    if contact_id:
        try:
            db.set_contact_trust(contact_id, "suspect")
        except Exception:
            log.exception("Failed to mark contact %s as suspect", contact_id)

    try:
        db.block_email(
            from_addr,
            reason=f"auto: {source} ({injection_reason})",
        )
    except Exception:
        log.exception("Failed to auto-block sender %s", from_addr)

    try:
        send_lockout_notice(config, from_addr, support_phone)
    except Exception:
        log.exception("Failed to send lockout notice to %s", from_addr)


def process_message(
    raw_bytes: bytes,
    config: EmailConfig,
    db,
    event_bus=None,
    support_phone: str = "",
) -> dict | None:
    """Process a single raw email message.

    Returns a dict describing what happened, or None if the message was
    skipped (e.g. we sent it ourselves).
    """
    msg = email.message_from_bytes(raw_bytes)

    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        message_id = f"<synth-{int(time.time()*1000)}@callen.local>"

    display_name, from_addr = _parse_address(msg.get("From", ""))
    to_addr = _parse_address(msg.get("To", ""))[1]
    subject = (msg.get("Subject") or "").strip()
    in_reply_to = (msg.get("In-Reply-To") or "").strip()
    references = (msg.get("References") or "").strip()

    if not from_addr:
        log.warning("Skipping message with no From address (Message-ID=%s)", message_id)
        return None

    # Skip our own outgoing mail
    own_addresses = {
        (config.from_address or "").lower(),
        (config.smtp_user or "").lower(),
        (config.hello_address or "").lower(),
    }
    own_addresses.discard("")
    if from_addr in own_addresses:
        log.debug("Skipping our own outgoing message from %s", from_addr)
        return None

    # Skip bounces / automated system mail — but whitelist known form
    # relay services (Formspree, etc.) that use noreply@ addresses to
    # forward legitimate form submissions.
    FORM_RELAY_DOMAINS = {"formspree.io"}
    sender_domain = from_addr.split("@", 1)[-1] if "@" in from_addr else ""
    if sender_domain not in FORM_RELAY_DOMAINS:
        if any(s in from_addr for s in ("mailer-daemon", "postmaster", "no-reply", "noreply")):
            log.info("Skipping system message from %s", from_addr)
            return None

    # Skip replies/bounces of our own lockout notice to avoid loops
    if subject.startswith(LOCKOUT_SUBJECT.split("]", 1)[0]) and LOCKOUT_SUBJECT in subject:
        log.info("Skipping bounce of our lockout notice from %s", from_addr)
        return None

    # --- Hard block check ---
    # If this sender's email address is on the blocked list, we do NOT
    # extract attachments, run OCR, call the preflight LLM, or touch the
    # Claude agent. Store a minimal row with status='rejected' so there's
    # an audit trail, and nothing else.
    try:
        is_blocked, block_reason = db.email_is_blocked(from_addr)
    except Exception:
        is_blocked, block_reason = False, ""
    if is_blocked:
        log.warning("BLOCKED sender %s: %s (message %s)",
                    from_addr, block_reason, message_id)
        try:
            blocked_record = EmailRecord(
                message_id=message_id,
                incident_id=None,
                direction="in",
                from_addr=from_addr,
                to_addr=to_addr,
                subject=subject,
                body_text="(body not stored — sender is on the block list)",
                body_html="",
                received_at=time.time(),
                in_reply_to=in_reply_to,
            )
            db.save_email(
                blocked_record,
                status="rejected",
                status_reason=f"blocked_sender: {block_reason}",
            )
        except Exception:
            log.exception("Failed to persist blocked-sender stub row")
        # Auto-reply with the lockout notice so the sender knows how to
        # reach a human. This runs on every blocked message so a false
        # positive gets a consistent path back in.
        try:
            send_lockout_notice(
                config, from_addr, support_phone, in_reply_to=message_id,
            )
        except Exception:
            log.exception("Failed to send lockout notice to %s", from_addr)
        return {
            "status": "blocked_sender",
            "from": from_addr,
            "reason": block_reason,
        }

    # Dedupe by message-id
    if db.find_email_by_message_id(message_id):
        log.debug("Message %s already stored, skipping", message_id)
        return {"status": "duplicate", "message_id": message_id}

    text_body, html_body = _extract_bodies(msg)
    is_bulk = _looks_like_bulk(msg)

    # --- Extract attachments (images via OCR, PDFs via pdfplumber) ---
    # We insert the attachment id placeholder after save_email, but we
    # need the extracted text NOW so the injection scanner and the
    # downstream preflight classifier can see it.
    # Using email_id=0 as a temporary placeholder for the directory slug;
    # we rename after the row is created.
    attachment_records: list[dict] = []
    try:
        attachment_records = extract_attachments(msg, email_id=0, message_id=message_id)
    except Exception:
        log.exception("Attachment extraction failed for %s", message_id)

    # Append extracted attachment text to the body so every downstream
    # layer sees it as if it were typed in the email.
    combined_body = append_extracted_text_to_body(text_body, attachment_records)

    # Scan for prompt-injection patterns in the COMBINED body (so OCR'd
    # text from an image gets the same security treatment). Matches are
    # non-blocking; they just tag the email as 'flagged' so the operator
    # sees it separately from the normal triage queue.
    injection_hit, injection_reason = _scan_prompt_injection(combined_body)

    # Formspree sends HTML-only emails with form data buried in styled
    # tables. Extract the key-value pairs into clean readable text so the
    # agent (and the DB body_text column) gets something useful instead of
    # 60KB of CSS.
    if sender_domain in FORM_RELAY_DOMAINS and html_body:
        cleaned = _extract_formspree_fields(html_body)
        if cleaned:
            text_body = cleaned
            combined_body = append_extracted_text_to_body(text_body, attachment_records)

    contact_id = None  # set below, only for non-bulk senders

    # --- Deterministic threading ---
    # Rule 1: In-Reply-To / References matching a stored message
    # Rule 2: Subject line contains [INC-NNNN] or INC-NNNN referencing a
    #         real incident (standard ticketing convention)
    incident_id = None
    routing_rule = None

    if in_reply_to:
        incident_id = db.find_incident_by_email_reference(in_reply_to)
        if incident_id:
            routing_rule = "in_reply_to"
    if not incident_id and references:
        for ref in references.split():
            incident_id = db.find_incident_by_email_reference(ref.strip())
            if incident_id:
                routing_rule = "references"
                break

    if not incident_id and subject:
        m = INCIDENT_ID_PATTERN.search(subject)
        if m:
            candidate = m.group(1)
            if db.get_incident(candidate):
                incident_id = candidate
                routing_rule = "subject_tag"
                log.info("Email routed by subject tag: %s -> %s", message_id, incident_id)

    is_reply = incident_id is not None

    # --- Decide the email's initial status ---
    # Precedence (security-first):
    #   1. is_reply wins      -> 'attached' (already going to an incident)
    #   2. injection hit wins -> 'flagged' (even on marketing senders, so we
    #                             see injection attacks regardless of channel)
    #   3. bulk/marketing     -> 'rejected' with reason 'auto_bulk'
    #   4. otherwise          -> 'pending' (agent triage queue)
    if is_reply:
        status = "attached"
        status_reason = f"auto_routed:{routing_rule}"
    elif injection_hit:
        status = "flagged"
        reason_parts = [f"prompt_injection:{injection_reason}"]
        if is_bulk:
            reason_parts.append("bulk_sender")
        status_reason = " ".join(reason_parts)
    elif is_bulk:
        status = "rejected"
        status_reason = "auto_bulk"
    else:
        status = "pending"
        status_reason = ""

    # Only create a contact for senders that might become real tickets.
    # Bulk/marketing senders (auto-rejected) should NOT pollute the
    # contacts list — that's where actual humans live.
    if status != "rejected":
        contact_id = db.upsert_contact_by_email(from_addr, display_name=display_name)

    # Store the email row. body_text is the COMBINED body (original +
    # OCR'd attachment text) so the agent and downstream tools see
    # everything in one place.
    record = EmailRecord(
        message_id=message_id,
        incident_id=incident_id,
        direction="in",
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        body_text=combined_body,
        body_html=html_body,
        received_at=time.time(),
        in_reply_to=in_reply_to,
    )
    email_id = db.save_email(record, status=status, status_reason=status_reason)

    # Persist attachment metadata rows now that we have the email_id.
    for att in attachment_records:
        try:
            db.save_email_attachment(
                email_id=email_id,
                filename=att["filename"],
                content_type=att["content_type"],
                file_path=att["file_path"],
                size_bytes=att["size_bytes"],
                extracted_text=att["extracted_text"],
                extraction_method=att["extraction_method"],
            )
        except Exception:
            log.exception("Failed to save attachment row for email %d", email_id)

    result = {
        "status": "stored",
        "email_status": status,
        "email_status_reason": status_reason,
        "message_id": message_id,
        "email_id": email_id,
        "contact_id": contact_id,
        "incident_id": incident_id,
        "is_reply": is_reply,
        "is_bulk": is_bulk,
        "injection_hit": injection_hit,
        "routing_rule": routing_rule,
    }

    if is_reply:
        # Auto-threaded reply — log on the incident timeline
        db.add_incident_entry(
            incident_id, "email",
            author=display_name or from_addr,
            linked_email_id=email_id,
            payload={
                "direction": "in",
                "from": from_addr,
                "subject": subject,
                "preview": (text_body[:300] if text_body else ""),
                "routing_rule": routing_rule,
            },
        )
        log.info("Threaded reply from %s -> %s (via %s)",
                 from_addr, incident_id, routing_rule)
    elif status == "flagged":
        log.warning(
            "FLAGGED email from %s: %s (subject: %s)",
            from_addr, status_reason, subject[:60],
        )
        apply_injection_response(
            db, config,
            email_id=email_id,
            from_addr=from_addr,
            contact_id=contact_id,
            injection_reason=injection_reason,
            support_phone=support_phone,
            source="regex scanner",
        )
    elif status == "rejected":
        log.info(
            "Auto-rejected email from %s: %s",
            from_addr, status_reason,
        )
    else:
        log.info(
            "Pending email from %s (%s%s): %s",
            from_addr,
            "bulk, " if is_bulk else "",
            "new sender" if display_name or not is_bulk else "unknown",
            subject[:60] if subject else "(no subject)",
        )

    if event_bus:
        event_bus.publish(
            "email.received",
            {
                "email_id": email_id,
                "contact_id": contact_id,
                "incident_id": incident_id,
                "from": from_addr,
                "subject": subject,
                "is_reply": is_reply,
                "is_bulk": is_bulk,
            },
        )

    return result
