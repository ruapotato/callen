# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
EventBus — simple pub/sub for cross-component communication.

Bridges pjsua2/IVR threads to the asyncio web layer via call_soon_threadsafe.
"""

import asyncio
import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = {}
        self._async_subscribers: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the asyncio event loop for async event delivery."""
        self._loop = loop

    def subscribe(self, event_type: str, callback: Callable):
        """Subscribe a synchronous callback. Called on the publisher's thread."""
        with self._lock:
            self._subscribers.setdefault(event_type, []).append(callback)

    def subscribe_async(self, event_type: str, callback: Callable):
        """Subscribe an async-safe callback. Delivered via call_soon_threadsafe."""
        with self._lock:
            self._async_subscribers.setdefault(event_type, []).append(callback)

    def unsubscribe(self, event_type: str, callback: Callable):
        with self._lock:
            for store in (self._subscribers, self._async_subscribers):
                if event_type in store and callback in store[event_type]:
                    store[event_type].remove(callback)

    def publish(self, event_type: str, data: Any = None):
        """Publish an event. Sync subscribers called immediately, async queued."""
        with self._lock:
            sync_subs = list(self._subscribers.get(event_type, []))
            async_subs = list(self._async_subscribers.get(event_type, []))

        for cb in sync_subs:
            try:
                cb(data)
            except Exception:
                log.exception("Event handler error for %s", event_type)

        if async_subs and self._loop:
            for cb in async_subs:
                try:
                    self._loop.call_soon_threadsafe(cb, data)
                except Exception:
                    log.exception("Async event delivery error for %s", event_type)
