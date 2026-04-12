# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Agent runner — spawns the `claude` CLI in headless mode for one-shot
prompts and streams the output back.

The runner is stateless: every prompt is a fresh claude subprocess with
the Callen project folder added via --add-dir, the Callen system prompt
appended, and --output-format stream-json so we can push output events
to the frontend as they arrive.

Because we use the user's installed claude CLI (which is authenticated
against their account), we don't need an ANTHROPIC_API_KEY — the
subprocess inherits the user's claude auth.
"""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SYSTEM_PROMPT_FILE = Path(__file__).resolve().parent / "system_prompt.md"


@dataclass
class AgentRun:
    """Metadata about one agent invocation."""
    run_id: str
    prompt: str
    context: dict
    started_at: float
    status: str = "pending"        # pending / running / done / error
    events: list[dict] = field(default_factory=list)
    result_text: str = ""
    error: str = ""
    ended_at: float | None = None


class AgentRunner:
    """Spawns `claude` processes and broadcasts their stream-json events.

    Keeps an in-memory log of recent runs so the UI can list them.
    WebSocket subscribers get live events; late subscribers can fetch
    a replay via get_run.

    Conversation continuity: claude's `--continue` flag resumes the most
    recent conversation in the current directory. After the first successful
    run we set _continue_next so that every subsequent prompt appends to the
    same session. Call reset_conversation() to start fresh.
    """

    def __init__(self, max_runs: int = 50, claude_bin: str | None = None,
                 db=None):
        self._runs: dict[str, AgentRun] = {}
        self._order: list[str] = []
        self._max_runs = max_runs
        self._claude_bin = claude_bin or shutil.which("claude") or "claude"
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        self._continue_next = False
        self._conversation_turn = 0
        # Optional DB handle — used to inject a snapshot of the currently
        # focused item (incident/contact/email) into the prompt preamble
        # so the agent doesn't have to make a tool call just to see what
        # the operator is looking at.
        self._db = db

    def system_prompt(self) -> str:
        try:
            return SYSTEM_PROMPT_FILE.read_text()
        except OSError:
            log.warning("System prompt file missing at %s", SYSTEM_PROMPT_FILE)
            return ""

    def _build_user_prompt(self, prompt: str, context: dict) -> str:
        """Prepend a context hint + a snapshot of the focused item so the
        agent can answer simple questions without a tool round-trip."""
        if not context:
            return prompt

        parts = ["[Callen dashboard context]"]
        for key in ("incident_id", "contact_id", "call_id", "email_id", "view"):
            val = context.get(key)
            if val:
                parts.append(f"- {key}: {val}")

        # If we have a DB handle, pull a small snapshot of whatever the
        # operator is currently looking at.
        snapshot = self._focus_snapshot(context)
        if snapshot:
            parts.append("")
            parts.append("[currently on screen]")
            parts.append(snapshot)

        parts.append("")
        parts.append(prompt)
        return "\n".join(parts)

    def _focus_snapshot(self, context: dict) -> str:
        """Return a short human-readable description of the item the
        operator currently has focused. Empty string if nothing useful."""
        if self._db is None:
            return ""

        try:
            incident_id = context.get("incident_id")
            if incident_id:
                inc = self._db.get_incident(incident_id)
                if not inc:
                    return ""
                lines = [
                    f"Incident: {inc['id']}",
                    f"Subject: {inc.get('subject') or '(none)'}",
                    f"Status: {inc.get('status')}  Priority: {inc.get('priority')}",
                ]
                labels = inc.get("labels") or []
                if labels:
                    lines.append(f"Labels: {', '.join(labels)}")

                if inc.get("contact_id"):
                    contact = self._db.get_contact(inc["contact_id"])
                    if contact:
                        name = contact.get("display_name") or "(unnamed)"
                        phones = ", ".join(p["e164"] for p in contact.get("phones") or [])
                        emails = ", ".join(e["address"] for e in contact.get("emails") or [])
                        lines.append(f"Contact: {name} ({contact['id']})")
                        if phones:
                            lines.append(f"  phones: {phones}")
                        if emails:
                            lines.append(f"  emails: {emails}")

                # Most recent few timeline entries
                entries = self._db.list_incident_entries(incident_id)
                if entries:
                    lines.append("Recent timeline:")
                    for e in entries[-5:]:
                        t = e.get("type", "?")
                        payload = e.get("payload") or {}
                        if t == "note":
                            text = (payload.get("text") or "").strip()[:140]
                            lines.append(f"  - note: {text}")
                        elif t == "call":
                            direction = payload.get("direction", "inbound")
                            lines.append(f"  - {direction} call")
                        elif t == "email":
                            lines.append(f"  - email from {payload.get('from','?')}: {payload.get('subject','')[:80]}")
                        else:
                            lines.append(f"  - {t}")

                return "\n".join(lines)

            contact_id = context.get("contact_id")
            if contact_id:
                contact = self._db.get_contact(contact_id)
                if not contact:
                    return ""
                name = contact.get("display_name") or "(unnamed)"
                phones = ", ".join(p["e164"] for p in contact.get("phones") or [])
                emails = ", ".join(e["address"] for e in contact.get("emails") or [])
                lines = [f"Contact: {name} ({contact['id']})"]
                if phones:
                    lines.append(f"phones: {phones}")
                if emails:
                    lines.append(f"emails: {emails}")
                return "\n".join(lines)

            email_id = context.get("email_id")
            if email_id:
                em = self._db.get_email(email_id)
                if not em:
                    return ""
                body = (em.get("body_text") or "").strip()[:400]
                return (
                    f"Email #{em['id']} [{em.get('status')}]\n"
                    f"From: {em.get('from_addr')}\n"
                    f"Subject: {em.get('subject')}\n"
                    f"Preview: {body}"
                )
        except Exception:
            log.exception("Failed to build focus snapshot")
            return ""

        return ""

    async def start(
        self,
        prompt: str,
        context: dict | None = None,
        autonomous: bool = False,
    ) -> AgentRun:
        """Launch a claude subprocess for this prompt. Returns immediately;
        events stream in via the run's queue.

        If autonomous=True, this run is a system-triggered review (e.g. a
        voicemail post-processor) and will NOT read or modify the operator's
        interactive conversation state. It always starts a fresh session
        and never arms continuation.
        """
        run_id = uuid.uuid4().hex[:12]
        run = AgentRun(
            run_id=run_id,
            prompt=prompt,
            context=context or {},
            started_at=time.time(),
            status="running",
        )
        async with self._lock:
            self._runs[run_id] = run
            self._order.append(run_id)
            if autonomous:
                # Autonomous runs never continue a conversation and never
                # touch _continue_next so the operator's session is safe.
                run.context = {**run.context, "_continues": False,
                               "_turn": 0, "_autonomous": True}
            else:
                # Interactive operator run — follow the continuation state
                run.context = {**run.context, "_continues": self._continue_next,
                               "_turn": self._conversation_turn + 1}
            # Trim to max_runs
            while len(self._order) > self._max_runs:
                old = self._order.pop(0)
                self._runs.pop(old, None)

        # Spawn in a task so the caller can return the run_id immediately
        asyncio.create_task(self._execute(run))
        return run

    def reset_conversation(self):
        """Clear the conversation state — the next prompt starts a new
        claude session instead of continuing the previous one."""
        self._continue_next = False
        self._conversation_turn = 0
        log.info("Agent conversation reset")

    async def _execute(self, run: AgentRun):
        """Run the claude subprocess and pump events into the run + subscribers."""
        system_prompt = self.system_prompt()
        user_prompt = self._build_user_prompt(run.prompt, run.context)

        # Pre-approve the specific tools the agent needs. We allow Bash for
        # any ./tools/* invocation (read-only AND write — the system prompt
        # tells the agent what's safe) plus Read for context files. The
        # agent still has to choose which commands to run; this just
        # removes the human-in-the-loop permission prompts.
        allowed_tools = [
            "Bash(./tools/*)",
            "Read",
            "Glob",
            "Grep",
        ]

        cmd = [
            self._claude_bin,
            "-p",
            "--add-dir", str(PROJECT_ROOT),
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--allowedTools", *allowed_tools,
        ]

        # Continue the previous conversation if we have one. claude's
        # --continue picks up the most recent session in the working dir,
        # so the system prompt is already in context and we don't need
        # to resend it — but re-appending is harmless.
        continues = run.context.get("_continues", False)
        if continues:
            cmd.append("--continue")

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        # Final positional: the user prompt
        cmd.append(user_prompt)

        log.info("Agent run %s: launching claude (%d tokens in prompt)",
                 run.run_id, len(user_prompt.split()))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
            )
        except FileNotFoundError:
            run.status = "error"
            run.error = f"claude CLI not found at {self._claude_bin}"
            run.ended_at = time.time()
            await self._broadcast(run.run_id, {
                "type": "error", "message": run.error,
            })
            return
        except Exception as e:
            run.status = "error"
            run.error = f"failed to spawn claude: {e}"
            run.ended_at = time.time()
            await self._broadcast(run.run_id, {
                "type": "error", "message": run.error,
            })
            return

        # Pump stdout line by line, parsing each as JSON
        async def _pump_stdout():
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    event = {"type": "raw", "text": text}
                run.events.append(event)
                await self._broadcast(run.run_id, event)

                # Capture the final "result" assistant text for convenience
                if isinstance(event, dict):
                    if event.get("type") == "result":
                        run.result_text = event.get("result", "") or run.result_text
                    elif event.get("type") == "assistant":
                        msg = event.get("message", {})
                        for block in msg.get("content", []) or []:
                            if isinstance(block, dict) and block.get("type") == "text":
                                t = block.get("text", "")
                                if t:
                                    run.result_text = t

        # Pump stderr into the run.error buffer
        async def _pump_stderr():
            assert proc.stderr is not None
            chunks = []
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                chunks.append(line.decode("utf-8", errors="replace"))
            if chunks:
                run.error = "".join(chunks)

        try:
            await asyncio.gather(_pump_stdout(), _pump_stderr())
            await proc.wait()
            if proc.returncode == 0:
                run.status = "done"
            else:
                run.status = "error"
                if not run.error:
                    run.error = f"claude exited with code {proc.returncode}"
        except Exception as e:
            run.status = "error"
            run.error = f"runner error: {e}"
            log.exception("Agent run %s crashed", run.run_id)
        finally:
            run.ended_at = time.time()
            # Arm continuation for the next prompt only if this run was an
            # interactive operator run AND it succeeded. Autonomous runs
            # (e.g. voicemail reviews) never touch the operator's session.
            if run.status == "done" and not run.context.get("_autonomous"):
                self._continue_next = True
                self._conversation_turn += 1
            await self._broadcast(run.run_id, {
                "type": "complete",
                "status": run.status,
                "result": run.result_text,
                "error": run.error,
                "turn": self._conversation_turn,
                "continues": self._continue_next,
            })
            log.info("Agent run %s finished: %s (turn=%d)",
                     run.run_id, run.status, self._conversation_turn)

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        """Get a queue that receives live events for a run.
        Also replays past events so late subscribers don't miss anything.
        """
        async with self._lock:
            q = asyncio.Queue()
            self._subscribers.setdefault(run_id, set()).add(q)
            run = self._runs.get(run_id)
            if run:
                # Replay any events already collected
                for ev in run.events:
                    await q.put(ev)
                if run.status in ("done", "error"):
                    await q.put({
                        "type": "complete",
                        "status": run.status,
                        "result": run.result_text,
                        "error": run.error,
                    })
        return q

    async def unsubscribe(self, run_id: str, queue: asyncio.Queue):
        async with self._lock:
            if run_id in self._subscribers:
                self._subscribers[run_id].discard(queue)
                if not self._subscribers[run_id]:
                    del self._subscribers[run_id]

    async def _broadcast(self, run_id: str, event: dict):
        subs = set()
        async with self._lock:
            subs = set(self._subscribers.get(run_id, set()))
        for q in subs:
            try:
                await q.put(event)
            except Exception:
                pass

    def get_run(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 20) -> list[dict]:
        out = []
        for rid in reversed(self._order[-limit:]):
            run = self._runs.get(rid)
            if run is None:
                continue
            out.append({
                "run_id": run.run_id,
                "prompt": run.prompt,
                "context": run.context,
                "status": run.status,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
                "result": run.result_text,
                "error": run.error,
                "event_count": len(run.events),
            })
        return out
