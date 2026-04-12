# Callen

GPL3 IVR (Interactive Voice Response) system for desktop Linux with a SIP
trunk backend. Originally a Librem 5 phone IVR; modernized for VoIP.ms and
the [freesoftware.support](https://freesoftware.support) free community tech
support service.

## What it does

You point a SIP trunk at it. When someone calls in, Callen answers, plays a
recording-consent greeting, lets the caller pick from a DTMF menu, and either
forwards them to your cell phone (with bidirectional audio bridging and live
transcription) or takes a voicemail. A web dashboard shows live transcripts,
call history, operator status, and notes — all in real time.

## Features

**Working**

- SIP registration with VoIP.ms (or any standard SIP trunk)
- Inbound call answering with TTS prompts (espeak-ng) played into the call
- DTMF detection with instant playback interrupt — press a key, the prompt
  cuts off mid-word and you move on
- Explicit recording-consent flow (press 1 to consent before proceeding)
- Multi-step IVR scripted in plain Python (`IVR.py`) — clean injected API
- Call bridging to operator's cell phone via the SIP trunk
- Auto operator availability tracking — second caller while you're on a call
  hits the busy voicemail prompt instead of ringing your cell
- Ring timeout — if you don't pick up your cell within 18 seconds, the caller
  rolls into Callen's voicemail (not your cell carrier's)
- Hangup propagation in both directions (caller hangs up → operator drops
  immediately, and vice versa)
- Voicemail recording with timestamped WAV files
- Live split-channel transcription via NVIDIA Parakeet-TDT — caller and
  technician transcribed independently with VAD-based segmentation (cut on
  natural pauses, hard 15s cap)
- Quart web dashboard with live WebSocket updates — auto-selects active
  calls, displays live transcript stream, call history with full transcript
  playback, notes, operator status toggle
- SQLite call history with consent timestamps and per-call transcript storage

**Not yet wired**

