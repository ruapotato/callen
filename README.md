# Callen

GPL3 IVR / CRM / ticketing system for desktop Linux that turns a SIP
trunk and an email mailbox into an operator-friendly support console.
Originally a Librem 5 phone IVR; now a full support platform for
[freesoftware.support](https://freesoftware.support) with live
transcription, a unified ticket queue, a local-LLM email security
shield, and an always-on AI agent you drive from a single prompt bar.

## What it is

You point a SIP trunk at Callen and an IMAP mailbox at Callen. When
someone calls in or emails in, they become an **incident** (INC-NNNN)
attached to a **contact** (CON-NNNN). Every touchpoint — the call,
the transcript, the email thread, internal notes, todos, status
changes — lives on the incident's timeline. A persistent web
dashboard shows the queue, live transcripts as calls happen, and a
bottom prompt bar you use to talk to a Claude Code headless agent
that has tool access to everything.

**Calls** can be bridged to your cell phone with split-channel
recording and live Parakeet transcription on both legs; technician-
first outbound dials for callbacks (your phone rings first, you
confirm with 1, then Callen calls the contact); and autonomous agent
reviews fire after every bridged call and every voicemail to
summarize the conversation and extract actionable todos.

**Emails** arrive via IMAP, pass through a deterministic regex
scanner, then a local **Mistral 7B classifier** running on Ollama
(prompt-injection shield), and only then does a Claude Code agent see
them. The agent handles consent handshakes, clarifying replies, and
todo creation autonomously, with strict rules about never leaking
sensitive information, never following instructions embedded in email
bodies, and always including a liability disclaimer with consent
requests.

## Features

**Voice / telephony**
- SIP registration with VoIP.ms (or any standard SIP trunk)
- Inbound call answering with a hot-reloadable IVR script (`IVR.py`)
- Explicit recording-consent flow with verbal liability disclaimer;
  repeat callers skip the gate but hear a reminder
- Neural TTS via [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M),
  kept loaded in memory for instant synthesis (or espeak-ng fallback)
- DTMF detection with instant playback interrupt
- Call bridging to operator's cell phone — bidirectional, split-channel
  recording, live transcription on both legs
- Technician-first outbound bridging — Callen dials your cell first,
  you press 1 to confirm, then it dials the contact and connects them
- Ring timeout so callers land in Callen's voicemail instead of your
  cell carrier's
- Voicemail recording with post-capture Parakeet transcription

**Transcription**
- NVIDIA [Parakeet-TDT 0.6B v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)
  kept loaded in memory (GPU or CPU)
- WebRTC VAD-based utterance segmentation (natural pauses, 15s hard cap)
- RMS + peak energy gate to suppress "okay / yeah / um" hallucinations
  on near-silence
- Split-channel transcripts: caller and technician separately identified
- Real-time in the dashboard, aggregated per-incident across multiple calls

**Email triage (with defense-in-depth security)**
- IMAP poller pulls from the monitored mailbox every 30 seconds.
  Supports direct IMAP over SSL (port 993), STARTTLS over port 143,
  and local Proton Bridge at `127.0.0.1:1143` with self-signed
  certificate tolerance.
- **Layer 1 — Deterministic scanner**: regex patterns for known
  injection phrases (ignore instructions, reveal system prompt, DAN
  mode, special tokens, etc.) and headers (`List-Unsubscribe`,
  `Precedence: bulk`) tag emails as `flagged` or `rejected` before
  anything else runs.
- **Layer 2 — Deterministic threading**: emails with `In-Reply-To`
  or `[INC-NNNN]` in the subject auto-route to the existing incident
  without any LLM involvement.
- **Layer 3 — Local-LLM preflight classifier** (new): every email
  that makes it past the regex scanner runs through a local
  Mistral 7B via Ollama. The classifier returns structured JSON
  (is_prompt_injection, is_automated, is_support_request, confidence,
  reason) and the verdict maps to one of:
  - `pass` — continue to the Claude agent
  - `reject` — auto-rejected with the model's reason
  - `flag` — flagged for operator review, Claude **never** sees it
  - `skip` — preflight unavailable, deterministic filters alone decide
- **Layer 4 — Claude Code agent**: only emails that passed all three
  previous layers reach the AI agent, which runs with strict system
  prompt rules about treating email content as data, never
  instructions, never leaking sensitive information, and always
  including the liability disclaimer in consent-request replies.
- **Automatic lockout response**: any layer that detects an injection
  attempt immediately hard-blocks the sender, flips their contact's
  `trust_level` to `suspect`, files a sender-only security warning
  ticket (no email body retained), and sends the offender a lockout
  auto-reply pointing them at the public support number so a false
  positive can call in and get unblocked.

**Contacts, incidents, and todos**
- Contacts identified by phone numbers (normalized to E.164-ish digits)
  and email addresses
- Consent tracked per contact per channel (phone / email) with source
  and timestamp. Returning callers skip the IVR consent gate; returning
  emailers skip the consent-request reply.
- **Trust levels** (`unverified` / `verified` / `suspect`) visible on
  the contact detail view. Injection detection flips to `suspect`
  automatically; the operator can toggle from the dashboard.
- Incidents carry subject, status, priority, labels, contact, and a
  structured timeline (calls, emails, notes, todos, status changes,
  consent events)
- Merge contacts and merge incidents for deduplication
- **Todos**: a first-class checklist per incident. Autonomous agent
  reviews extract concrete action items from call transcripts and
  email threads. The dashboard has an aggregate **Todos tab** that
  groups all open todos across every incident, sorted by priority.

**Autonomous agent reviews**
- After every bridged call ends, an autonomous Claude Code run fires
  to review the transcript, update the subject, add a summary note,
  and extract todos.
- After every voicemail is transcribed, the same review flow runs.
- After every inbound email that passes the preflight, an autonomous
  run triages it: for new threads, creates an incident and sends a
  consent-request; for existing threads, updates the incident and
  sends clarifying replies as needed.
- All autonomous runs use `autonomous=True` so they never clobber
  the operator's interactive prompt-bar conversation state.

**Agent tool API (the primary interface)**
- Bash scripts in `tools/` that wrap a single Python CLI
- The web dashboard's prompt bar sends prompts to Claude Code headless
  (`claude -p --add-dir .`) with a detailed system prompt listing
  every available tool
- Conversation continuity via `claude --continue` between prompts,
  with a "New chat" button to reset
- The prompt preamble includes a live snapshot of the focused
  incident (subject, status, contact, last 15 transcript lines, open
  todos) so the agent can answer simple questions without a tool call

**Agent behavior rules**
- Autonomy-first: the agent tries to solve the user's problem over
  email before escalating
- Phone escalation: when a thread is stuck or the user is frustrated,
  the agent offers the main support number (541-919-4096 in this
  deployment) with natural phrasing
- On-site awareness: on-site visits only offered within ~50 miles of
  Roseburg, Oregon
- Donation nudge on resolution: the final "issue resolved" email
  includes a short, pressure-free pointer to
  [freesoftware.support/support.html](https://freesoftware.support/support.html).
  Skipped on clarifying replies, frustrated users, and repeat donors.
- Auto-close on bridged calls: calls the operator answers live are
  closed automatically on hang-up so only voicemails and missed calls
  stay in the open queue
- Consent-first: no substantive support is given until the contact
  has explicitly consented. For phone that's the IVR press-1; for
  email that's a reply containing "I consent" / "yes".
- Transcripts are treated as noisy ASR (rules about name extraction,
  phone number fragments, trailing silence hallucinations)

**Web dashboard**
- Three-panel workspace: queue, incident detail, contact context
- Sticky LIVE strip at the top of the queue — active calls visible
  regardless of which tab you're on, with a pulsing red indicator
- Unified incident timeline with inline split-channel transcripts,
  emails, notes, status changes, todos, consent events
- Cold-call any contact by clicking a phone number → fires the
  technician-first bridging flow
- Create contacts from the UI
- Bottom prompt bar with Ctrl+K focus, streaming agent output in a
  floating drawer with live tool calls and results
- Autonomous agent runs auto-pop the drawer so the operator sees
  background activity in real time
- Dark theme, vanilla JS SPA (no framework)

## Architecture

```
callen/
├── sip/             pjsua2 wrappers — endpoint, account, call, media,
│                    bridge, DTMF, thread-safe SIPCommandQueue
├── ivr/             IVR script runner, injected API, outbound bridging
├── audio/           Resampler (8kHz -> 16kHz), chunked buffer,
│                    split-channel recorder
├── transcription/   Parakeet processor, per-channel stream with VAD +
│                    silence gate, voicemail post-processor
├── tts/             TTS engine abstraction (Kokoro default, espeak fallback)
├── agent/           Claude Code headless runner, system prompt
├── security/        Local-LLM preflight classifier (Ollama + Mistral)
├── notify/          IMAP poller + email processor + SMTP sender
├── web/             Quart server, REST API, WebSocket, static SPA
├── state/           Operator availability, call registry, event bus
├── storage/         SQLite schema + migrations + queries
├── cli.py           Python CLI backing the tools/ wrappers
└── app.py           Top-level orchestrator
docs/
└── freesoftware-support.md   Knowledge file the agent reads on project questions
tools/               Bash command wrappers (35+ commands)
IVR.py               Hot-reloaded IVR script (the file you edit)
```

**Threading**: pjsua2 runs with `threadCnt=0` on a single dedicated
SIP poll thread. IVR scripts run in per-call worker threads and submit
pjsua2 operations via the SIPCommandQueue. Each IVR thread registers
with `libRegisterThread` so pjsua2 object destruction is safe. The
Quart web server runs on its own asyncio loop; events from SIP/IVR
threads reach WebSocket clients via `asyncio.run_coroutine_threadsafe`.
The agent runner spawns `claude` as a subprocess and streams its
`stream-json` output back over WebSocket. The IMAP poller and
preflight classifier each run in their own daemon threads.

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

Pulls in NeMo + PyTorch + Kokoro TTS + Quart + webrtcvad (~3GB on
first install). A CUDA GPU is strongly recommended for live
transcription. Kokoro runs happily on CPU too.

### 4. Install Ollama + Mistral for the email preflight classifier

```sh
# Install Ollama — see https://ollama.com
curl -fsSL https://ollama.com/install.sh | sh

# Pull the preflight model
ollama pull mistral:7b
```

Ollama runs as a systemd service on `127.0.0.1:11434` by default.
Callen's preflight classifier assumes that URL — change
`[preflight] url` in `config.toml` if yours is different. You can
disable preflight entirely with `[preflight] enabled = false` but
then every inbound email hits Claude unfiltered.

### 5. Claude Code CLI

The agent runner shells out to `claude` in headless mode. Install the
Claude Code CLI and sign in once with your Anthropic account:

```sh
# See https://docs.claude.com/en/docs/claude-code for installation
claude --version
```

Callen calls `claude -p --add-dir .` for every agent prompt, so no
API key is needed — it uses your Claude account session.

### 6. Mail provider

You need an IMAP endpoint Callen can poll. Options:

- **Fastmail / Gmail with app passwords / self-hosted**: use the
  provider's real IMAP host (`imap.fastmail.com:993`,
  `imap.gmail.com:993`, etc.) with `imap_ssl = true` and pure SSL.
- **Proton Mail**: Proton has no public IMAP endpoint. Install
  **Proton Bridge** (`apt install protonmail-bridge`), run
  `protonmail-bridge --cli`, `login`, then `info` — it shows you
  the Bridge-specific username / password. Use `imap_host =
  "127.0.0.1"`, `imap_port = 1143`, `imap_ssl = false`,
  `imap_starttls = true`, and paste the Bridge-generated password
  as `imap_password` (NOT your real Proton password). The same
  Bridge instance also exposes SMTP at `127.0.0.1:1025` with the
  same credentials, so point the `smtp_*` fields there too.
  Callen auto-accepts Proton Bridge's self-signed cert on localhost.

### 7. Configure

```sh
cp config.toml.example config.toml
$EDITOR config.toml
```

**Critical VoIP.ms setup notes:**

- **`registrar` and `domain` must be the POP-specific server** for
  your DID (e.g. `seattle1.voip.ms`). Using the generic `sip.voip.ms`
  returns 403 Forbidden.
- **`username` is your VoIP.ms account number**, NOT the DID.
- **`password` is the SIP/IAX password** set in the portal.
- **`cell_phone` is bare E.164 digits**, no `+`.
- **`support_phone`** (under `[operator]`) is the public number the
  lockout auto-reply tells blocked senders to call for appeal —
  usually your VoIP.ms DID, not your personal cell.

`config.toml` is gitignored — credentials never get committed.

### 8. Run

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
INFO  IMAP poller started for 127.0.0.1 (every 30s)
INFO  Preflight email classifier: mistral:7b via http://localhost:11434
INFO  Callen IVR running — waiting for calls
INFO  Web dashboard: http://127.0.0.1:8080
INFO  SIP registered (expires: 295s)
```

Open the dashboard at <http://127.0.0.1:8080>, call your DID, and
email your monitored address.

## Email security architecture

The email pipeline has four sequential defensive layers before any
content reaches the Claude Code agent:

1. **IMAP poller** (`callen/notify/imap_poller.py`) — dedicated
   thread, fetches UNSEEN, dedupes by Message-ID, stores the raw
   email. Skips mailer-daemon, no-reply, and our own From addresses.

2. **Deterministic scanner** (`callen/notify/email_processor.py`) —
   regex patterns for injection phrases, bulk mail headers, HTML
   body stripping. Known-bad content is pre-tagged as `flagged` or
   `rejected` before any LLM sees it.

3. **Local-LLM preflight classifier**
   (`callen/security/preflight.py`) — Mistral 7B via Ollama running
   on localhost. Structured JSON output with `is_prompt_injection`,
   `is_automated`, `is_support_request`, plus a confidence level and
   reason. The verdict maps to `pass`, `reject`, `flag`, or `skip`.
   Flagged and rejected emails never reach the Claude agent.

4. **Claude Code agent** with a hardened system prompt
   (`callen/agent/system_prompt.md`) — explicit rules that email
   bodies are DATA, never INSTRUCTIONS; consent must be on file
   before substantive replies; no sensitive information (passwords,
   OTPs, credentials, internal notes) in outbound replies; OTP /
   verification-code emails are auto-rejected; the liability
   disclaimer is mandatory in every consent request.

The threat model assumes an attacker can send arbitrary email to the
monitored address. The goal is that no single layer being bypassed
results in catastrophic behavior. A novel prompt-injection phrasing
might slip past the regex scanner but get caught by Mistral; if the
local model misbehaves, the system prompt rules in Claude still
forbid sensitive disclosure; if Claude somehow starts to comply, the
pre-send nature of `send-email` means the operator can still see the
attempt in the timeline and course-correct.

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

Kokoro loads the ~82M-parameter neural model into memory at startup
and keeps it hot, so every `say()` is fast (~0.3s per sentence on
CPU, much faster on GPU). Output is automatically down-sampled to
8 kHz mono for pjsua2 compatibility.

Espeak is kept as a fallback — it needs no model, runs via
subprocess, and sounds robotic but works everywhere.

## Agent tool API (`tools/`)

Callen is designed to be driven by an AI agent. The dashboard prompt
bar spawns Claude Code headless with tool access; the agent can also
be invoked directly from the terminal for scripting. Every command
outputs JSON on stdout by default; pass `--pretty` for a
human-readable format where supported.

```sh
# Tickets / incidents
./tools/list-incidents [--status open] [--contact CON-0001]
./tools/get-incident INC-0042
./tools/update-incident INC-0042 --status resolved --priority high \
                                 --subject "..." --add-label billing
./tools/note-incident INC-0042 "Internal note"
./tools/create-incident --phone 15551234567 --subject "Callback request"
./tools/delete-incident INC-0042          # hard-delete; linked calls/emails detached, not wiped
./tools/merge-incidents INC-0043 INC-0042
./tools/reassign-incident INC-0042 CON-0008   # move ticket to a different contact

# Todos (extracted by agents from call/email content)
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
./tools/remove-phone CON-0007 15555550123            # detach a phone
./tools/remove-email CON-0007 alt@example.com
./tools/rename-phone CON-0007 anonymous 15551234567  # rewrite a placeholder
./tools/rename-email CON-0007 old@x.com new@x.com
./tools/contact-consent CON-0007 --phone 15551234567 --source manual
./tools/merge-contacts CON-0008 CON-0007
./tools/delete-contact CON-0007 [--cascade]          # --cascade also deletes every incident on the contact

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

**Typical autonomous heartbeat flow** (agent driving itself after a
bridged call ends, a voicemail is transcribed, or an email arrives):

```sh
./tools/get-incident INC-0042                     # full context
./tools/get-transcript --incident INC-0042 --text # latest transcript
./tools/update-incident INC-0042 --subject "..."
./tools/add-todo INC-0042 "Drive to address and install GPU"
./tools/note-incident INC-0042 "Customer agreed to $X by Y date"
# ...or for email:
./tools/send-email INC-0042 --body "Could you share the printer model?"
./tools/contact-consent CON-0042 --email dave@example.com --source email
```

Every write command logs a timeline entry so the agent can re-run
without duplicating state.

## Web dashboard API

For custom integrations or your own frontends:

```
GET    /api/incidents                 List incidents
GET    /api/incidents/<id>            Full detail with timeline, todos, emails
PATCH  /api/incidents/<id>            status / priority / subject / labels
POST   /api/incidents/<id>/notes      Add a note
GET    /api/incidents/<id>/todos      List todos on one incident
POST   /api/incidents/<id>/todos      Add a todo
GET    /api/todos?status=open         Aggregate todos across all incidents
PATCH  /api/todos/<id>                Toggle done / update text
DELETE /api/todos/<id>

GET    /api/contacts                  List
GET    /api/contacts/<id>             Detail with phones/emails/incidents
POST   /api/contacts                  Create

GET    /api/calls                     Active calls
GET    /api/history                   Call history
GET    /api/recordings/<id>/<channel> Download a call recording

GET    /api/emails?status=pending     Triage queue (pending|flagged|rejected|attached)
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
`medConfig.noVad = True` is already set in `callen/sip/endpoint.py`.
If you still see drops, increase the jitter buffer (`jbInit`,
`jbMax`) in the same file.

**"Okay / yeah / um" hallucinations from silent audio**  
The RMS/peak gate in `callen/transcription/stream.py` should catch
them. Thresholds are `rms < 0.005` and `peak < 0.03`; tighten if your
microphone is noisier than average.

**Cell carrier voicemail picks up before Callen's**  
Lower `RING_TIMEOUT` in `callen/ivr/api.py` (default 18s). US
carriers typically roll to voicemail at 20-25s.

**Kokoro fails to load**  
The TTS factory automatically falls back to espeak-ng. Check
`pip show kokoro torch` and ensure the cache at
`~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/` is
accessible. Set `engine = "espeak"` in `config.toml` to skip Kokoro
entirely.

**Ollama preflight disabled / unavailable**  
If Ollama isn't running, Callen logs it and the preflight classifier
returns `skip` for every email. The deterministic scanner and
Claude's hardened system prompt are still in effect, but you lose
the intent-aware injection shield. Start Ollama (`systemctl start
ollama`) and pull the model (`ollama pull mistral:7b`) to restore
full protection.

**Proton Mail IMAP fails with "Name or service not known"**  
Proton has no public IMAP endpoint. Install `protonmail-bridge`,
run `protonmail-bridge --cli`, `login`, then `info` to get the
Bridge-specific credentials. Callen's poller already handles the
self-signed certificate on `127.0.0.1`. See step 6 in Quick Start.

**Agent runs return immediately without doing anything**  
Check that `claude` is on PATH and authenticated:
`which claude && claude --version`. The runner pre-approves
`Bash(./tools/*)`, `Read`, `Glob`, and `Grep` via `--allowedTools`,
so the agent doesn't need interactive permission prompts.

**Doubled agent output in the drawer**  
Fixed in `0b4db4f` — the dashboard now only renders text from the
final `result` event, not streaming `assistant` events. Hard-refresh
your browser to bust the cached JS.

**"database is locked"**  
Should not happen — SQLite is configured with WAL mode and a 10s
busy timeout. Write methods explicitly rollback on error. If you see
it, your filesystem may not support proper locking.

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
