# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Top-level orchestrator — wires everything together and runs the system."""

import logging
import signal
import sys
import threading

import pjsua2 as pj

from callen.config import load_config
from callen.sip.commands import SIPCommandQueue
from callen.sip.endpoint import SIPEndpoint
from callen.sip.account import CallenAccount
from callen.sip.call import CallenCall, CallState
from callen.sip.media import check_audio_tools
from callen.ivr.engine import IVREngine
from callen.ivr import api
from callen.state.events import EventBus
from callen.state.operator import OperatorState
from callen.state.calls import CallRegistry
from callen.storage.db import Database
from callen.storage.models import CallRecord

log = logging.getLogger(__name__)


def main(config_path: str = "config.toml"):
    config = load_config(config_path)

    logging.basicConfig(
        level=getattr(logging, config.general.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)-30s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info("Starting Callen IVR system")

    missing = check_audio_tools()
    if missing:
        log.warning("Missing audio tools: %s — install with: sudo apt install %s",
                     ", ".join(missing), " ".join(missing))

    event_bus = EventBus()
    cmd_queue = SIPCommandQueue()
    operator_state = OperatorState(event_bus, config.operator.default_status)
    call_registry = CallRegistry()
    db = Database(config.general.db_path)
    db.initialize()

    def on_call_ended(data):
        call_id = data.get("call_id")
        call = call_registry.get(call_id)
        if call:
            record = CallRecord(
                id=call.uuid,
                caller_id=call.caller_id,
                started_at=call.started_at,
                answered_at=call.answered_at,
                ended_at=call.ended_at,
                duration_seconds=call.duration,
                consented=call.consented_to_recording,
                was_bridged=(call.state == CallState.BRIDGED),
            )
            try:
                db.save_call(record)
            except Exception:
                log.exception("Failed to save call record")

    event_bus.subscribe("call.ended", on_call_ended)

    ivr_engine = IVREngine(
        config=config,
        cmd_queue=cmd_queue,
        operator_state=operator_state,
        event_bus=event_bus,
        call_registry=call_registry,
    )

    sip_endpoint = SIPEndpoint(config.sip, cmd_queue)

    def on_incoming_call(call: CallenCall):
        ivr_engine.handle_call(call)

    sip_account = CallenAccount(config.sip, on_incoming_call)

    def make_outbound_call(inbound_call, dst_uri):
        """Place outbound call to operator. MUST return quickly (called on SIP thread).
        Caller waits on outbound.media_ready themselves."""
        outbound = CallenCall(sip_account)
        outbound.caller_id = "operator"
        prm = pj.CallOpParam(True)  # Use default opts
        try:
            outbound.makeCall(dst_uri, prm)
            return outbound
        except Exception:
            log.exception("Outbound call failed: %s", dst_uri)
            return None

    api._make_outbound_call = make_outbound_call

    sip_endpoint.start()
    # Register account on the SIP thread (all pjlib calls must happen there)
    cmd_queue.submit(sip_account.register).result(timeout=10)

    log.info("=" * 60)
    log.info("Callen IVR running — waiting for calls")
    log.info("Press Ctrl+C to shut down")
    log.info("=" * 60)

    shutdown_event = threading.Event()

    def shutdown(sig, frame):
        log.info("Shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    sip_endpoint.shutdown()
    log.info("Callen stopped")
