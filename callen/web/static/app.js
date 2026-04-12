// Callen — Support Console frontend
// Vanilla JS SPA; no framework. Talks to the Quart backend.

// ============ State ============
const state = {
    currentTab: 'incidents',
    queueItems: [],
    selectedKind: null,      // 'incident' | 'email' | 'call' | 'contact'
    selectedId: null,
    operatorStatus: 'available',
    counts: { open: 0, pending: 0, flagged: 0, active: 0 },
    callsWs: null,
    agentWs: null,
    currentAgentRunId: null,
};

// ============ DOM helpers ============
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, children = []) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === 'class') e.className = v;
        else if (k === 'html') e.innerHTML = v;
        else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
        else if (v != null) e.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
        if (c == null) continue;
        e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    }
    return e;
};
const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };
const escapeHtml = (s) => {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
};

// ============ API ============
async function api(path, opts = {}) {
    const res = await fetch('/api' + path, {
        headers: { 'Content-Type': 'application/json' },
        ...opts,
    });
    if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`${res.status}: ${text || res.statusText}`);
    }
    return res.json();
}

// ============ Formatting ============
function fmtTime(epoch) {
    if (!epoch) return '—';
    const d = new Date(epoch * 1000);
    return d.toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
}

function fmtDuration(seconds) {
    if (seconds == null) return '—';
    const s = Math.max(0, Math.floor(seconds));
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return `${m}:${rest.toString().padStart(2, '0')}`;
}

function fmtTranscriptTime(offset) {
    const s = Math.max(0, Math.floor(offset || 0));
    return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, '0')}`;
}

// ============ Operator status ============
async function loadOperatorStatus() {
    try {
        const d = await api('/operator/status');
        state.operatorStatus = d.status;
        updateStatusButtons();
    } catch (e) { console.error(e); }
}

function updateStatusButtons() {
    document.querySelectorAll('.status-btn').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.status === state.operatorStatus);
    });
}

document.querySelectorAll('.status-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
        try {
            await api('/operator/status', {
                method: 'PUT',
                body: JSON.stringify({ status: btn.dataset.status }),
            });
            state.operatorStatus = btn.dataset.status;
            updateStatusButtons();
        } catch (e) { console.error(e); }
    });
});

// ============ Count refresh ============
async function refreshCounts() {
    try {
        const [open, pending, flagged, active] = await Promise.all([
            api('/incidents?status=open&limit=500'),
            api('/emails?status=pending&limit=500'),
            api('/emails?status=flagged&limit=500'),
            api('/calls'),
        ]);
        state.counts = {
            open: open.length,
            pending: pending.length,
            flagged: flagged.length,
            active: active.length,
        };
        $('count-open').textContent = `${state.counts.open} open`;
        $('count-pending').textContent = `${state.counts.pending} pending`;
        $('count-flagged').textContent = `${state.counts.flagged} flagged`;
        $('count-active').textContent = `${state.counts.active} active`;
    } catch (e) { console.error(e); }
}

// ============ Queue tabs ============
document.querySelectorAll('.queue-tabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

function switchTab(name) {
    state.currentTab = name;
    document.querySelectorAll('.queue-tabs .tab').forEach((t) => {
        t.classList.toggle('active', t.dataset.tab === name);
    });
    loadQueue();
}

async function loadQueue() {
    const list = $('queue-list');
    list.innerHTML = '<div class="empty-state">Loading…</div>';

    try {
        let items = [];
        switch (state.currentTab) {
            case 'incidents':
                items = await api('/incidents?limit=200');
                renderIncidentsQueue(items);
                break;
            case 'pending-emails':
                items = await api('/emails?status=pending&limit=100');
                renderEmailsQueue(items, 'pending');
                break;
            case 'flagged-emails':
                items = await api('/emails?status=flagged&limit=100');
                renderEmailsQueue(items, 'flagged');
                break;
            case 'active-calls':
                items = await api('/calls');
                renderCallsQueue(items);
                break;
            case 'contacts':
                items = await api('/contacts?limit=200');
                renderContactsQueue(items);
                break;
        }
    } catch (e) {
        list.innerHTML = `<div class="empty-state">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function renderQueueItems(list, items, builder, emptyMsg) {
    clear(list);
    if (!items.length) {
        list.appendChild(el('div', { class: 'empty-state' }, emptyMsg));
        return;
    }
    for (const item of items) list.appendChild(builder(item));
}

