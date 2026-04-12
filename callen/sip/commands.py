# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
SIPCommandQueue — thread-safe bridge between IVR threads and the pjsua2 poll thread.

IVR scripts run in their own threads but pjsua2 objects can only be manipulated
from the thread that runs libHandleEvents(). This queue lets IVR threads submit
callables that the poll thread executes, returning results via Futures.
"""

import logging
import queue
from concurrent.futures import Future
from typing import Any, Callable

log = logging.getLogger(__name__)


class SIPCommandQueue:
    def __init__(self):
        self._queue: queue.Queue[tuple[Callable, tuple, dict, Future]] = queue.Queue()

    def submit(self, fn: Callable, *args: Any, **kwargs: Any) -> Future:
        """Submit a callable to be executed on the SIP thread. Returns a Future."""
        fut = Future()
        self._queue.put((fn, args, kwargs, fut))
        return fut

    def process_pending(self):
        """Called from the pjsua2 poll loop. Executes all queued commands."""
        while True:
            try:
                fn, args, kwargs, fut = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                result = fn(*args, **kwargs)
                fut.set_result(result)
            except Exception as exc:
                log.exception("SIP command failed: %s", fn)
                fut.set_exception(exc)
