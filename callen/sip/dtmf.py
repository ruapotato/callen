# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
DTMF collection — reads digits from a CallenCall's queue with timeout support.
"""

import logging
import queue

from callen.sip.call import CallenCall, CallState

log = logging.getLogger(__name__)


def collect_dtmf(call: CallenCall, count: int = 1, timeout: float | None = None) -> str | None:
    """
    Block until `count` DTMF digits are received or timeout expires.

    Returns the digits as a string, or None on timeout.
    Returns None immediately if the call is disconnected.
    """
    digits = []
    remaining = timeout

    for _ in range(count):
        if call.state == CallState.DISCONNECTED:
            return None

        try:
            digit = call.dtmf_queue.get(timeout=remaining)
        except queue.Empty:
            return None

        # None is pushed on disconnect
        if digit is None:
            return None

        digits.append(digit)

    return "".join(digits)