function renderIncidentsQueue(items) {
    const list = $('queue-list');
    renderQueueItems(list, items, (inc) => {
        const selected = state.selectedKind === 'incident' && state.selectedId === inc.id;
        const node = el('div', {
            class: 'queue-item' + (selected ? ' selected' : ''),
            onclick: () => selectIncident(inc.id),
        }, [
            el('div', { class: 'q-title' }, inc.subject || '(no subject)'),
            el('div', { class: 'q-meta' }, [
                el('span', { class: 'q-id' }, inc.id),
                el('span', { class: `status-pill ${inc.status}` }, inc.status),
                el('span', {}, fmtTime(inc.updated_at)),
            ]),
        ]);
        return node;
    }, 'No incidents');
}

function renderEmailsQueue(items, kind) {
    const list = $('queue-list');
    renderQueueItems(list, items, (em) => {
        const selected = state.selectedKind === 'email' && state.selectedId === em.id;
        return el('div', {
            class: 'queue-item' + (selected ? ' selected' : ''),
            onclick: () => selectEmail(em.id),
        }, [
            el('div', { class: 'q-title' }, em.subject || '(no subject)'),
            el('div', { class: 'q-meta' }, [
                el('span', { class: 'q-id' }, `#${em.id}`),
                el('span', { class: `status-pill ${em.status}` }, em.status),
                el('span', {}, em.from_addr),
            ]),
        ]);
    }, kind === 'pending' ? 'No pending emails' : 'No flagged emails');
}

function renderCallsQueue(items) {
    const list = $('queue-list');
    renderQueueItems(list, items, (c) => {
        const selected = state.selectedKind === 'call' && state.selectedId === c.id;
        return el('div', {
            class: 'queue-item' + (selected ? ' selected' : ''),
            onclick: () => selectActiveCall(c.id),
        }, [
            el('div', { class: 'q-title' }, c.caller_id || 'unknown'),
            el('div', { class: 'q-meta' }, [
                el('span', { class: 'q-id' }, c.id.slice(0, 8)),
                el('span', { class: `status-pill ${c.state}` }, c.state),
                el('span', {}, fmtDuration(c.duration)),
            ]),
        ]);
    }, 'No active calls');
}

function renderContactsQueue(items) {
    const list = $('queue-list');
    renderQueueItems(list, items, (c) => {
        const selected = state.selectedKind === 'contact' && state.selectedId === c.id;
        return el('div', {
            class: 'queue-item' + (selected ? ' selected' : ''),
            onclick: () => selectContact(c.id),
        }, [
            el('div', { class: 'q-title' }, c.display_name || '(unnamed)'),
            el('div', { class: 'q-meta' }, [
                el('span', { class: 'q-id' }, c.id),
                el('span', {}, c.phones || c.emails || '—'),
            ]),
        ]);
    }, 'No contacts');
}

