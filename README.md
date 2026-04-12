# Callen

GPL3 IVR / CRM / ticketing system for desktop Linux. Originally a Librem 5
phone IVR; now a full support console for [freesoftware.support](https://freesoftware.support)
that combines SIP trunking, live transcription, a unified ticket queue, and
an always-on AI agent you drive from a single prompt bar.

## What it is

You point a SIP trunk at Callen and an IMAP mailbox at Callen. When someone
calls in or emails in, they become an **incident** (INC-NNNN) attached to a
**contact** (CON-NNNN). Every touchpoint — the call, the transcript, the
email thread, internal notes, todos, status changes — lives on the
incident's timeline. A persistent web dashboard shows the queue, live
transcripts as calls happen, and a bottom prompt bar you can use to talk
to a Claude-Code-headless agent that has tool access to everything.

Calls can be bridged to your cell phone with split-channel recording and
live Parakeet transcription on both legs, technician-first outbound dials
for callbacks (your phone rings first, you confirm with 1, then Callen
calls the contact), and autonomous agent reviews fire after every bridged
call and every voicemail — the agent reads the transcript, updates the
subject, adds a summary note, and extracts concrete action items as todos
you can check off.

## Features

**Voice / telephony**
- SIP registration with VoIP.ms (or any standard SIP trunk)
- Inbound call answering with a hot-reloadable IVR script (`IVR.py`)
- Explicit recording-consent flow; repeat callers skip the gate but hear
  a reminder
- Neural TTS via [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M),
  kept loaded in memory for instant synthesis (or fall back to espeak-ng)
- DTMF detection with instant playback interrupt
- Call bridging to operator's cell phone — bidirectional, split-channel
  recording, live transcription on both legs
- Technician-first outbound bridging — Callen dials your cell first, you
  press 1 to confirm, then it dials the contact and connects the two
- Ring timeout so callers land in Callen's voicemail instead of your cell
  carrier's
- Voicemail recording with post-capture transcription via Parakeet

**Transcription**
- NVIDIA [Parakeet-TDT 0.6B v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)
  kept loaded in memory (GPU or CPU)
- WebRTC VAD-based utterance segmentation (natural pauses, 15s hard cap)
- Silence / low-energy gate to suppress "okay / yeah / um" hallucinations
- Split-channel transcripts: caller and technician separately identified
- Real-time in the dashboard, aggregated per-incident across multiple calls

**Email triage queue**
- IMAP poller pulls from the monitored mailbox every 30 seconds
- Deterministic threading via In-Reply-To headers AND `[INC-NNNN]` subject
  tags
- Auto-classification at ingest:
  - `attached` — deterministic thread match → routed to existing incident
  - `flagged` — prompt-injection pattern scan triggered → operator review
  - `rejected` — marketing / bulk (List-Unsubscribe, Precedence: bulk,
    mailer-daemon, etc.)
  - `pending` — everything else, waiting for agent triage
- Unrouted email never auto-creates an incident — an agent (or the
  operator) decides via `./tools/assign-email` whether it's a real
  support request
- Soft-reject keeps an audit trail; `mark-safe` moves flagged or
  rejected emails back to pending

**Contacts & incidents**
- Contacts identified by phone numbers (normalized to E.164-ish digits)
  and email addresses
- Consent tracked per contact per channel (phone / email) with source and
  timestamp
- Incidents carry subject, status (open / in_progress / waiting /
  resolved / closed), priority, labels, contact, and a structured timeline
- Merge contacts and merge incidents for deduplication
- Todos: a first-class checklist per incident, with the agent extracting
  concrete action items from call transcripts and the operator ticking
  them off in the dashboard

**Agent tool API (this is the primary interface)**
- Bash scripts in `tools/` that wrap a single Python CLI
- The web dashboard's prompt bar sends prompts to Claude Code headless
  (`claude -p --add-dir .`) with a detailed system prompt listing every
  available tool
- Conversation continuity via `claude --continue` between prompts, with
  a "New chat" button to reset
- Autonomous runs fire after bridged calls and voicemails with a fresh
  session so they don't interfere with the operator's interactive chat
- The prompt preamble includes a live snapshot of the focused incident
  (subject, status, contact, last 15 transcript lines, open todos) so
  the agent can answer simple questions without a tool call

**Web dashboard**
- Three-panel workspace: queue, incident detail, contact context
- Sticky LIVE strip at the top of the queue — active calls visible
  regardless of which tab you're on, with a pulsing indicator
- Unified incident timeline with inline split-channel transcripts,
  emails, notes, status changes, todos, consent events
- Cold-call any contact by clicking a phone number → fires the
  technician-first bridging flow
- Create contacts from the UI
- Bottom prompt bar with Ctrl+K focus, streaming agent output in a
  floating drawer with live tool calls and results
- Dark theme, vanilla JS SPA (no framework)

## Architecture

```
callen/
├── sip/             pjsua2 wrappers — endpoint, account, call, media,
│                    bridge, DTMF, thread-safe SIPCommandQueue
├── ivr/             IVR script runner, injected API, outbound bridging
├── audio/           Resampler (8kHz → 16kHz), chunked buffer,
│                    split-channel recorder
├── transcription/   Parakeet processor, per-channel stream with VAD,
│                    voicemail post-processor
├── tts/             TTS engine abstraction (kokoro default, espeak fallback)
├── agent/           Claude-code headless runner with system prompt
├── notify/          IMAP poller + email processor + SMTP sender
├── web/             Quart server, REST API, WebSocket, static SPA
├── state/           Operator availability, call registry, event bus
├── storage/         SQLite schema + migrations + queries
├── cli.py           Python CLI backing the tools/ wrappers
└── app.py           Top-level orchestrator
tools/               Bash command wrappers (35+ commands)
IVR.py               Hot-reloaded IVR script (the file you edit)
```

**Threading**: pjsua2 runs with `threadCnt=0` on a single dedicated SIP
poll thread. IVR scripts run in per-call worker threads and submit
pjsua2 operations via the SIPCommandQueue. Each IVR thread registers
with `libRegisterThread` so pjsua2 object destruction is safe. The
Quart web server runs on its own asyncio loop; events from SIP/IVR
threads reach WebSocket clients via `asyncio.run_coroutine_threadsafe`.
The agent runner spawns `claude` as a subprocess and streams its
`stream-json` output back over WebSocket.

## Quick start

### 1. System packages

```sh
sudo apt install espeak-ng sox build-essential python3-dev swig \
                 libasound2-dev uuid-dev libssl-dev
```

### 2. Build pjproject from source (one-time, ~5 minutes)

The pip `pjsua2-pybind11` package is missing `AudioMediaPlayer`,
`AudioMediaRecorder`, and `AudioMediaPort`. Callen needs the SWIG
bindings built from pjproject source.

```sh
git clone --branch 2.14.1 https://github.com/pjsip/pjproject.git ~/pjproject
cd ~/pjproject
./configure --enable-shared
make dep && make
sudo make install
sudo ldconfig

cd pjsip-apps/src/swig/python
make
python3 setup.py install --user
```

Verify:

```sh
python3 -c "import pjsua2; print(pjsua2.AudioMediaPlayer)"
# <class 'pjsua2.AudioMediaPlayer'>
```

### 3. Python dependencies

```sh
pip install -r requirements.txt
```

Pulls in NeMo + PyTorch + Kokoro TTS + Quart (~3GB on first install).
A CUDA GPU is strongly recommended for live transcription. Kokoro runs
happily on CPU too.

### 4. Claude Code CLI

The agent runner shells out to `claude` in headless mode. Install the
Claude Code CLI and sign in once with your Anthropic account:

```sh
# See https://docs.claude.com/en/docs/claude-code for installation
claude --version
```

Callen will call `claude -p --add-dir .` for every agent prompt, so no
API key is needed — it uses your Claude account session.

### 5. Configure

```sh
cp config.toml.example config.toml
$EDITOR config.toml
```

**Critical VoIP.ms setup notes:**

- **`registrar` and `domain` must be the POP-specific server** for your
  DID (e.g. `seattle1.voip.ms`). Using the generic `sip.voip.ms` returns
  403 Forbidden.
- **`username` is your VoIP.ms account number**, NOT the DID.
- **`password` is the SIP/IAX password** set in the portal.
- **`cell_phone` is bare E.164 digits**, no `+`.

`config.toml` is gitignored — credentials never get committed.

### 6. Run

```sh
python3 -m callen
```

Expected startup:

```
INFO  Starting Callen IVR system
INFO  Loading Kokoro TTS model (hexgrad/Kokoro-82M)...
INFO  Kokoro model ready (voice=af_heart)
INFO  TTS engine ready: kokoro
INFO  Loading Parakeet model (this takes a moment on first run)...
INFO  Parakeet model loaded successfully
INFO  Transcription enabled
INFO  pjsua2 endpoint started on port 5060
INFO  SIP account created: sip:NNNNNN@<pop>.voip.ms
INFO  IMAP poller started for imap.example.com (every 30s)
INFO  Callen IVR running — waiting for calls
INFO  Web dashboard: http://127.0.0.1:8080
INFO  SIP registered (expires: 295s)
```

Open the dashboard at <http://127.0.0.1:8080> and call your DID.

## Customizing the IVR

`IVR.py` defines the inbound call flow. Edit it freely — Callen
**hot-reloads the script on every inbound call** so edits take effect
without a restart. The injected API:

| Function                                              | Description |
|---|---|
| `say(call, text, repeat=True)`                        | TTS via the configured engine, interrupted by DTMF |
| `play(call, wav_path)`                                | Play a pre-recorded WAV |
| `dtmf(call, count=1, timeout=10)`                     | Wait for digits |
| `bridge_to_operator(call)`                            | Forward to operator's cell (handles availability + ring timeout) |
| `record_voicemail(call, prompt=None)`                 | Voicemail with `#` to end |
| `hangup(call)`                                        | End the call |
| `caller_id(call)`                                     | Caller's phone number |
| `operator_available()`                                | True if operator is available |
| `has_consented(call)`                                 | True if this phone number already consented on a prior call |

## TTS engines

Configure in `config.toml` `[tts]` section:

```toml
[tts]
engine = "kokoro"  # "kokoro" (default, neural) or "espeak" (fast fallback)
voice = "af_heart"
lang_code = "a"    # 'a' = American English, 'b' = British
device = ""        # "", "cpu", or "cuda"
```

Kokoro loads the ~82M-parameter neural model into memory at startup and
keeps it hot, so every `say()` is fast (~0.3s per sentence on CPU, much
faster on GPU). Output is automatically down-sampled to 8 kHz mono for
pjsua2 compatibility.

Espeak is kept as a fallback — it needs no model, runs via subprocess,
and sounds robotic but works everywhere.

## Agent tool API (`tools/`)

Callen is designed to be driven by an AI agent. The dashboard prompt bar
spawns Claude Code headless with tool access; the agent can also be
invoked directly from the terminal for scripting. Every command outputs
JSON on stdout by default; pass `--pretty` for human-readable.

```sh
# Tickets / incidents
./tools/list-incidents [--status open] [--contact CON-0001]
./tools/get-incident INC-0042
./tools/update-incident INC-0042 --status resolved --priority high \
                                 --subject "..." --add-label billing
./tools/note-incident INC-0042 "Internal note"
./tools/create-incident --phone 15551234567 --subject "Callback request"
./tools/merge-incidents INC-0043 INC-0042

# Todos
./tools/list-todos INC-0042
./tools/add-todo INC-0042 "Drive to 5231 Alpine St and install GPU"
./tools/complete-todo 17
./tools/uncomplete-todo 17
./tools/update-todo 17 "Updated text"
./tools/delete-todo 17

# Contacts
./tools/list-contacts
./tools/get-contact CON-0007
./tools/create-contact --name "Jane" --phone 15551234567 \
                       --email jane@example.com
./tools/update-contact CON-0007 --name "Jane" --notes "Prefers email"
./tools/add-phone CON-0007 15555550123
./tools/add-email CON-0007 alt@example.com
./tools/contact-consent CON-0007 --phone 15551234567 --source manual
./tools/merge-contacts CON-0008 CON-0007

# Transcripts and audio
./tools/get-transcript --incident INC-0042 --text
./tools/get-transcript --call <uuid>
./tools/get-audio --incident INC-0042 --channel caller --out /tmp/call.wav
./tools/get-audio --incident INC-0042 --channel tech
./tools/get-audio --incident INC-0042 --channel voicemail

# Calls
./tools/list-calls
./tools/originate INC-0042                           # callback via cell
./tools/originate INC-0042 --destination 15551234567 --display-name "Jane"

# Email triage queue
./tools/list-pending-emails                   # agent triage queue
./tools/list-flagged-emails                   # security review queue
./tools/list-rejected-emails                  # audit of filtered mail
./tools/get-email 42
./tools/assign-email 42 --incident INC-0042          # thread to existing
./tools/assign-email 42 --create-incident --subject "..." --priority high
./tools/reject-email 42 --reason "marketing"         # soft-reject
./tools/mark-safe 42                                 # flagged/rejected -> pending
./tools/send-email INC-0042 --body "Reply" --to ...

# Operator state
./tools/get-operator-status
./tools/set-operator-status {available|busy|dnd}

# Search
./tools/search "jane"
./tools/search 15555550123
./tools/search "router"
```

**Typical heartbeat flow during or after a call:**

```sh
./tools/list-incidents --status open --limit 1    # find the active ticket
./tools/get-incident INC-0042                     # full context
./tools/get-transcript --incident INC-0042 --text # latest transcript
./tools/update-incident INC-0042 --subject "..."
./tools/add-todo INC-0042 "Drive to address and install GPU"
./tools/note-incident INC-0042 "Customer agreed to $X by Y date"
```

Every write command logs a timeline entry so the agent can re-run
without duplicating state.

## Web dashboard API

For custom integrations or your own frontends:

```
GET    /api/incidents                 Active incidents
GET    /api/incidents/<id>            Full detail with timeline, todos
PATCH  /api/incidents/<id>            status / priority / subject / labels
POST   /api/incidents/<id>/notes      Add a note
GET    /api/incidents/<id>/todos      List todos
POST   /api/incidents/<id>/todos      Add a todo
PATCH  /api/todos/<id>                Toggle done / update text
DELETE /api/todos/<id>

GET    /api/contacts                  List
GET    /api/contacts/<id>             Detail with phones/emails/incidents
POST   /api/contacts                  Create

GET    /api/calls                     Active calls
GET    /api/history                   Call history
GET    /api/recordings/<id>/<channel> Download a call recording

GET    /api/emails?status=pending     Triage queue (status = pending|flagged|rejected|attached)
GET    /api/emails/<id>               Full message

GET    /api/transcripts/<call_id>     Segments
POST   /api/call/originate            Technician-first outbound

GET    /api/operator/status
PUT    /api/operator/status

POST   /api/agent                     Start an agent run {prompt, context}
GET    /api/agent/runs                Recent runs
GET    /api/agent/runs/<id>           Full run with events
POST   /api/agent/reset               Clear conversation state
GET    /api/agent/state               {continuing, turn}

WS     /ws/calls                      Live call events + transcript nudges
WS     /ws/transcript/<call_id>       Per-call transcript stream
WS     /ws/agent                      Global agent run lifecycle events
WS     /ws/agent/<run_id>             Per-run stream-json events
```

## Troubleshooting

**403 Forbidden on SIP registration**  
You're using `sip.voip.ms` instead of your DID's POP server. Set
`registrar = "sip:seattle1.voip.ms"` (or your POP) in `config.toml`.

**Audio cuts out during pauses**  
`medConfig.noVad = True` is already set in `callen/sip/endpoint.py`. If
you still see drops, increase the jitter buffer (`jbInit`, `jbMax`) in
the same file.

**Cell carrier voicemail picks up before Callen's**  
Lower `RING_TIMEOUT` in `callen/ivr/api.py` (default 18s). US carriers
typically roll to voicemail at 20-25s.

**Kokoro fails to load**  
The TTS factory automatically falls back to espeak-ng. Check
`pip show kokoro torch` and ensure the cache at
`~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/` is accessible.
Set `engine = "espeak"` in `config.toml` to skip Kokoro entirely.

**Agent runs return immediately without doing anything**  
Check that `claude` is on PATH and authenticated:
`which claude && claude --version`. The runner pre-approves
`Bash(./tools/*)`, `Read`, `Glob`, and `Grep` via `--allowedTools`, so
the agent doesn't need interactive permission prompts.

**"database is locked"**  
Should not happen — SQLite is configured with WAL mode and a 10s busy
timeout. Write methods explicitly rollback on error. If you see it,
your filesystem may not support proper locking.

## License

```
Copyright (C) 2020 David Hamner

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
```
