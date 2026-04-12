# Callen

GPL3 IVR (Interactive Voice Response) system for desktop Linux with a SIP trunk
backend. Originally targeted at the Librem 5; now modernized for VoIP.ms and
freesoftware.support.

## Features

- Inbound call answering via VoIP.ms SIP trunk
- IVR scripting in plain Python (`IVR.py`) — clean `say()`, `dtmf()`,
  `bridge_to_operator()`, `record_voicemail()` API
- TTS prompts via espeak-ng played into the call
- DTMF detection with instant playback interrupt
- Explicit recording-consent flow (press 1 to consent before proceeding)
- Call bridging — forward to operator's cell phone via the SIP trunk
- Voicemail recording with timestamped WAV files
- SQLite call history with consent tracking
- Designed for live transcription via NVIDIA Parakeet (Phase 5, in progress)
- Designed for a Quart web dashboard with WebSocket live updates (Phase 6)

## Setup

### 1. System packages

```
sudo apt install espeak-ng sox build-essential python3-dev swig \
                 libasound2-dev uuid-dev libssl-dev
```

### 2. Build pjproject from source (required)

The pip `pjsua2-pybind11` package is missing `AudioMediaPlayer`,
`AudioMediaRecorder`, and `AudioMediaPort` classes — Callen needs the SWIG
bindings built from pjproject source.

```
git clone --branch 2.14.1 https://github.com/pjsip/pjproject.git
cd pjproject
./configure --enable-shared
make dep && make
sudo make install
sudo ldconfig

cd pjsip-apps/src/swig/python
make
python3 setup.py install --user
```

### 3. Python dependencies

```
pip install -r requirements.txt
```

### 4. Configuration

```
cp config.toml.example config.toml
# Edit config.toml with your VoIP.ms credentials
```

**Important VoIP.ms setup notes:**

- The `registrar` and `domain` must be the **POP-specific server** for your
  DID (e.g. `seattle1.voip.ms`), NOT `sip.voip.ms`. Using the generic server
  causes 403 Forbidden after digest auth.
- The `username` is your VoIP.ms account number (from the portal under
  Main Account > Account Settings), NOT the DID number.
- The `password` is the SIP/IAX password set in the portal Account Settings.
- The `cell_phone` should be in bare E.164 format (`15417999823`), no `+`.

`config.toml` is gitignored — your credentials never leave your machine.

### 5. Run

```
python3 -m callen
```

You should see:

```
INFO  Starting Callen IVR system
INFO  pjsua2 endpoint started on port 5060
INFO  SIP account created: sip:NNNNNN@seattleN.voip.ms
INFO  Callen IVR running — waiting for calls
INFO  SIP registered (expires: 295s)
```

## IVR Script

Edit `IVR.py` to customize the call flow. The injected API:

- `say(call, text, repeat=True)` — TTS playback (interrupted by DTMF)
- `play(call, wav_path)` — pre-recorded WAV
- `dtmf(call, count=1, timeout=10)` — wait for digits, returns `str` or `None`
- `bridge_to_operator(call)` — forward to operator's cell
- `record_voicemail(call)` — record with `#` to end
- `hangup(call)` — end the call
- `caller_id(call)` — caller's number
- `operator_available()` — check operator state

## Status

Working end-to-end:
- SIP registration with VoIP.ms
- Inbound calls answered
- TTS prompts playing in-call with instant DTMF interrupt
- Multi-step IVR flow with consent gate
- Call bridging to operator's cell with bidirectional audio
- Hangup propagation between bridged legs
- Voicemail recording (16kHz mono PCM WAV)
- Call history persistence

In progress:
- Live transcription via NVIDIA Parakeet (split-channel caller/technician)
- Quart web dashboard with WebSocket live updates
- Voicemail email notification
- Operator availability state in web UI

---

Copyright (C) 2020 David Hamner
Licensed under GNU General Public License v3