// ============ Detail: incident ============
async function selectIncident(incidentId) {
    state.selectedKind = 'incident';
    state.selectedId = incidentId;
    loadQueue();  // re-render to show selection
    try {
        const inc = await api(`/incidents/${incidentId}`);
        renderIncidentDetail(inc);
        renderContactContext(inc.contact || null, inc);
    } catch (e) {
        $('detail-body').innerHTML = `<div class="empty-state">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function renderIncidentDetail(inc) {
    // Header
    const labels = (inc.labels || []).map((l) =>
        el('span', { class: 'status-pill' }, l)
    );
    const header = $('detail-header');
    clear(header);
    header.appendChild(el('div', { class: 'detail-title' }, [
        el('span', {}, inc.subject || '(no subject)'),
        el('span', { class: 'detail-id' }, inc.id),
    ]));
    const meta = el('div', { class: 'detail-meta' }, [
        el('span', { class: `status-pill ${inc.status}` }, inc.status),
        el('span', { class: `status-pill priority-pill ${inc.priority}` }, inc.priority),
        ...labels,
    ]);
    header.appendChild(meta);

    // Action buttons
    const actions = $('detail-actions');
    clear(actions);
    actions.appendChild(el('button', {
        class: 'primary',
        onclick: () => callbackContact(inc),
    }, '📞 Call back'));
    actions.appendChild(el('button', {
        onclick: () => addNotePrompt(inc.id),
    }, '📝 Add note'));
    actions.appendChild(el('button', {
        onclick: () => updateStatusPrompt(inc.id, inc.status),
    }, '🔄 Status'));

    // Timeline body
    const body = $('detail-body');
    clear(body);
    body.appendChild(buildTimeline(inc));
}

function buildTimeline(inc) {
    const container = el('div', { class: 'timeline' });

    // Collect entries with call/email detail lookups
    const entries = [...(inc.entries || [])];
    const callMap = new Map((inc.calls || []).map((c) => [c.id, c]));
    const emailMap = new Map((inc.emails || []).map((e) => [e.id, e]));
    const transcriptByCall = new Map();
    for (const seg of inc.transcript || []) {
        if (!transcriptByCall.has(seg.call_id)) transcriptByCall.set(seg.call_id, []);
        transcriptByCall.get(seg.call_id).push(seg);
    }

    // Sort timeline chronologically
    entries.sort((a, b) => (a.occurred_at || 0) - (b.occurred_at || 0));

    if (!entries.length) {
        container.appendChild(el('div', { class: 'empty-state' },
            'No timeline entries yet.'));
        return container;
    }

    for (const e of entries) container.appendChild(renderTimelineEntry(e, callMap, emailMap, transcriptByCall));
    return container;
}

function renderTimelineEntry(entry, callMap, emailMap, transcriptByCall) {
    const type = entry.type || 'note';
    const markerIcon = {
        call: '📞', email: '✉', note: '📝',
        status_change: '⟲', label_change: '🏷', consent: '✓',
        consent_request: '?',
    }[type] || '•';

    const markerClass = {
        call: 'call', email: 'email', note: 'note',
        status_change: 'status', label_change: 'label', consent: 'consent',
    }[type] || '';

    const marker = el('div', { class: `tl-marker ${markerClass}` }, markerIcon);
    const body = el('div', { class: 'tl-body' });

    const metaLine = [
        fmtTime(entry.occurred_at),
        entry.author ? `· ${entry.author}` : null,
    ].filter(Boolean).join(' ');

    body.appendChild(el('div', { class: 'tl-meta' }, [
        el('span', {}, type),
        el('span', {}, metaLine),
    ]));

    const content = el('div', { class: 'tl-content' });

    switch (type) {
        case 'call': {
            const call = entry.linked_call_id ? callMap.get(entry.linked_call_id) : null;
            if (call) {
                content.appendChild(renderCallBlock(call, transcriptByCall.get(call.id) || []));
            } else {
                content.textContent = JSON.stringify(entry.payload || {});
            }
            break;
        }
        case 'email': {
            const email = entry.linked_email_id ? emailMap.get(entry.linked_email_id) : null;
            if (email) {
                content.appendChild(renderEmailBlock(email));
            } else {
                const p = entry.payload || {};
                content.innerHTML = `<strong>${escapeHtml(p.subject || '')}</strong><br>${escapeHtml((p.preview || '').slice(0, 300))}`;
            }
            break;
        }
        case 'note':
            content.textContent = entry.payload?.text || '';
            break;
        case 'status_change': {
            const p = entry.payload || {};
            content.innerHTML = `Status: <span class="status-pill">${escapeHtml(p.from || '')}</span> → <span class="status-pill">${escapeHtml(p.to || '')}</span>`;
            break;
        }
        case 'label_change':
            content.textContent = `Labels: ${(entry.payload?.labels || []).join(', ') || '(none)'}`;
            break;
        default:
            content.textContent = JSON.stringify(entry.payload || {});
    }

    body.appendChild(content);
    return el('div', { class: 'tl-entry' }, [marker, body]);
}

function renderCallBlock(call, transcriptSegments) {
    const box = el('div', { class: 'call-detail' });
    box.appendChild(el('div', { class: 'call-row' }, [
        el('span', {}, call.direction === 'outbound' ? 'Outbound ▸' : 'Inbound ◂'),
        el('span', {}, call.caller_id || '—'),
        el('span', {}, fmtDuration(call.duration_seconds)),
        el('span', {}, call.was_bridged ? 'bridged' : 'no bridge'),
    ]));

    const audio = el('div', { class: 'audio-links' });
    if (call.caller_recording_path)
        audio.appendChild(el('a', { href: `/api/recordings/${call.id}/caller`, target: '_blank' }, '▶ caller'));
    if (call.tech_recording_path)
        audio.appendChild(el('a', { href: `/api/recordings/${call.id}/tech`, target: '_blank' }, '▶ tech'));
    if (call.voicemail_path)
        audio.appendChild(el('a', { href: `/api/recordings/${call.id}/voicemail`, target: '_blank' }, '▶ voicemail'));
    if (audio.childNodes.length) box.appendChild(audio);

    if (transcriptSegments.length) {
        const tr = el('div', { class: 'transcript-block' });
        for (const seg of transcriptSegments) {
            tr.appendChild(el('div', { class: 'transcript-line' }, [
                el('span', { class: 't-time' }, fmtTranscriptTime(seg.timestamp_offset)),
                el('span', { class: `t-speaker ${seg.speaker}` }, seg.speaker),
                el('span', { class: 't-text' }, seg.text),
            ]));
        }
        box.appendChild(tr);
    }

    return box;
}

function renderEmailBlock(email) {
    const box = el('div', {});
    box.appendChild(el('div', { class: 'call-row' }, [
        el('span', {}, email.direction === 'out' ? 'OUT ▸' : 'IN ◂'),
        el('span', {}, email.from_addr || '—'),
        el('span', { class: `status-pill ${email.status}` }, email.status || ''),
    ]));
    box.appendChild(el('div', { class: 'tl-content', style: 'margin-top:6px;' },
        email.subject || '(no subject)'));
    if (email.body_text) {
        box.appendChild(el('div', {
            class: 'tl-content preview', style: 'margin-top:4px; max-height:200px; overflow-y:auto;',
        }, email.body_text.slice(0, 1200)));
    }
    return box;
}

// ============ Detail: email (pending / flagged) ============
async function selectEmail(emailId) {
    state.selectedKind = 'email';
    state.selectedId = emailId;
    loadQueue();
    try {
        const em = await api(`/emails/${emailId}`);
        renderEmailDetail(em);
        renderEmailContext(em);
    } catch (e) {
        $('detail-body').innerHTML = `<div class="empty-state">Error: ${escapeHtml(e.message)}</div>`;
    }
}

function renderEmailDetail(em) {
    const header = $('detail-header');
    clear(header);
    header.appendChild(el('div', { class: 'detail-title' }, [
        el('span', {}, em.subject || '(no subject)'),
        el('span', { class: 'detail-id' }, `#${em.id}`),
    ]));
    header.appendChild(el('div', { class: 'detail-meta' }, [
        el('span', { class: `status-pill ${em.status}` }, em.status),
        el('span', {}, `from: ${em.from_addr}`),
        el('span', {}, fmtTime(em.received_at)),
        em.status_reason ? el('span', { class: 'status-pill' }, em.status_reason) : null,
    ]));

    const actions = $('detail-actions');
    clear(actions);
    if (em.status === 'pending' || em.status === 'flagged') {
        actions.appendChild(el('button', { class: 'primary',
            onclick: () => createIncidentFromEmail(em.id),
        }, 'Create ticket'));
        actions.appendChild(el('button', {
            onclick: () => rejectEmailPrompt(em.id),
        }, 'Reject'));
    }
    if (em.status === 'flagged' || em.status === 'rejected') {
        actions.appendChild(el('button', {
            onclick: () => markEmailSafe(em.id),
        }, 'Mark safe'));
    }

    const body = $('detail-body');
    clear(body);
    body.appendChild(el('div', { class: 'tl-content', style: 'font-family:var(--mono); background: var(--bg-elev); padding: 16px; border: 1px solid var(--border); border-radius: 6px;' },
        em.body_text || '(empty body)'));
}

