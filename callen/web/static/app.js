// Callen — IVR Dashboard Frontend

const state = {
    activeCalls: [],
    selectedCallId: null,
    operatorStatus: 'available',
    transcriptWs: null,
};

// --- API helpers ---

async function api(path, opts = {}) {
    const res = await fetch('/api' + path, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
    });
    return res.json();
}

// --- Operator status ---

async function loadOperatorStatus() {
    const data = await api('/operator/status');
    state.operatorStatus = data.status;
    updateStatusButtons();
}

function updateStatusButtons() {
    document.querySelectorAll('.status-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.status === state.operatorStatus);
        btn.classList.toggle('busy', btn.dataset.status === 'busy' && state.operatorStatus === 'busy');
        btn.classList.toggle('dnd', btn.dataset.status === 'dnd' && state.operatorStatus === 'dnd');
    });
}

document.querySelectorAll('.status-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
        await api('/operator/status', {
            method: 'PUT',
            body: JSON.stringify({ status: btn.dataset.status }),
        });
        state.operatorStatus = btn.dataset.status;
        updateStatusButtons();
    });
});

// --- Active calls ---

async function loadActiveCalls() {
    state.activeCalls = await api('/calls');
    renderCallList();
}

function renderCallList() {
    const el = document.getElementById('call-list');
    if (state.activeCalls.length === 0) {
        el.innerHTML = '<div class="empty-state">No active calls</div>';
        return;
    }
    el.innerHTML = state.activeCalls.map(c => `
        <div class="call-card ${c.id === state.selectedCallId ? 'selected' : ''}"
             onclick="selectCall('${c.id}')">
            <div class="caller">${c.caller_id || 'Unknown'}</div>
            <div class="meta">
                <span class="state ${c.state}">${c.state}</span>
                &middot; ${formatDuration(c.duration)}
            </div>
        </div>
    `).join('');
}

function selectCall(callId) {
    state.selectedCallId = callId;
    renderCallList();
    connectTranscriptWs(callId);
    loadNotes(callId);

    const call = state.activeCalls.find(c => c.id === callId);
    const header = document.getElementById('transcript-header');
    header.textContent = call
        ? `Live transcript: ${call.caller_id} (${call.state})`
        : 'Transcript';
}

// --- Live transcript WebSocket ---

function connectTranscriptWs(callId) {
    if (state.transcriptWs) {
        state.transcriptWs.close();
    }

    document.getElementById('transcript-container').innerHTML = '';

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/transcript/${callId}`);
    state.transcriptWs = ws;

    ws.onmessage = (evt) => {
        const data = JSON.parse(evt.data);
        appendTranscriptSegment(data);
    };

    ws.onclose = () => {
        if (state.transcriptWs === ws) state.transcriptWs = null;
    };
}

function appendTranscriptSegment(seg) {
    const container = document.getElementById('transcript-container');
    // Remove empty state
    const empty = container.querySelector('.empty-state');
    if (empty) empty.remove();

    const div = document.createElement('div');
    div.className = 'transcript-segment';
    div.innerHTML = `
        <span class="speaker ${seg.speaker}">${seg.speaker}</span>
        <span class="text">${escapeHtml(seg.text)}</span>
        <span class="timestamp">${formatTimestamp(seg.timestamp_offset)}</span>
    `;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

// --- Call events WebSocket ---

function connectCallsWs() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/calls`);

    ws.onmessage = (evt) => {
        const data = JSON.parse(evt.data);
        handleCallEvent(data);
    };

    ws.onclose = () => {
        // Reconnect after delay
        setTimeout(connectCallsWs, 3000);
    };
}

function handleCallEvent(data) {
    if (data.type === 'operator_status') {
        state.operatorStatus = data.new;
        updateStatusButtons();
    } else {
        // Refresh active calls on any call event
        loadActiveCalls();
    }
}

// --- Notes ---

async function loadNotes(callId) {
    // Try active call first, fall back to history
    try {
        const notes = await api(`/history/${callId}`);
        renderNotes(notes.notes || []);
    } catch {
        renderNotes([]);
    }
}

function renderNotes(notes) {
    const el = document.getElementById('notes-list');
    if (notes.length === 0) {
        el.innerHTML = '<div class="empty-state" style="padding: 16px;">No notes yet</div>';
        return;
    }
    el.innerHTML = notes.map(n => `
        <div class="note">
            ${escapeHtml(n.text)}
            <div class="note-meta">${n.author} &middot; ${new Date(n.created_at * 1000).toLocaleString()}</div>
        </div>
    `).join('');
}

document.getElementById('add-note-btn').addEventListener('click', async () => {
    if (!state.selectedCallId) return;
    const textarea = document.getElementById('note-text');
    const text = textarea.value.trim();
    if (!text) return;

    await api(`/history/${state.selectedCallId}/notes`, {
        method: 'POST',
        body: JSON.stringify({ text }),
    });
    textarea.value = '';
    loadNotes(state.selectedCallId);
});

// --- Call history ---

async function loadHistory() {
    const records = await api('/history?limit=50');
    const body = document.getElementById('history-body');
    if (records.length === 0) {
        body.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-dim);">No call history</td></tr>';
        return;
    }
    body.innerHTML = records.map(r => `
        <tr onclick="viewHistoricalCall('${r.id}')">
            <td>${r.caller_id}</td>
            <td>${r.state}</td>
            <td>${formatDuration(r.duration_seconds)}</td>
            <td>${new Date(r.started_at * 1000).toLocaleString()}</td>
            <td>${r.was_bridged ? 'Yes' : 'No'}</td>
        </tr>
    `).join('');
}

async function viewHistoricalCall(callId) {
    state.selectedCallId = callId;
    const data = await api(`/history/${callId}`);

    // Show transcript
    const container = document.getElementById('transcript-container');
    container.innerHTML = '';
    const header = document.getElementById('transcript-header');
    header.textContent = `Transcript: ${data.caller_id} (${data.state})`;

    if (data.transcript && data.transcript.length > 0) {
        data.transcript.forEach(seg => appendTranscriptSegment(seg));
    } else {
        container.innerHTML = '<div class="empty-state">No transcript available</div>';
    }

    renderNotes(data.notes || []);
}

// --- Utilities ---

function formatDuration(seconds) {
    if (!seconds) return '0:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatTimestamp(offset) {
    const m = Math.floor(offset / 60);
    const s = Math.floor(offset % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// --- Init ---

loadOperatorStatus();
loadActiveCalls();
loadHistory();
connectCallsWs();

// Refresh active calls every 5 seconds (duration updates)
setInterval(loadActiveCalls, 5000);
// Refresh history every 30 seconds
setInterval(loadHistory, 30000);
