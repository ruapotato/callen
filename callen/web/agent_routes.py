# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Web routes for the agent runner.

POST /api/agent            — start a new run, returns {run_id}
GET  /api/agent/runs       — list recent runs
GET  /api/agent/runs/<id>  — replay one run (full event log + status)
WS   /ws/agent/<run_id>    — live event stream for a run
"""

import asyncio
import json
import logging

from quart import Blueprint, current_app, jsonify, request, websocket

log = logging.getLogger(__name__)
bp = Blueprint("agent", __name__)


def _runner():
    return current_app.config.get("agent_runner")


@bp.route("/api/agent", methods=["POST"])
async def start_agent_run():
    runner = _runner()
    if runner is None:
        return jsonify({"error": "agent runner not configured"}), 503

    data = await request.get_json()
    if not data:
        return jsonify({"error": "missing body"}), 400

    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    context = data.get("context") or {}
    if not isinstance(context, dict):
        context = {}

    run = await runner.start(prompt, context)
    return jsonify({
        "run_id": run.run_id,
        "status": run.status,
        "started_at": run.started_at,
    })


@bp.route("/api/agent/runs")
async def list_agent_runs():
    runner = _runner()
    if runner is None:
        return jsonify([])
    limit = request.args.get("limit", 20, type=int)
    return jsonify(runner.list_runs(limit=limit))


@bp.route("/api/agent/runs/<run_id>")
async def get_agent_run(run_id):
    runner = _runner()
    if runner is None:
        return jsonify({"error": "agent runner not configured"}), 503
    run = runner.get_run(run_id)
    if run is None:
        return jsonify({"error": "run not found"}), 404
    return jsonify({
        "run_id": run.run_id,
        "prompt": run.prompt,
        "context": run.context,
        "status": run.status,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "result": run.result_text,
        "error": run.error,
        "events": run.events,
    })


@bp.websocket("/ws/agent/<run_id>")
async def ws_agent(run_id):
    """Stream events for one run to the browser.

    The queue is pre-populated with any already-seen events so reconnections
    don't lose state. The socket closes when the run's 'complete' event
    arrives, but the client can always refetch via GET /api/agent/runs/<id>.
    """
    runner = _runner()
    if runner is None:
        await websocket.close(1011, "agent runner not configured")
        return

    queue = await runner.subscribe(run_id)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # Heartbeat so NAT / idle timeouts don't kill us
                await websocket.send(json.dumps({"type": "heartbeat"}))
                continue

            await websocket.send(json.dumps(event))

            if isinstance(event, dict) and event.get("type") == "complete":
                break
    except asyncio.CancelledError:
        pass
    finally:
        await runner.unsubscribe(run_id, queue)