- Voicemail email notification (the SMTP module exists but isn't hooked up)
- Per-call recording playback in the web UI
- Custom ticketing integration (deliberately out of scope for now)

## Quick start

### 1. System packages

```sh
sudo apt install espeak-ng sox build-essential python3-dev swig \
                 libasound2-dev uuid-dev libssl-dev
```

### 2. Build pjproject from source (one-time, ~5 minutes)

The pip `pjsua2-pybind11` package is missing `AudioMediaPlayer`,
`AudioMediaRecorder`, and `AudioMediaPort` — Callen needs the SWIG bindings
built from pjproject source.

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
# Should print: <class 'pjsua2.AudioMediaPlayer'>
```

### 3. Python dependencies

```sh
pip install -r requirements.txt
```

This pulls in NeMo + PyTorch (~3GB on first install). A CUDA GPU is strongly
recommended for live transcription — Parakeet runs on CPU but is much slower.

### 4. Configure VoIP.ms credentials

```sh
cp config.toml.example config.toml
$EDITOR config.toml
```

**Critical VoIP.ms setup notes** — these caused real debugging pain, so read
them carefully:

- **`registrar` and `domain` must be the POP-specific server** for your DID
  (e.g. `seattle1.voip.ms`, `dallas.voip.ms`). Do NOT use `sip.voip.ms` —
  the generic server returns 403 Forbidden after digest auth.
- **`username` is your VoIP.ms account number** (6 digits, from portal
  Main Account > Account Settings). It is NOT the DID number.
- **`password` is the SIP/IAX password** set in the VoIP.ms portal Account
  Settings — separate from your portal login password.
- **`cell_phone` is bare E.164 digits**, no `+` (e.g. `15417999823`).
- The DID's "Routing" in the VoIP.ms portal should point to "[SIP] Main
  Account" (or whichever sub-account matches your `username`).

`config.toml` is gitignored — your credentials never get committed.

### 5. Run

```sh
python3 -m callen
```

You should see:

```
INFO  Starting Callen IVR system
INFO  Loading Parakeet model (this takes a moment on first run)...
INFO  Parakeet model loaded successfully
INFO  Transcription enabled
INFO  pjsua2 endpoint started on port 5060
INFO  SIP account created: sip:NNNNNN@<pop>.voip.ms
INFO  Callen IVR running — waiting for calls
INFO  Web dashboard: http://127.0.0.1:8080
INFO  SIP registered (expires: 295s)
```

Open the dashboard at <http://127.0.0.1:8080> and call your DID number.

## Customizing the IVR

`IVR.py` defines the call flow as a plain Python function. Edit it freely.
The injected API (no imports needed — these are placed in the script's
namespace by the engine):

| Function                                              | Description |
|---|---|
| `say(call, text, repeat=True)`                        | TTS playback. If `repeat=True`, loops until interrupted by DTMF or hangup. |
| `play(call, wav_path)`                                | Play a pre-recorded WAV file. |
| `dtmf(call, count=1, timeout=10)`                     | Wait for `count` DTMF digits. Returns the digit string or `None` on timeout/hangup. |
| `bridge_to_operator(call)`                            | Forward to the operator's cell. Handles availability and ring timeout. |
| `record_voicemail(call, prompt=None)`                 | Record voicemail with `#` to end. Custom prompt optional. |
| `hangup(call)`                                        | End the call. |
| `caller_id(call)`                                     | Caller's phone number (best-effort from SIP From header). |
| `operator_available()`                                | True if operator is AVAILABLE (not on a call, not in DND). |

The default `IVR.py` is the freesoftware.support flow: consent gate → menu →
bridge or voicemail.

## Agent tool API (`tools/`)

Callen is designed to be driven by an AI agent running in the project folder
via Claude Code headless mode. The `tools/` directory holds short bash
commands, each a thin wrapper around `python3 -m callen.cli`. Every command
outputs JSON on stdout by default; pass `--pretty` for a human-readable
format where supported.

```sh
# Tickets / incidents
./tools/list-incidents [--status open] [--contact CON-0001]
./tools/get-incident INC-0042
./tools/update-incident INC-0042 --status resolved --priority high \
                                 --subject "Router config" \
                                 --add-label billing,networking
./tools/note-incident INC-0042 "Caller wants a callback after 2pm"
echo "longer note" | ./tools/note-incident INC-0042 -
./tools/create-incident --phone 15551234567 --subject "Callback request"

# Contacts (the persistent identity of a caller/emailer)
./tools/list-contacts
./tools/get-contact CON-0007
./tools/create-contact --name "Jane Doe" --phone 15551234567 \
                       --email jane@example.com
./tools/update-contact CON-0007 --name "Jane Doe" --notes "Prefers email"
./tools/contact-consent CON-0007 --phone 15551234567 --source manual
./tools/contact-consent CON-0007 --email jane@example.com --source email

# Transcripts and audio
./tools/get-transcript --incident INC-0042 --text
./tools/get-transcript --call <uuid>
./tools/get-audio --incident INC-0042 --channel caller --out /tmp/call.wav
./tools/get-audio --incident INC-0042 --channel tech
./tools/get-audio --incident INC-0042 --channel voicemail

# Raw calls
./tools/list-calls

# Operator status (shared with the web dashboard)
./tools/get-operator-status
./tools/set-operator-status busy        # available / busy / dnd

# Merging (for deduplication)
./tools/merge-contacts CON-0006 CON-0005   # fold 0006 into 0005
./tools/merge-incidents INC-0022 INC-0021  # fold 0022 into 0021

# Attach data to existing contacts
./tools/add-phone CON-0004 15555550123
./tools/add-email CON-0004 alt@example.com

# Fuzzy search across contacts and incidents
./tools/search "jane"
./tools/search 5559990000
./tools/search "router"
```

**Typical agent flow** (heartbeat during an active call):

```sh
./tools/list-incidents --status open --limit 1    # find the active ticket
./tools/get-incident INC-0042                     # full context
./tools/get-transcript --incident INC-0042 --text # latest transcript
# reason about the transcript...
./tools/update-incident INC-0042 \
    --subject "Router port-forwarding issue" \
    --add-label networking
./tools/note-incident INC-0042 "Caller is on OpenWRT, confirmed MAC OK"
```

Every write command is idempotent and logs a timeline entry on the incident,
so the agent can re-run without duplicating state.

## Architecture

```
callen/
├── sip/             pjsua2 wrappers — endpoint, account, call, media,
│                    bridge, DTMF, thread-safe SIPCommandQueue
├── ivr/             Script loader and the injected API (the file you edit)
├── audio/           Resampler (8kHz → 16kHz for STT), chunk buffer
├── transcription/   Parakeet processor + per-channel streams with VAD
│                    segmentation
├── state/           Operator state, active call registry, event bus
├── storage/         SQLite — calls, transcript_segments, notes
├── web/             Quart server, REST API, WebSocket, static SPA
├── notify/          Voicemail email (not yet wired)
└── app.py           Top-level orchestrator
```

**Threading model**: pjsua2 runs with `threadCnt=0` on a single dedicated
SIP poll thread. IVR scripts run in per-call worker threads and submit
pjsua2 operations via the `SIPCommandQueue`. Each IVR thread registers with
`libRegisterThread` so pjsua2 object destruction at thread exit is safe.
The Quart web server runs in its own thread with its own asyncio loop;
events from the SIP/IVR side reach WebSocket clients via
`asyncio.run_coroutine_threadsafe`.

## Web dashboard

- **Active calls** (left panel) — auto-selects when a call comes in
- **Live transcript** (center) — caller + technician streams via WebSocket
- **Notes** (right) — add notes during or after a call
- **Operator status** (top right) — Available / Busy / DND toggle
- **Call history** (bottom) — click any row to view its full transcript

REST API (also useful for scripting):

```
GET  /api/calls                 Active calls
GET  /api/history               Call history (paginated)
GET  /api/history/<id>          Detail with transcript and notes
POST /api/history/<id>/notes    Add a note
GET  /api/transcripts/<id>      Just the transcript segments
GET  /api/operator/status       Current status
PUT  /api/operator/status       Set status {"status": "available"|"busy"|"dnd"}
```

WebSockets:

```
WS /ws/calls                 Live call state events
WS /ws/transcript/<call_id>  Live transcript segments for one call
```

## Troubleshooting

**SIP registration returns 403 Forbidden after the auth challenge**

You're almost certainly using `sip.voip.ms` instead of your DID's POP server.
Set `registrar = "sip:seattle1.voip.ms"` (or your POP) in `config.toml`.

**Audio cuts out during natural conversational pauses**

VAD/silence-suppression is disabled by default in `callen/sip/endpoint.py`
via `medConfig.noVad = True`. If you still get drops, try increasing the
jitter buffer (`jbInit`, `jbMax` in the same file).

**Cell carrier voicemail picks up before Callen's voicemail**

Lower `RING_TIMEOUT` in `callen/ivr/api.py` (default 18s). Most US carriers
roll to voicemail at 20-25s.

**Transcription not appearing in dashboard**

Verify the model loaded at startup ("Parakeet model loaded successfully").
On first run it downloads ~2GB from Hugging Face. The dashboard auto-selects
the first active call, so live transcripts should appear without clicking.

**`sqlite3.OperationalError: database is locked`**

Should not happen — the DB is configured with WAL mode and a 10s busy
timeout. If you see it, your filesystem may not support proper locking
(e.g. some network mounts).

## License

```
Copyright (C) 2020 David Hamner

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
```
