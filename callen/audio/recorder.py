# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Split-channel recording manager for bridged calls."""

import logging
import os
from datetime import datetime

from callen.sip.media import CallRecorder

log = logging.getLogger(__name__)


class SplitChannelRecorder:
    """Manages separate caller and technician recordings for a bridged call."""

    def __init__(self, call_id: str, caller_id: str, output_dir: str):
        self._call_id = call_id
        os.makedirs(output_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.caller_path = os.path.join(
            output_dir, f"{timestamp}_{caller_id}_caller.wav"
        )
        self.tech_path = os.path.join(
            output_dir, f"{timestamp}_{caller_id}_tech.wav"
        )

        self._caller_rec: CallRecorder | None = None
        self._tech_rec: CallRecorder | None = None

    def start(self, caller_media, tech_media):
        """Start recording both channels. Must be called from SIP thread."""
        self._caller_rec = CallRecorder(self.caller_path)
        self._tech_rec = CallRecorder(self.tech_path)
        self._caller_rec.start(caller_media)
        self._tech_rec.start(tech_media)
        log.info("Split recording started for call %s", self._call_id[:8])

    def stop(self, caller_media, tech_media):
        """Stop both recorders. Must be called from SIP thread."""
        if self._caller_rec:
            self._caller_rec.stop(caller_media)
        if self._tech_rec:
            self._tech_rec.stop(tech_media)
        log.info("Split recording stopped for call %s", self._call_id[:8])

    def cleanup(self):
        if self._caller_rec:
            self._caller_rec.cleanup()
        if self._tech_rec:
            self._tech_rec.cleanup()
