# Callen GPL3
# Copyright (C) 2020 David Hamner
# Licensed under GNU General Public License v3

"""
Callen command-line interface — for agents and humans.

Agents running in the project folder call thin bash wrappers in `tools/`
that dispatch here. All subcommands default to JSON output on stdout
(easy to pipe into jq or parse with Python). Pass --pretty for a
human-readable format where supported.

Every command returns exit 0 on success, 1 on not-found/invalid-arg,
2 on internal error. Errors go to stderr.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from callen.config import load_config
from callen.storage.db import Database, normalize_phone
from callen.storage.models import CallRecord

# --- Helpers ---


def _out(data, pretty: bool = False):
    """Write a Python value to stdout as JSON."""
    if pretty:
        json.dump(data, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        json.dump(data, sys.stdout, default=str)
        sys.stdout.write("\n")


def _err(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _db(args) -> Database:
    config = load_config(args.config)
    db = Database(config.general.db_path)
    db.initialize()
    return db


def _human_incident(inc: dict) -> str:
    return (
        f"{inc['id']}  [{inc['status']}/{inc['priority']}]  "
        f"{inc.get('subject', '')}  "
        f"contact={inc.get('contact_id') or '-'}  "
        f"updated={time.strftime('%Y-%m-%d %H:%M', time.localtime(inc['updated_at']))}"
    )


def _human_contact(c: dict) -> str:
    phones = c.get("phones", "") or ""
    emails = c.get("emails", "") or ""
    name = c.get("display_name") or "(unnamed)"
    return f"{c['id']}  {name}  phones=[{phones}]  emails=[{emails}]"


# --- Incident commands ---


def cmd_list_incidents(args):
    db = _db(args)
    rows = db.list_incidents(
        status=args.status, contact_id=args.contact,
        limit=args.limit, offset=args.offset,
    )
    if args.pretty:
        if not rows:
            print("(no incidents)")
            return
        for r in rows:
            print(_human_incident(r))
        return
    _out(rows)


def cmd_get_incident(args):
    db = _db(args)
    inc = db.get_incident(args.incident_id)
    if not inc:
        _err(f"incident not found: {args.incident_id}")
    inc["entries"] = db.list_incident_entries(args.incident_id)
    inc["calls"] = db.get_calls_for_incident(args.incident_id)
    inc["transcript"] = db.get_transcript_for_incident(args.incident_id)
    if inc.get("contact_id"):
        inc["contact"] = db.get_contact(inc["contact_id"])
    _out(inc, pretty=args.pretty)


def cmd_update_incident(args):
    db = _db(args)
    add_labels = [l.strip() for l in args.add_label.split(",")] if args.add_label else None
    remove_labels = [l.strip() for l in args.remove_label.split(",")] if args.remove_label else None
    ok = db.update_incident(
        args.incident_id,
        status=args.status,
        priority=args.priority,
        subject=args.subject,
        assigned_to=args.assigned_to,
        add_labels=add_labels,
        remove_labels=remove_labels,
    )
    if not ok:
        _err(f"incident not found: {args.incident_id}")
    inc = db.get_incident(args.incident_id)
    _out(inc, pretty=args.pretty)


def cmd_note_incident(args):
    db = _db(args)
    inc = db.get_incident(args.incident_id)
    if not inc:
        _err(f"incident not found: {args.incident_id}")
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    entry_id = db.add_incident_entry(
        args.incident_id, "note", author=args.author,
        payload={"text": text},
    )
    _out({"entry_id": entry_id, "incident_id": args.incident_id})


def cmd_create_incident(args):
    db = _db(args)
    contact_id = args.contact
    if not contact_id and args.phone:
        contact_id, _ = db.upsert_contact_by_phone(args.phone)
    elif not contact_id and args.email:
        contact_id = db.upsert_contact_by_email(args.email)
    incident_id = db.create_incident(
        contact_id=contact_id,
        subject=args.subject or "",
        channel=args.channel,
        status=args.status,
        priority=args.priority,
    )
    _out(db.get_incident(incident_id), pretty=args.pretty)


# --- Contact commands ---


def cmd_list_contacts(args):
    db = _db(args)
    rows = db.list_contacts(limit=args.limit, offset=args.offset)
    if args.pretty:
        if not rows:
            print("(no contacts)")
            return
        for r in rows:
            print(_human_contact(r))
        return
    _out(rows)


def cmd_get_contact(args):
    db = _db(args)
    c = db.get_contact(args.contact_id)
    if not c:
        _err(f"contact not found: {args.contact_id}")
    # Include incidents for this contact
    c["incidents"] = db.list_incidents(contact_id=args.contact_id, limit=100)
    _out(c, pretty=args.pretty)


def cmd_create_contact(args):
    db = _db(args)
    contact_id = None
    if args.phone:
        contact_id, _ = db.upsert_contact_by_phone(args.phone, display_name=args.name or "")
    if args.email:
        if contact_id:
            # Associate the email with the existing contact
            db._conn().execute(
                "INSERT OR IGNORE INTO contact_emails (contact_id, address, created_at) VALUES (?, ?, unixepoch('subsec'))",
                (contact_id, args.email.strip().lower()),
            )
            db._conn().commit()
        else:
            contact_id = db.upsert_contact_by_email(args.email, display_name=args.name or "")
    if not contact_id:
        _err("must provide --phone or --email")
    if args.name:
        db.update_contact(contact_id, display_name=args.name)
    _out(db.get_contact(contact_id), pretty=args.pretty)


def cmd_update_contact(args):
    db = _db(args)
    c = db.get_contact(args.contact_id)
    if not c:
        _err(f"contact not found: {args.contact_id}")
    db.update_contact(args.contact_id, display_name=args.name, notes=args.notes)
    _out(db.get_contact(args.contact_id), pretty=args.pretty)


def cmd_contact_consent(args):
    db = _db(args)
    c = db.get_contact(args.contact_id)
    if not c:
        _err(f"contact not found: {args.contact_id}")

    if args.phone:
        e164 = normalize_phone(args.phone) or args.phone
        db.record_phone_consent(e164, source=args.source)
    elif args.email:
        db._conn().execute(
            """UPDATE contact_emails SET
                 consented_at = COALESCE(consented_at, unixepoch('subsec')),
                 consent_source = COALESCE(consent_source, ?)
               WHERE address = ?""",
            (args.source, args.email.strip().lower()),
        )
        db._conn().commit()
    else:
        _err("must provide --phone or --email")

    _out(db.get_contact(args.contact_id), pretty=args.pretty)


# --- Call / transcript / audio commands ---


def cmd_list_calls(args):
    db = _db(args)
    rows = db.get_call_history(limit=args.limit, offset=args.offset)
    _out(rows, pretty=args.pretty)


def cmd_get_transcript(args):
    db = _db(args)
    if args.incident_id:
        segments = db.get_transcript_for_incident(args.incident_id)
    elif args.call_id:
        segments = db.get_transcript(args.call_id)
    else:
        _err("must provide --incident or --call")

    if args.text:
        # Plain-text formatted transcript, one line per utterance
        for s in segments:
            ts = f"{int(s['timestamp_offset'] // 60):02d}:{int(s['timestamp_offset'] % 60):02d}"
            print(f"[{ts}] {s['speaker']}: {s['text']}")
    else:
        _out(segments)


def cmd_get_audio(args):
    db = _db(args)
    # Resolve audio source — either a specific call or the first call in an incident
    call = None
    if args.call_id:
        call = db.get_call(args.call_id)
    elif args.incident_id:
        calls = db.get_calls_for_incident(args.incident_id)
        if calls:
            call = calls[0]
    if not call:
        _err("no call found for the given id")

    # Pick channel
    channel = args.channel
    path_key_map = {
        "caller": "caller_recording_path",
        "tech": "tech_recording_path",
        "voicemail": "voicemail_path",
    }
    if channel not in path_key_map:
        _err(f"invalid channel: {channel}")

    src = call.get(path_key_map[channel])
    if not src:
        _err(f"no {channel} recording for call {call['id']}")
    src_path = Path(src)
    if not src_path.exists():
        _err(f"recording file missing on disk: {src_path}")

    if args.out:
        dst = Path(args.out)
        shutil.copy2(src_path, dst)
        _out({"copied_to": str(dst), "size_bytes": dst.stat().st_size})
    else:
        # Just print the path
        print(str(src_path))


# --- Outbound call (stub, wired in Phase 9) ---


def cmd_originate(args):
    _err("call originate is wired in Phase 9 — not yet available", code=2)


# --- Argument parsing ---


def build_parser() -> argparse.ArgumentParser:
    # Common flags that apply to every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default="config.toml",
        help="Path to config.toml (default: config.toml)",
    )
    common.add_argument(
        "--pretty", action="store_true",
        help="Human-readable output where supported",
    )

    p = argparse.ArgumentParser(
        prog="callen",
        description="Callen ticketing/CRM command-line interface",
        parents=[common],
    )

    sub = p.add_subparsers(dest="subcommand", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    # incident list
    pp = sub.add_parser("list-incidents", help="List incidents / tickets")
    pp.add_argument("--status", help="filter by status")
    pp.add_argument("--contact", help="filter by contact id")
    pp.add_argument("--limit", type=int, default=100)
    pp.add_argument("--offset", type=int, default=0)
    pp.set_defaults(func=cmd_list_incidents)

    pp = sub.add_parser("get-incident", help="Fetch one incident with full context")
    pp.add_argument("incident_id")
    pp.set_defaults(func=cmd_get_incident)

    pp = sub.add_parser("update-incident", help="Update incident fields")
    pp.add_argument("incident_id")
    pp.add_argument("--status", choices=["open", "in_progress", "waiting", "resolved", "closed"])
    pp.add_argument("--priority", choices=["low", "normal", "high", "urgent"])
    pp.add_argument("--subject")
    pp.add_argument("--assigned-to", dest="assigned_to")
    pp.add_argument("--add-label", help="comma-separated labels to add")
    pp.add_argument("--remove-label", help="comma-separated labels to remove")
    pp.set_defaults(func=cmd_update_incident)

    pp = sub.add_parser("note-incident", help="Add an internal note to an incident")
    pp.add_argument("incident_id")
    pp.add_argument("text", help="note text (use - to read from stdin)")
    pp.add_argument("--author", default="agent")
    pp.set_defaults(func=cmd_note_incident)

    pp = sub.add_parser("create-incident", help="Manually create an incident")
    pp.add_argument("--contact", help="existing contact id")
    pp.add_argument("--phone", help="phone (creates/links contact)")
    pp.add_argument("--email", help="email (creates/links contact)")
    pp.add_argument("--subject")
    pp.add_argument("--channel", default="manual", choices=["phone", "email", "manual"])
    pp.add_argument("--status", default="open")
    pp.add_argument("--priority", default="normal")
    pp.set_defaults(func=cmd_create_incident)

    # contact
    pp = sub.add_parser("list-contacts", help="List contacts")
    pp.add_argument("--limit", type=int, default=100)
    pp.add_argument("--offset", type=int, default=0)
    pp.set_defaults(func=cmd_list_contacts)

    pp = sub.add_parser("get-contact", help="Fetch one contact with incidents")
    pp.add_argument("contact_id")
    pp.set_defaults(func=cmd_get_contact)

    pp = sub.add_parser("create-contact", help="Create a contact")
    pp.add_argument("--name", help="display name")
    pp.add_argument("--phone", help="phone number")
    pp.add_argument("--email", help="email address")
    pp.set_defaults(func=cmd_create_contact)

    pp = sub.add_parser("update-contact", help="Update contact fields")
    pp.add_argument("contact_id")
    pp.add_argument("--name", dest="name", help="display name")
    pp.add_argument("--notes", help="free-form notes")
    pp.set_defaults(func=cmd_update_contact)

    pp = sub.add_parser("contact-consent", help="Record consent for a contact")
    pp.add_argument("contact_id")
    pp.add_argument("--phone", help="which phone number consented")
    pp.add_argument("--email", help="which email consented")
    pp.add_argument("--source", default="manual")
    pp.set_defaults(func=cmd_contact_consent)

    # calls
    pp = sub.add_parser("list-calls", help="List raw call records")
    pp.add_argument("--limit", type=int, default=50)
    pp.add_argument("--offset", type=int, default=0)
    pp.set_defaults(func=cmd_list_calls)

    # transcript
    pp = sub.add_parser("get-transcript", help="Get a transcript")
    g = pp.add_mutually_exclusive_group(required=True)
    g.add_argument("--incident", dest="incident_id")
    g.add_argument("--call", dest="call_id")
    pp.add_argument("--text", action="store_true", help="plain text instead of JSON")
    pp.set_defaults(func=cmd_get_transcript)

    # audio
    pp = sub.add_parser("get-audio", help="Download / locate a call recording")
    g = pp.add_mutually_exclusive_group(required=True)
    g.add_argument("--incident", dest="incident_id")
    g.add_argument("--call", dest="call_id")
    pp.add_argument(
        "--channel", default="caller",
        choices=["caller", "tech", "voicemail"],
    )
    pp.add_argument("--out", help="copy the WAV to this path")
    pp.set_defaults(func=cmd_get_audio)

    # originate
    pp = sub.add_parser("originate", help="Originate an outbound call (Phase 9)")
    pp.add_argument("incident_id")
    pp.set_defaults(func=cmd_originate)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
