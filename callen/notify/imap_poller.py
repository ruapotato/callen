# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
IMAP poller — checks the monitored mailbox for new messages on an interval
and hands each one to email_processor.process_message.

Runs in its own thread. Reconnects on error; sleeps the configured interval
between polls.
"""

import imaplib
import logging
import ssl
import threading
import time

from callen.config import EmailConfig
from callen.notify.email_processor import process_message

log = logging.getLogger(__name__)


class IMAPPoller:
    def __init__(self, config: EmailConfig, db, event_bus=None):
        self._config = config
        self._db = db
        self._event_bus = event_bus
        self._running = False
        self._thread: threading.Thread | None = None
        self._conn: imaplib.IMAP4 | None = None

    def start(self):
        if not self._config.imap_enabled:
            log.info("IMAP poller disabled in config")
            return
        if not self._config.imap_host:
            log.warning("IMAP enabled but imap_host not set — skipping")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="imap-poll", daemon=True,
        )
        self._thread.start()
        log.info("IMAP poller started for %s (every %ds)",
                 self._config.imap_host, self._config.imap_poll_seconds)

    def stop(self):
        self._running = False
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None
        if self._thread:
            self._thread.join(timeout=5)

    # --- Internals ---

    def _run(self):
        while self._running:
            try:
                self._poll_once()
            except Exception:
                log.exception("IMAP poll error — reconnecting next cycle")
                if self._conn:
                    try:
                        self._conn.logout()
                    except Exception:
                        pass
                    self._conn = None
            # Interruptible sleep
            for _ in range(self._config.imap_poll_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _connect(self) -> imaplib.IMAP4:
        cfg = self._config
        # Proton Bridge uses a self-signed certificate on 127.0.0.1, so we
        # need an SSL context that doesn't verify for localhost connections.
        # For real remote hosts this should use the default verified context.
        is_local = cfg.imap_host in ("127.0.0.1", "localhost", "::1")
        if is_local:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()

        if cfg.imap_ssl:
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx)
        else:
            conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
            if cfg.imap_starttls:
                conn.starttls(ssl_context=ctx)
        user = cfg.imap_user or cfg.smtp_user
        password = cfg.imap_password or cfg.smtp_password
        conn.login(user, password)
        conn.select(cfg.imap_mailbox)
        return conn

    def _poll_once(self):
        if self._conn is None:
            self._conn = self._connect()

        # Find unseen messages
        typ, data = self._conn.search(None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError(f"IMAP SEARCH failed: {typ}")

        ids = data[0].split() if data and data[0] else []
        if not ids:
            return

        log.info("IMAP: %d new message(s) in %s", len(ids), self._config.imap_mailbox)

        for uid in ids:
            try:
                typ, msg_data = self._conn.fetch(uid, "(RFC822)")
                if typ != "OK" or not msg_data:
                    log.warning("FETCH failed for UID %s", uid.decode())
                    continue
                raw_bytes = None
                for part in msg_data:
                    if isinstance(part, tuple) and len(part) >= 2:
                        raw_bytes = part[1]
                        break
                if raw_bytes is None:
                    continue

                result = process_message(
                    raw_bytes, self._config, self._db, self._event_bus,
                )
                if result:
                    log.debug("Processed UID %s: %s", uid.decode(), result)

                # Mark seen (UNSEEN scan won't pick it up next iteration)
                self._conn.store(uid, "+FLAGS", "\\Seen")
            except Exception:
                log.exception("Failed to process IMAP UID %s", uid.decode() if uid else "?")
