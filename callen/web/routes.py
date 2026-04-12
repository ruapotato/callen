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
