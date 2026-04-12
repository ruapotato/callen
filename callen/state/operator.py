# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Operator availability state management.
"""

import enum
import logging
import threading

log = logging.getLogger(__name__)


class OperatorStatus(enum.Enum):
    AVAILABLE = "available"
    BUSY = "busy"
    DND = "dnd"


class OperatorState:
    def __init__(self, event_bus, default_status: str = "available"):
        self._status = OperatorStatus(default_status)
        self._lock = threading.Lock()
        self._event_bus = event_bus
        self._auto_busied = False

    @property
    def status(self) -> OperatorStatus:
        with self._lock:
            return self._status

    def set_status(self, status: OperatorStatus):
        with self._lock:
            old = self._status
            self._status = status
            self._auto_busied = False
        log.info("Operator status: %s -> %s", old.value, status.value)
        self._event_bus.publish("operator.status_changed", {
            "old": old.value,
            "new": status.value,
        })

    def auto_busy(self):
        """Mark busy because operator answered a bridged call."""
        with self._lock:
            if self._status == OperatorStatus.AVAILABLE:
                self._status = OperatorStatus.BUSY
                self._auto_busied = True
                log.info("Operator auto-busy (on call)")

    def auto_available(self):
        """Restore to available after bridged call ends (if was auto-busied)."""
        with self._lock:
            if self._auto_busied:
                self._status = OperatorStatus.AVAILABLE
                self._auto_busied = False
                log.info("Operator auto-available (call ended)")

    @property
    def is_available(self) -> bool:
        return self.status == OperatorStatus.AVAILABLE
