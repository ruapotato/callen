# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""WebSocket endpoints for live call updates and transcription."""

import asyncio
import json
import logging

from quart import Blueprint, current_app, websocket

log = logging.getLogger(__name__)
bp = Blueprint("ws", __name__)

# Connected WebSocket clients
_call_subscribers: set = set()
_transcript_subscribers: dict[str, set] = {}  # call_id -> set of send functions


async def _broadcast_call_event(data):
    """Send call event to all /ws/calls subscribers."""
    msg = json.dumps(data)
    dead = set()
    for send_fn in _call_subscribers.copy():
        try:
            await send_fn(msg)
        except Exception:
            dead.add(send_fn)
    _call_subscribers -= dead


async def _broadcast_transcript(data):
    """Send transcript segment to subscribers of that call."""
    call_id = data.get("call_id")
    if not call_id:
        return
    subs = _transcript_subscribers.get(call_id, set()).copy()
    msg = json.dumps(data)
    dead = set()
    for send_fn in subs:
        try:
            await send_fn(msg)
        except Exception:
            dead.add(send_fn)
    if call_id in _transcript_subscribers:
        _transcript_subscribers[call_id] -= dead


def setup_event_forwarding(event_bus, loop):
    """Subscribe to EventBus events and forward to WebSocket clients."""

    def on_call_event(data):
        asyncio.run_coroutine_threadsafe(_broadcast_call_event(data), loop)

    def on_transcript(data):
        asyncio.run_coroutine_threadsafe(_broadcast_transcript(data), loop)

    event_bus.subscribe("call.incoming", lambda d: on_call_event({"type": "incoming", **d}))
    event_bus.subscribe("call.bridged", lambda d: on_call_event({"type": "bridged", **d}))
    event_bus.subscribe("call.ended", lambda d: on_call_event({"type": "ended", **d}))
    event_bus.subscribe("operator.status_changed", lambda d: on_call_event({"type": "operator_status", **d}))
    event_bus.subscribe("email.received", lambda d: on_call_event({"type": "email", **d}))
    event_bus.subscribe("transcript.update", on_transcript)


@bp.websocket("/ws/calls")
async def ws_calls():
    """Live call state updates."""
    send_fn = websocket.send

    _call_subscribers.add(send_fn)
    try:
        while True:
            # Keep connection alive, ignore client messages
            await websocket.receive()
    except asyncio.CancelledError:
        pass
    finally:
        _call_subscribers.discard(send_fn)


@bp.websocket("/ws/transcript/<call_id>")
async def ws_transcript(call_id):
    """Live transcription for a specific call."""
    send_fn = websocket.send

    if call_id not in _transcript_subscribers:
        _transcript_subscribers[call_id] = set()
    _transcript_subscribers[call_id].add(send_fn)

    try:
        while True:
            await websocket.receive()
    except asyncio.CancelledError:
        pass
    finally:
        if call_id in _transcript_subscribers:
            _transcript_subscribers[call_id].discard(send_fn)
            if not _transcript_subscribers[call_id]:
                del _transcript_subscribers[call_id]
