# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
SIP Endpoint — manages pjsua2 library lifecycle.
SWIG pjsua2: threadCnt=0, Python drives event loop via libHandleEvents().
All pjsua2 operations happen on the poll thread.
"""

import logging
import threading

import pjsua2 as pj

from callen.config import SIPConfig
from callen.sip.commands import SIPCommandQueue

log = logging.getLogger(__name__)


class SIPEndpoint:
    def __init__(self, config: SIPConfig, cmd_queue: SIPCommandQueue):
        self._config = config
        self._cmd_queue = cmd_queue
        self._ep = pj.Endpoint()
        self._running = False
        self._poll_thread: threading.Thread | None = None
        self._started = threading.Event()

    @property
    def endpoint(self) -> pj.Endpoint:
        return self._ep

    def start(self):
        """Start pjsua2 on a dedicated thread (all pjlib calls happen there)."""
        self._running = True
        self._poll_thread = threading.Thread(
            target=self._sip_thread_main,
            name="sip-poll",
            daemon=True,
        )
        self._poll_thread.start()
        self._started.wait(timeout=10)

    def _sip_thread_main(self):
        """The SIP thread — all pjlib operations happen here."""
        self._ep.libCreate()

        ep_cfg = pj.EpConfig()
        ep_cfg.uaConfig.threadCnt = 0
        ep_cfg.uaConfig.mainThreadOnly = False
        ep_cfg.logConfig.level = 3
        ep_cfg.logConfig.consoleLevel = 3

        # Disable VAD/silence suppression — VoIP.ms calls were dropping audio
        # during natural conversational pauses with VAD on.
        ep_cfg.medConfig.noVad = True
        # Larger jitter buffer helps with the WAN path to VoIP.ms
        ep_cfg.medConfig.jbInit = 60
        ep_cfg.medConfig.jbMinPre = 20
        ep_cfg.medConfig.jbMaxPre = 240
        ep_cfg.medConfig.jbMax = 360

        self._ep.libInit(ep_cfg)

        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self._config.port
        self._ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tp_cfg)

        self._ep.libStart()
        self._ep.audDevManager().setNullDev()

        log.info("pjsua2 endpoint started on port %d", self._config.port)
        self._started.set()

        # Event loop
        while self._running:
            self._ep.libHandleEvents(20)
            self._cmd_queue.process_pending()

        # Shutdown on this thread too
        try:
            self._ep.hangupAllCalls()
        except Exception:
            pass
        self._ep.libDestroy()
        log.info("SIP endpoint destroyed")

    def shutdown(self):
        log.info("Shutting down SIP endpoint")
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