function renderEmailContext(em) {
    const ctx = $('context-section');
    clear(ctx);
    ctx.appendChild(el('div', { class: 'ctx-block' }, [
        el('h3', {}, 'From'),
        el('div', { class: 'ctx-item' }, el('span', { class: 'ci-main' }, em.from_addr)),
    ]));
    ctx.appendChild(el('div', { class: 'ctx-block' }, [
        el('h3', {}, 'Headers'),
        el('div', { class: 'ctx-item' }, [
            el('span', { class: 'ci-extra' }, 'to'),
            el('span', { class: 'ci-main' }, em.to_addr || '—'),
        ]),
        em.in_reply_to ? el('div', { class: 'ctx-item' }, [
            el('span', { class: 'ci-extra' }, 'in-reply-to'),
            el('span', { class: 'ci-main' }, em.in_reply_to),
        ]) : null,
    ]));
}

// ============ Detail: active call ============
async function selectActiveCall(callId) {
    state.selectedKind = 'call';
    state.selectedId = callId;
    loadQueue();

    // If the call is linked to an incident, just switch to that incident view.
    try {
        const c = await api(`/history/${callId}`);
        if (c.incident_id) {
            selectIncident(c.incident_id);
            return;
        }
        // Fallback: render a minimal live transcript view
        const header = $('detail-header');
        clear(header);
        header.appendChild(el('div', { class: 'detail-title' }, [
            el('span', {}, c.caller_id || 'Active call'),
            el('span', { class: 'detail-id' }, callId.slice(0, 8)),
        ]));
        $('detail-body').innerHTML = '<div class="empty-state">Live transcript will appear here.</div>';
    } catch (e) {
        console.error(e);
    }
}

