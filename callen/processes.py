# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Process scheduler and runner.

Runs registered scripts on cron schedules or on-demand. Captures
output and exit codes in the process_runs table so the dashboard
and agent can see history.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class ProcessRunner:
    """Runs scripts and logs results to the DB."""

    def __init__(self, db, project_root: str = "."):
        self._db = db
        self._root = Path(project_root).resolve()

    def run(self, process_id: str, triggered_by: str = "manual") -> dict:
        """Execute a process synchronously. Returns the run record."""
        proc = self._db.get_process(process_id)
        if not proc:
            raise ValueError(f"process not found: {process_id}")

        script = self._root / proc["script_path"]
        if not script.exists():
            raise FileNotFoundError(f"script not found: {script}")

        started = time.time()
        log.info("Running process %s: %s (triggered by %s)",
                 process_id, script, triggered_by)

        try:
            result = subprocess.run(
                [str(script)],
                capture_output=True, text=True,
                timeout=300, cwd=str(self._root),
            )
            output = result.stdout + result.stderr
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            output = "ERROR: process timed out (5 min limit)"
            exit_code = -1
        except Exception as e:
            output = f"ERROR: {e}"
            exit_code = -1

        run_id = self._db.log_process_run(
            process_id, exit_code, output.strip(),
            started_at=started, triggered_by=triggered_by,
        )

        log.info("Process %s finished: exit=%d, run_id=%d",
                 process_id, exit_code, run_id)

        return {
            "run_id": run_id,
            "process_id": process_id,
            "exit_code": exit_code,
            "output": output.strip(),
            "triggered_by": triggered_by,
        }


class ProcessScheduler:
    """Background thread that checks cron schedules every minute."""

    def __init__(self, runner: ProcessRunner, db):
        self._runner = runner
        self._db = db
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="process-scheduler", daemon=True,
        )
        self._thread.start()
        log.info("Process scheduler started")

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._check_schedules()
            except Exception:
                log.exception("Scheduler error")
            # Sleep 60s, checking for stop every second
            for _ in range(60):
                if not self._running:
                    return
                time.sleep(1)

    def _check_schedules(self):
        now = time.localtime()
        for proc in self._db.get_scheduled_processes():
            if self._cron_matches(proc["cron_schedule"], now):
                try:
                    self._runner.run(proc["id"], triggered_by="scheduler")
                except Exception:
                    log.exception("Scheduled process %s failed", proc["id"])

    @staticmethod
    def _cron_matches(expr: str, t: time.struct_time) -> bool:
        """Simple 5-field cron match: M H DoM Mon DoW."""
        try:
            fields = expr.strip().split()
            if len(fields) != 5:
                return False
            minute, hour, dom, month, dow = fields
            return (
                _field_matches(minute, t.tm_min) and
                _field_matches(hour, t.tm_hour) and
                _field_matches(dom, t.tm_mday) and
                _field_matches(month, t.tm_mon) and
                _field_matches(dow, t.tm_wday)  # 0=Monday in Python
            )
        except Exception:
            return False


def _field_matches(field: str, value: int) -> bool:
    """Match a single cron field against a value."""
    if field == "*":
        return True
    # Handle */N
    if field.startswith("*/"):
        step = int(field[2:])
        return value % step == 0
    # Handle comma-separated values
    for part in field.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            if int(lo) <= value <= int(hi):
                return True
        elif int(part) == value:
            return True
    return False
