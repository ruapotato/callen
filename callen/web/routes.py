# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""REST API endpoints."""

import logging
from pathlib import Path

from quart import Blueprint, current_app, jsonify, request, send_file

log = logging.getLogger(__name__)
bp = Blueprint("api", __name__)


def _registry():
    return current_app.config["call_registry"]

def _operator():
    return current_app.config["operator_state"]

def _db():
    return current_app.config["db"]


@bp.route("/")
async def index():
    """Serve the SPA."""
    static = Path(current_app.static_folder)
    return await send_file(static / "index.html")


@bp.route("/api/calls")
async def active_calls():
    """List active calls."""
    calls = _registry().active_calls()
    return jsonify([
        {
            "id": c.uuid,
            "caller_id": c.caller_id,
            "state": c.state.value,
            "duration": round(c.duration, 1),
            "consented": c.consented_to_recording,
        }
        for c in calls
    ])


@bp.route("/api/history")
async def call_history():
    """Paginated call history."""
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    records = _db().get_call_history(limit=limit, offset=offset)
    return jsonify(records)


@bp.route("/api/history/<call_id>")
async def call_detail(call_id):
    """Single call with transcript and notes."""
    record = _db().get_call(call_id)
    if not record:
        return jsonify({"error": "not found"}), 404
    record["transcript"] = _db().get_transcript(call_id)
    record["notes"] = _db().get_notes(call_id)
    return jsonify(record)


@bp.route("/api/history/<call_id>/notes", methods=["POST"])
async def add_note(call_id):
    """Add a note to a call."""
    data = await request.get_json()
    text = data.get("text", "").strip()
    author = data.get("author", "operator")
    if not text:
        return jsonify({"error": "text required"}), 400
    _db().save_note(call_id, text, author)
    return jsonify({"status": "ok"})


@bp.route("/api/operator/status")
async def get_operator_status():
    return jsonify({"status": _operator().status.value})


@bp.route("/api/operator/status", methods=["PUT"])
async def set_operator_status():
    data = await request.get_json()
    status_str = data.get("status", "")
    try:
        from callen.state.operator import OperatorStatus
        new_status = OperatorStatus(status_str)
    except ValueError:
        return jsonify({"error": "invalid status"}), 400
    _operator().set_status(new_status)
    return jsonify({"status": new_status.value})


@bp.route("/api/transcripts/<call_id>")
async def get_transcript(call_id):
    segments = _db().get_transcript(call_id)
    return jsonify(segments)


# --- Incident-centric routes (primary dashboard API) ---


@bp.route("/api/incidents")
async def list_incidents():
    """List incidents, filterable by status and contact."""
    status = request.args.get("status")
    contact_id = request.args.get("contact")
    limit = request.args.get("limit", 200, type=int)
    offset = request.args.get("offset", 0, type=int)
    rows = _db().list_incidents(
        status=status, contact_id=contact_id,
        limit=limit, offset=offset,
    )
    return jsonify(rows)


@bp.route("/api/incidents/<incident_id>")
async def incident_detail(incident_id):
    """Full incident context: timeline, calls, transcripts, contact, emails, todos."""
    db = _db()
    inc = db.get_incident(incident_id)
    if not inc:
        return jsonify({"error": "not found"}), 404

    inc["entries"] = db.list_incident_entries(incident_id)
    inc["calls"] = db.get_calls_for_incident(incident_id)
    inc["transcript"] = db.get_transcript_for_incident(incident_id)
    inc["emails"] = db.list_emails_for_incident(incident_id)
    inc["todos"] = db.list_todos(incident_id)
    if inc.get("contact_id"):
        inc["contact"] = db.get_contact(inc["contact_id"])
    return jsonify(inc)


# --- Todos ---


@bp.route("/api/todos")
async def list_all_todos():
    """Aggregate todos across all incidents.

    ?status=open (default)  — only undone todos
    ?status=done            — only completed
    ?status=all             — both
    """
    status = request.args.get("status", "open")
    limit = request.args.get("limit", 500, type=int)
    if status == "done":
        done = True
    elif status == "all":
        done = None
    else:
        done = False
    return jsonify(_db().list_all_todos(done=done, limit=limit))