// ============ Detail: contact ============
async function selectContact(contactId) {
    state.selectedKind = 'contact';
    state.selectedId = contactId;
    loadQueue();
    try {
        const c = await api(`/contacts/${contactId}`);
        const header = $('detail-header');
        clear(header);
        header.appendChild(el('div', { class: 'detail-title' }, [
            el('span', {}, c.display_name || '(unnamed)'),
            el('span', { class: 'detail-id' }, c.id),
        ]));
        header.appendChild(el('div', { class: 'detail-meta' }, [
            el('span', {}, `${(c.incidents || []).length} tickets`),
        ]));

        $('detail-actions').innerHTML = '';

        const body = $('detail-body');
        clear(body);
        const list = el('div', { class: 'timeline' });
        for (const inc of c.incidents || []) {
            list.appendChild(el('div', {
                class: 'ctx-incident-item',
                onclick: () => selectIncident(inc.id),
            }, [
                el('div', {}, `${inc.id}  ${inc.subject || ''}`),
                el('div', { class: 'ci-sub' }, `${inc.status} · ${fmtTime(inc.updated_at)}`),
            ]));
        }
        body.appendChild(list);

        renderContactContext(c, null);
    } catch (e) { console.error(e); }
}

// ============ Context panel for contact ============
function renderContactContext(contact, incident) {
    const ctx = $('context-section');
    clear(ctx);
    if (!contact) {
        ctx.appendChild(el('div', { class: 'empty-state' }, 'No contact linked.'));
        return;
    }

    ctx.appendChild(el('div', { class: 'ctx-block' }, [
        el('h3', {}, 'Contact'),
        el('div', { class: 'ctx-item' }, [
            el('span', { class: 'ci-main' }, contact.id),
            el('span', { class: 'ci-extra' }, contact.display_name || '(unnamed)'),
        ]),
    ]));

    if (contact.phones?.length) {
        const phones = el('div', { class: 'ctx-block' }, [el('h3', {}, 'Phones')]);
        for (const p of contact.phones) {
            phones.appendChild(el('div', { class: 'ctx-item' }, [
                el('span', { class: 'ci-main' }, p.e164),
                el('span', { class: p.consented_at ? 'consent-yes' : 'consent-no' },
                    p.consented_at ? '✓ consented' : '— no consent'),
            ]));
        }
        ctx.appendChild(phones);
    }

    if (contact.emails?.length) {
        const emails = el('div', { class: 'ctx-block' }, [el('h3', {}, 'Emails')]);
        for (const e of contact.emails) {
            emails.appendChild(el('div', { class: 'ctx-item' }, [
                el('span', { class: 'ci-main' }, e.address),
                el('span', { class: e.consented_at ? 'consent-yes' : 'consent-no' },
                    e.consented_at ? '✓' : '—'),
            ]));
        }
        ctx.appendChild(emails);
    }

    // Related incidents (only when we have an incident context)
    if (incident && contact.incidents?.length > 1) {
        const related = el('div', { class: 'ctx-block' }, [el('h3', {}, 'Other tickets')]);
        for (const inc of contact.incidents) {
            if (inc.id === incident.id) continue;
            related.appendChild(el('div', {
                class: 'ctx-incident-item',
                onclick: () => selectIncident(inc.id),
            }, [
                el('div', {}, inc.id),
                el('div', { class: 'ci-sub' }, (inc.subject || '').slice(0, 40)),
            ]));
        }
        ctx.appendChild(related);
    }
}

