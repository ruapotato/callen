# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Active call registry — thread-safe tracking of all live calls.
"""

import logging
import threading

from callen.sip.call import CallenCall

log = logging.getLogger(__name__)


class CallRegistry:
    def __init__(self):
        self._calls: dict[str, CallenCall] = {}
        self._lock = threading.Lock()

    def add(self, call: CallenCall):
        with self._lock:
            self._calls[call.uuid] = call
        log.info("Call registered: %s from %s", call.uuid[:8], call.caller_id)

    def remove(self, call_id: str):
        with self._lock:
            self._calls.pop(call_id, None)
        log.info("Call removed: %s", call_id[:8])

    def get(self, call_id: str) -> CallenCall | None:
        with self._lock:
            return self._calls.get(call_id)

    def active_calls(self) -> list[CallenCall]:
        with self._lock:
            return list(self._calls.values())

    def count(self) -> int:
        with self._lock:
            return len(self._calls)