@bp.route("/api/incidents/<incident_id>/todos")
async def list_incident_todos(incident_id):
    return jsonify(_db().list_todos(incident_id))


@bp.route("/api/incidents/<incident_id>/todos", methods=["POST"])
async def add_incident_todo(incident_id):
    data = await request.get_json() or {}
    text = (data.get("text") or "").strip()
    author = data.get("author", "operator")
    if not text:
        return jsonify({"error": "text required"}), 400
    db = _db()
    if not db.get_incident(incident_id):
        return jsonify({"error": "incident not found"}), 404
    todo_id = db.add_todo(incident_id, text, author=author)
    db.add_incident_entry(
        incident_id, "todo_added", author=author,
        payload={"todo_id": todo_id, "text": text},
    )
    return jsonify(db.get_todo(todo_id)), 201


@bp.route("/api/todos/<int:todo_id>", methods=["PATCH"])
async def update_todo(todo_id):
    data = await request.get_json() or {}
    db = _db()
    todo = db.get_todo(todo_id)
    if not todo:
        return jsonify({"error": "not found"}), 404

    done = data.get("done")
    text = data.get("text")
    db.update_todo(todo_id, text=text, done=done)
    if done is True:
        db.add_incident_entry(
            todo["incident_id"], "todo_done",
            author=data.get("author", "operator"),
            payload={"todo_id": todo_id, "text": todo["text"]},
        )
    return jsonify(db.get_todo(todo_id))


@bp.route("/api/todos/<int:todo_id>", methods=["DELETE"])
async def delete_todo(todo_id):
    db = _db()
    todo = db.get_todo(todo_id)
    if not todo:
        return jsonify({"error": "not found"}), 404
    db.delete_todo(todo_id)
    return jsonify({"deleted": todo_id})


@bp.route("/api/incidents/<incident_id>", methods=["PATCH"])
async def update_incident(incident_id):
    """Update status / priority / subject / labels."""
    data = await request.get_json() or {}
    db = _db()

    add_labels = data.get("add_labels") or None
    remove_labels = data.get("remove_labels") or None

    ok = db.update_incident(
        incident_id,
        status=data.get("status"),
        priority=data.get("priority"),
        subject=data.get("subject"),
        assigned_to=data.get("assigned_to"),
        add_labels=add_labels,
        remove_labels=remove_labels,
    )
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify(db.get_incident(incident_id))


@bp.route("/api/incidents/<incident_id>/notes", methods=["POST"])
async def incident_add_note(incident_id):
    data = await request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    db = _db()
    if not db.get_incident(incident_id):
        return jsonify({"error": "incident not found"}), 404
    entry_id = db.add_incident_entry(
        incident_id, "note",
        author=data.get("author", "operator"),
        payload={"text": text},
    )
    return jsonify({"entry_id": entry_id, "incident_id": incident_id})


# --- Contacts ---


@bp.route("/api/contacts")
async def list_contacts():
    limit = request.args.get("limit", 200, type=int)
    offset = request.args.get("offset", 0, type=int)
    return jsonify(_db().list_contacts(limit=limit, offset=offset))


@bp.route("/api/contacts/<contact_id>")
async def contact_detail(contact_id):
    c = _db().get_contact(contact_id)
    if not c:
        return jsonify({"error": "not found"}), 404
    c["incidents"] = _db().list_incidents(contact_id=contact_id, limit=100)
    return jsonify(c)


@bp.route("/api/contacts", methods=["POST"])
async def create_contact():
    """Create a new contact.

    Body: {name: str, phone?: str, email?: str, notes?: str}
    Returns the created contact (or the existing one if phone/email matched).
    """
    data = await request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    email = (data.get("email") or "").strip()
    notes = (data.get("notes") or "").strip()

    if not phone and not email:
        return jsonify({"error": "phone or email required"}), 400

    db = _db()
    contact_id = None

    if phone:
        contact_id, _ = db.upsert_contact_by_phone(phone, display_name=name)
    if email:
        if contact_id:
            # Attach email to existing contact
            conn = db._conn()
            conn.execute(
                "INSERT OR IGNORE INTO contact_emails (contact_id, address, created_at) VALUES (?, ?, unixepoch('subsec'))",
                (contact_id, email.lower()),
            )
            conn.commit()
        else:
            contact_id = db.upsert_contact_by_email(email, display_name=name)

    if name or notes:
        db.update_contact(
            contact_id,
            display_name=name or None,
            notes=notes or None,
        )

    return jsonify(db.get_contact(contact_id)), 201


