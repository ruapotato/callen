# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""CallenAccount — SIP account registered with VoIP.ms. SWIG API."""

import logging
from typing import Callable

import pjsua2 as pj

from callen.config import SIPConfig
from callen.sip.call import CallenCall

log = logging.getLogger(__name__)


class CallenAccount(pj.Account):
    def __init__(self, config: SIPConfig, call_handler: Callable[[CallenCall], None]):
        super().__init__()
        self._config = config
        self._call_handler = call_handler
        self._active_calls: list[CallenCall] = []

    def register(self):
        acfg = pj.AccountConfig()
        acfg.idUri = f"sip:{self._config.username}@{self._config.domain}"
        acfg.regConfig.registrarUri = self._config.registrar

        cred = pj.AuthCredInfo()
        cred.scheme = "digest"
        cred.realm = "*"
        cred.username = self._config.username
        cred.data = self._config.password
        cred.dataType = 0
        acfg.sipConfig.authCreds.append(cred)

        self.create(acfg)
        log.info("SIP account created: %s", acfg.idUri)

    def onRegState(self, prm):
        try:
            ai = self.getInfo()
            if ai.regIsActive:
                log.info("SIP registered (expires: %ds)", ai.regExpiresSec)
            else:
                log.warning("SIP registration failed: %d", ai.regLastErr)
        except Exception:
            log.exception("Error in onRegState")

    def onIncomingCall(self, prm):
        call = CallenCall(self, prm.callId)
        call.caller_id = call.get_caller_id()
        log.info("Incoming call from %s (call %s)", call.caller_id, call.uuid[:8])

        self._active_calls.append(call)

        def on_state_change(c):
            if c.state.value == "disconnected" and c in self._active_calls:
                self._active_calls.remove(c)

        call.set_callbacks(on_state_change=on_state_change)

        prm_answer = pj.CallOpParam()
        prm_answer.statusCode = pj.PJSIP_SC_OK
        try:
            call.answer(prm_answer)
        except Exception:
            log.exception("Failed to answer call %s", call.uuid[:8])
            return

        self._call_handler(call)