// ============ Action helpers (bridged to backend) ============
async function callbackContact(inc) {
    if (!inc.contact_id) return alert('This incident has no contact linked.');
    const c = await api(`/contacts/${inc.contact_id}`);
    if (!c.phones?.length) return alert('Contact has no phone number on file.');
    const phone = c.phones[0].e164;
    if (!confirm(`Place an outbound callback to ${phone}?\n\nYour cell will ring first — press 1 to proceed, then Callen dials the contact.`))
        return;
    try {
        await api('/call/originate', {
            method: 'POST',
            body: JSON.stringify({
                incident_id: inc.id,
                destination: phone,
                display_name: c.display_name || '',
            }),
        });
        alert('Callback initiated. Your cell should ring shortly.');
    } catch (e) {
        alert(`Failed: ${e.message}`);
    }
}

async function addNotePrompt(incidentId) {
    const text = prompt('Note text:');
    if (!text) return;
    try {
        await api(`/incidents/${incidentId}/notes`, {
            method: 'POST',
            body: JSON.stringify({ text, author: 'operator' }),
        });
        selectIncident(incidentId);
    } catch (e) { alert(`Failed: ${e.message}`); }
}

async function updateStatusPrompt(incidentId, current) {
    const choices = ['open', 'in_progress', 'waiting', 'resolved', 'closed'];
    const next = prompt(`New status (${choices.join(', ')}):`, current);
    if (!next || !choices.includes(next)) return;
    try {
        await api(`/incidents/${incidentId}`, {
            method: 'PATCH',
            body: JSON.stringify({ status: next }),
        });
        selectIncident(incidentId);
        refreshCounts();
    } catch (e) { alert(`Failed: ${e.message}`); }
}

async function createIncidentFromEmail(emailId) {
    // Use the agent to do this via the CLI — keeps logic in one place.
    // (The backend doesn't yet have a REST endpoint for this, so we just fall
    // back to a manual Python call through the agent.)
    const subject = prompt('Ticket subject (leave empty to use email subject):', '');
    sendAgentPrompt(`Route email ${emailId} into a new incident${subject ? ` with subject "${subject}"` : ''}. Use ./tools/assign-email ${emailId} --create-incident${subject ? ` --subject "${subject}"` : ''}.`, { email_id: emailId });
}

async function rejectEmailPrompt(emailId) {
    const reason = prompt('Reject reason:', 'marketing');
    if (!reason) return;
    sendAgentPrompt(`Reject email ${emailId} with reason "${reason}". Use ./tools/reject-email ${emailId} --reason "${reason}".`, { email_id: emailId });
}

async function markEmailSafe(emailId) {
    sendAgentPrompt(`Mark email ${emailId} as safe. Use ./tools/mark-safe ${emailId}.`, { email_id: emailId });
}

// ============ Agent prompt bar ============
$('prompt-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const input = $('prompt-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    await sendAgentPrompt(text, collectContext());
});

function collectContext() {
    const ctx = { view: state.currentTab };
    if (state.selectedKind === 'incident' && state.selectedId) ctx.incident_id = state.selectedId;
    if (state.selectedKind === 'contact' && state.selectedId) ctx.contact_id = state.selectedId;
    if (state.selectedKind === 'email' && state.selectedId) ctx.email_id = state.selectedId;
    if (state.selectedKind === 'call' && state.selectedId) ctx.call_id = state.selectedId;
    return ctx;
}

