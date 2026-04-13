# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
IVR Engine — loads the user's IVR script and runs it in a per-call thread.
"""

import logging
import threading
from pathlib import Path

from callen.sip.call import CallenCall, CallState
from callen.sip.commands import SIPCommandQueue
from callen.state.operator import OperatorState
from callen.state.events import EventBus
from callen.state.calls import CallRegistry
from callen.config import CallenConfig
from callen.ivr import api

log = logging.getLogger(__name__)


class IVREngine:
    def __init__(
        self,
        config: CallenConfig,
        cmd_queue: SIPCommandQueue,
        operator_state: OperatorState,
        event_bus: EventBus,
        call_registry: CallRegistry,
    ):
        self._config = config
        self._cmd_queue = cmd_queue
        self._operator_state = operator_state
        self._event_bus = event_bus
        self._call_registry = call_registry
        self._script_source: str | None = None
        self._load_script()

        # Wire up the API module globals
        api._cmd_queue = cmd_queue
        api._operator_state = operator_state
        api._event_bus = event_bus
        api._config = config
        # Allow the IVR api to hand a DB reference to post-processing
        # workers like the voicemail transcriber. We pull it from the
        # storage layer the engine already has access to.

    def _load_script(self):
        script_path = Path(self._config.general.ivr_script)
        if not script_path.exists():
            log.error("IVR script not found: %s", script_path)
            return
        self._script_source = script_path.read_text()
        log.info("IVR script loaded: %s", script_path)

    def reload_script(self):
        self._load_script()

    def handle_call(self, call: CallenCall):
        """Called when a new inbound call is answered. Spawns IVR thread."""
        self._call_registry.add(call)
        self._event_bus.publish("call.incoming", {
            "call_id": call.uuid,
            "caller_id": call.caller_id,
        })

        # Wait for media to be ready before running IVR
        thread = threading.Thread(
            target=self._run_ivr,
            args=(call,),
            name=f"ivr-{call.uuid[:8]}",
            daemon=True,
        )
        thread.start()

    def _run_ivr(self, call: CallenCall):
        """Thread target: wait for media, then execute the IVR script."""
        # Register this thread with pjlib so we can safely touch pjsua2 objects
        # (e.g. when they get garbage collected at the end of this thread)
        import pjsua2 as pj
        try:
            ep = pj.Endpoint.instance()
            if not ep.libIsThreadRegistered():
                ep.libRegisterThread(threading.current_thread().name)
        except Exception:
            log.exception("Failed to register IVR thread with pjlib")

        # Reload the IVR script on every call so edits to IVR.py apply
        # live without restarting Callen. The file is small, this is cheap.
        self._load_script()

        # Wait for audio media to be established
        call.media_ready.wait(timeout=10)
        if call.state == CallState.DISCONNECTED:
            log.info("Call %s disconnected before IVR started", call.uuid[:8])
            self._call_registry.remove(call.uuid)
            return

        if not self._script_source:
            log.error("No IVR script loaded, hanging up call %s", call.uuid[:8])
            api.hangup(call)
            self._call_registry.remove(call.uuid)
            return

        # Build namespace with IVR API functions
        namespace = {
            "say": api.say,
            "play": api.play,
            "dtmf": api.dtmf,
            "bridge_to_operator": api.bridge_to_operator,
            "record_voicemail": api.record_voicemail,
            "hangup": api.hangup,
            "caller_id": api.caller_id,
            "operator_available": api.operator_available,
            "has_consented": api.has_consented,
            "is_blocked": api.is_blocked,
        }

        try:
            exec(self._script_source, namespace)

            ivr_func = namespace.get("IVR")
            if ivr_func is None:
                log.error("IVR script does not define an IVR() function")
                api.hangup(call)
                return

            log.info("Running IVR for call %s from %s", call.uuid[:8], call.caller_id)
            ivr_func(call)

        except Exception:
            log.exception("IVR script error for call %s", call.uuid[:8])
        finally:
            if call.state != CallState.DISCONNECTED:
                api.hangup(call)
            self._call_registry.remove(call.uuid)
            call.cleanup()
            log.info("IVR thread finished for call %s", call.uuid[:8])
