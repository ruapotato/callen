# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""WebSocket endpoints for live call updates and transcription.

Uses the per-subscriber asyncio.Queue pattern so a slow/dead consumer
never silences other subscribers. Each handler owns a queue; the event
forwarder pushes messages into every registered queue; the handler
concurrently pulls from its queue and sends to its websocket.
"""

import asyncio
import json
import logging

from quart import Blueprint, current_app, websocket

log = logging.getLogger(__name__)
bp = Blueprint("ws", __name__)

# Subscriber queues, one per websocket connection
_call_queues: set[asyncio.Queue] = set()
_transcript_queues: dict[str, set[asyncio.Queue]] = {}  # call_id -> queues


async def _broadcast_call_event(data):
    """Fan out a call event to every /ws/calls subscriber."""
    msg = json.dumps(data)
    for q in _call_queues.copy():
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # drop on overflow rather than block


async def _broadcast_transcript(data):
    """Fan out a transcript segment to the relevant /ws/transcript/<call_id>."""
    call_id = data.get("call_id")
    if not call_id:
        return
    msg = json.dumps(data)
    for q in _transcript_queues.get(call_id, set()).copy():
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def setup_event_forwarding(event_bus, loop):
    """Subscribe to EventBus events and forward to WebSocket clients."""

    def on_call_event(data):
        asyncio.run_coroutine_threadsafe(_broadcast_call_event(data), loop)

    def on_transcript(data):
        asyncio.run_coroutine_threadsafe(_broadcast_transcript(data), loop)

    event_bus.subscribe("call.incoming", lambda d: on_call_event({"type": "incoming", **d}))
    event_bus.subscribe("call.bridged", lambda d: on_call_event({"type": "bridged", **d}))
    event_bus.subscribe("call.ended", lambda d: on_call_event({"type": "ended", **d}))
    event_bus.subscribe("call.bridge_completed",
                        lambda d: on_call_event({"type": "bridge_completed", **d}))
    event_bus.subscribe("operator.status_changed",
                        lambda d: on_call_event({"type": "operator_status", **d}))
    event_bus.subscribe("email.received", lambda d: on_call_event({"type": "email", **d}))
    # Transcript updates go to both per-call channels AND the global
    # calls channel so the dashboard can refresh the focused incident
    # whenever new segments land.
    event_bus.subscribe("transcript.update", on_transcript)
    event_bus.subscribe("transcript.update",
                        lambda d: on_call_event({"type": "transcript", **d}))


@bp.websocket("/ws/calls")
async def ws_calls():
    """Live call state updates. Two concurrent tasks — sender pulls from
    the queue; receiver detects client disconnect."""
    queue = asyncio.Queue(maxsize=500)
    _call_queues.add(queue)

    async def _sender():
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                await websocket.send(msg)
            except asyncio.TimeoutError:
                # heartbeat to keep the connection alive
                await websocket.send(json.dumps({"type": "heartbeat"}))

    async def _receiver():
        # Client never sends anything, but we need to watch for disconnect
        while True:
            await websocket.receive()

    sender = asyncio.ensure_future(_sender())
    receiver = asyncio.ensure_future(_receiver())
    try:
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except asyncio.CancelledError:
        sender.cancel()
        receiver.cancel()
    finally:
        _call_queues.discard(queue)


@bp.websocket("/ws/transcript/<call_id>")
async def ws_transcript(call_id):
    """Live transcription for a specific call."""
    queue = asyncio.Queue(maxsize=500)
    _transcript_queues.setdefault(call_id, set()).add(queue)

    async def _sender():
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                await websocket.send(msg)
            except asyncio.TimeoutError:
                await websocket.send(json.dumps({"type": "heartbeat"}))

    async def _receiver():
        while True:
            await websocket.receive()

    sender = asyncio.ensure_future(_sender())
    receiver = asyncio.ensure_future(_receiver())
    try:
        done, pending = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    except asyncio.CancelledError:
        sender.cancel()
        receiver.cancel()
    finally:
        if call_id in _transcript_queues:
            _transcript_queues[call_id].discard(queue)
            if not _transcript_queues[call_id]:
                del _transcript_queues[call_id]