async function sendAgentPrompt(text, context) {
    showAgentDrawer();
    setAgentStatus('running', 'Running…');
    clearAgentBody();
    appendAgentLine(`▸ ${text}`, 'assistant');

    try {
        const resp = await api('/agent', {
            method: 'POST',
            body: JSON.stringify({ prompt: text, context }),
        });
        state.currentAgentRunId = resp.run_id;
        $('agent-run-id').textContent = resp.run_id;
        connectAgentWs(resp.run_id);
    } catch (e) {
        setAgentStatus('error', 'Error');
        appendAgentLine(`error: ${e.message}`, 'error');
    }
}

function connectAgentWs(runId) {
    if (state.agentWs) { try { state.agentWs.close(); } catch {} }
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/agent/${runId}`);
    state.agentWs = ws;
    ws.onmessage = (evt) => {
        let data;
        try { data = JSON.parse(evt.data); } catch { return; }
        handleAgentEvent(data);
    };
    ws.onerror = () => { setAgentStatus('error', 'Connection error'); };
    ws.onclose = () => {};
}

function handleAgentEvent(ev) {
    if (!ev || typeof ev !== 'object') return;
    switch (ev.type) {
        case 'assistant': {
            const msg = ev.message || {};
            for (const block of msg.content || []) {
                if (block.type === 'text' && block.text) {
                    appendAgentLine(block.text, 'assistant');
                } else if (block.type === 'tool_use') {
                    const inp = block.input || {};
                    if (block.name === 'Bash' && inp.command) {
                        appendAgentLine(`$ ${inp.command}`, 'tool');
                    } else {
                        appendAgentLine(`[${block.name}] ${JSON.stringify(inp).slice(0, 120)}`, 'tool');
                    }
                }
            }
            break;
        }
        case 'user': {
            // Tool results
            const msg = ev.message || {};
            for (const block of msg.content || []) {
                if (block.type === 'tool_result') {
                    const out = typeof block.content === 'string'
                        ? block.content
                        : (block.content || []).map((b) => b.text || '').join('\n');
                    if (out) appendAgentLine(out, 'tool-result');
                }
            }
            break;
        }
        case 'result':
            if (ev.result) appendAgentLine(ev.result, 'assistant');
            break;
        case 'complete':
            setAgentStatus(ev.status === 'done' ? 'done' : 'error',
                           ev.status === 'done' ? 'Done' : `Error: ${ev.error || ''}`);
            if (state.agentWs) state.agentWs.close();
            // Refresh everything — the agent may have changed state
            refreshCounts();
            loadQueue();
            if (state.selectedKind === 'incident' && state.selectedId) {
                selectIncident(state.selectedId);
            }
            break;
        case 'error':
            setAgentStatus('error', 'Error');
            appendAgentLine(ev.message || 'unknown error', 'error');
            break;
    }
}

function showAgentDrawer() { $('agent-drawer').classList.remove('hidden'); }
function hideAgentDrawer() { $('agent-drawer').classList.add('hidden'); }

function setAgentStatus(kind, text) {
    const el = $('agent-status');
    el.textContent = text;
    el.className = `agent-status ${kind}`;
}

function clearAgentBody() { $('agent-drawer-body').innerHTML = ''; }

function appendAgentLine(text, kind) {
    const line = el('div', { class: `agent-line ${kind || ''}` }, String(text));
    $('agent-drawer-body').appendChild(line);
    const body = $('agent-drawer-body');
    body.scrollTop = body.scrollHeight;
}

$('agent-close').addEventListener('click', hideAgentDrawer);

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
        e.preventDefault();
        $('prompt-input').focus();
    } else if (e.key === 'Escape') {
        hideAgentDrawer();
    }
});

// ============ Live calls WebSocket (refreshes the queue) ============
function connectCallsWs() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/calls`);
    state.callsWs = ws;

    ws.onmessage = () => {
        refreshCounts();
        if (state.currentTab === 'active-calls' || state.currentTab === 'incidents') loadQueue();
    };
    ws.onclose = () => setTimeout(connectCallsWs, 3000);
}

// ============ Init ============
async function init() {
    loadOperatorStatus();
    await refreshCounts();
    await loadQueue();
    connectCallsWs();
    setInterval(refreshCounts, 15000);
}

init();
