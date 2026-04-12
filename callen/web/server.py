# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""Quart web server — serves REST API, WebSocket endpoints, and static frontend."""

import logging
from pathlib import Path

from quart import Quart

from callen.config import WebConfig
from callen.state.calls import CallRegistry
from callen.state.operator import OperatorState
from callen.state.events import EventBus
from callen.storage.db import Database

log = logging.getLogger(__name__)


def create_app(
    config: WebConfig,
    call_registry: CallRegistry,
    operator_state: OperatorState,
    event_bus: EventBus,
    db: Database,
) -> Quart:
    """Create and configure the Quart application."""
    static_dir = Path(__file__).parent / "static"
    app = Quart(__name__, static_folder=str(static_dir), static_url_path="/static")

    # Store shared objects on the app for access in routes
    app.config["call_registry"] = call_registry
    app.config["operator_state"] = operator_state
    app.config["event_bus"] = event_bus
    app.config["db"] = db

    # Register routes
    from callen.web.routes import bp as routes_bp
    from callen.web.websocket import bp as ws_bp
    app.register_blueprint(routes_bp)
    app.register_blueprint(ws_bp)

    return app