# --- Email queues ---


@bp.route("/api/emails")
async def list_emails():
    """List emails by status. Defaults to 'pending'."""
    status = request.args.get("status", "pending")
    limit = request.args.get("limit", 100, type=int)
    return jsonify(_db().list_emails_by_status(status, limit=limit))


@bp.route("/api/emails/<int:email_id>")
async def get_email(email_id):
    e = _db().get_email(email_id)
    if not e:
        return jsonify({"error": "not found"}), 404
    e["attachments"] = _db().list_email_attachments(email_id)
    return jsonify(e)


@bp.route("/api/attachments/<int:attachment_id>")
async def download_attachment(attachment_id):
    """Serve the raw attachment file."""
    att = _db().get_email_attachment(attachment_id)
    if not att:
        return jsonify({"error": "not found"}), 404
    path = att.get("file_path")
    if not path or not Path(path).exists():
        return jsonify({"error": "file missing on disk"}), 404
    return await send_file(
        path,
        mimetype=att.get("content_type") or "application/octet-stream",
    )


@bp.route("/api/call/originate", methods=["POST"])
async def originate_call():
    """Kick off a technician-first outbound call.

    POST body:
      {"incident_id": "INC-0042", "destination": "15551234567",
       "display_name": "Jane Doe"}
    OR, to cold-call a contact without an existing ticket:
      {"contact_id": "CON-0007"}
      — destination defaults to the contact's first phone, and a fresh
        incident is auto-created and linked.

    The operator's cell rings first; after DTMF 1 confirmation, the contact
    is dialed and the two legs are bridged.
    """
    data = await request.get_json()
    if not data:
        return jsonify({"error": "missing body"}), 400

    db = _db()
    incident_id = data.get("incident_id")
    destination = data.get("destination")
    display_name = data.get("display_name") or ""
    contact_id = data.get("contact_id")

    # If only a contact_id was given, resolve the destination from their
    # first phone and auto-create an incident on that contact.
    if contact_id and not incident_id:
        contact = db.get_contact(contact_id)
        if not contact:
            return jsonify({"error": "contact not found"}), 404
        if not destination:
            phones = contact.get("phones") or []
            if not phones:
                return jsonify({"error": "contact has no phone on file"}), 400
            destination = phones[0]["e164"]
        if not display_name:
            display_name = contact.get("display_name") or ""

        # Create a fresh incident attached to this contact
        incident_id = db.create_incident(
            contact_id=contact_id,
            subject=f"Outbound call to {display_name or destination}",
            channel="phone",
            status="open",
        )
        db.add_incident_entry(
            incident_id, "note", author="operator",
            payload={"text": f"Outbound call initiated from dashboard to {destination}"},
        )

    if not incident_id or not destination:
        return jsonify({"error": "incident_id/contact_id and destination required"}), 400

    # Validate the incident exists
    inc = db.get_incident(incident_id)
    if not inc:
        return jsonify({"error": "incident not found"}), 404

    from callen.ivr import outbound
    outbound.originate(incident_id, destination, display_name)
    return jsonify({
        "status": "initiated",
        "incident_id": incident_id,
        "destination": destination,
        "display_name": display_name,
    })


@bp.route("/api/recordings/<call_id>/<channel>")
async def get_recording(call_id, channel):
    """Download a recording WAV. Channel is 'caller' or 'tech'."""
    record = _db().get_call(call_id)
    if not record:
        return jsonify({"error": "not found"}), 404

    if channel == "caller":
        path = record.get("caller_recording_path")
    elif channel == "tech":
        path = record.get("tech_recording_path")
    elif channel == "voicemail":
        path = record.get("voicemail_path")
    else:
        return jsonify({"error": "invalid channel"}), 400

    if not path or not Path(path).exists():
        return jsonify({"error": "recording not found"}), 404

    return await send_file(path, mimetype="audio/wav")
