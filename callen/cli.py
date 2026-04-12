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


# --- Operator status ---


def cmd_get_operator_status(args):
    db = _db(args)
    _out({"status": db.get_operator_status()}, pretty=args.pretty)


def cmd_set_operator_status(args):
    db = _db(args)
    if args.status not in ("available", "busy", "dnd"):
        _err(f"invalid status: {args.status}")
    db.set_operator_status(args.status)
    _out({"status": args.status})


# --- Merge operations ---


def cmd_merge_contacts(args):
    """Merge source contact into destination. Phones, emails, and incidents
    move to the destination; the source contact row is deleted."""
    db = _db(args)
    src = db.get_contact(args.source)
    dst = db.get_contact(args.destination)
    if not src:
        _err(f"source contact not found: {args.source}")
    if not dst:
        _err(f"destination contact not found: {args.destination}")
    if args.source == args.destination:
        _err("source and destination are the same")

    conn = db._conn()
    # Move phones (on UNIQUE collision keep the destination's existing row)
    conn.execute(
        "UPDATE OR IGNORE contact_phones SET contact_id = ? WHERE contact_id = ?",
        (args.destination, args.source),
    )
    conn.execute("DELETE FROM contact_phones WHERE contact_id = ?", (args.source,))
    # Move emails
    conn.execute(
        "UPDATE OR IGNORE contact_emails SET contact_id = ? WHERE contact_id = ?",
        (args.destination, args.source),
    )
    conn.execute("DELETE FROM contact_emails WHERE contact_id = ?", (args.source,))
    # Move incidents
    conn.execute(
        "UPDATE incidents SET contact_id = ? WHERE contact_id = ?",
        (args.destination, args.source),
    )
    conn.execute("DELETE FROM contacts WHERE id = ?", (args.source,))
    conn.commit()

    # Log merge as a note on every affected incident? Just log once on the dst.
    # Simpler: return the merged contact.
    _out({
        "merged_into": args.destination,
        "removed": args.source,
        "contact": db.get_contact(args.destination),
    }, pretty=args.pretty)


def cmd_merge_incidents(args):
    """Merge source incident into destination. Calls, emails, and timeline
    entries move to the destination; source is marked closed with a note."""
    db = _db(args)
    src = db.get_incident(args.source)
    dst = db.get_incident(args.destination)
    if not src:
        _err(f"source incident not found: {args.source}")
    if not dst:
        _err(f"destination incident not found: {args.destination}")
    if args.source == args.destination:
        _err("source and destination are the same")

    conn = db._conn()
    # Move calls
    conn.execute(
        "UPDATE calls SET incident_id = ? WHERE incident_id = ?",
        (args.destination, args.source),
    )
    # Move emails
    conn.execute(
        "UPDATE emails SET incident_id = ? WHERE incident_id = ?",
        (args.destination, args.source),
    )
    # Move timeline entries
    conn.execute(
        "UPDATE incident_entries SET incident_id = ? WHERE incident_id = ?",
        (args.destination, args.source),
    )
    conn.commit()

    # Add a merge record
    db.add_incident_entry(
        args.destination, "note", author="cli",
        payload={"text": f"Merged from {args.source}"},
    )
    # Close the source incident with a pointer
    db.update_incident(args.source, status="closed")
    db.add_incident_entry(
        args.source, "note", author="cli",
        payload={"text": f"Merged into {args.destination}"},
    )

    _out({
        "merged_into": args.destination,
        "source_closed": args.source,
        "incident": db.get_incident(args.destination),
    }, pretty=args.pretty)


# --- Add phone/email to existing contact ---


def cmd_add_phone(args):
    db = _db(args)
    c = db.get_contact(args.contact_id)
    if not c:
        _err(f"contact not found: {args.contact_id}")
    e164 = normalize_phone(args.phone) or args.phone
    conn = db._conn()
    # If the phone already belongs to someone, refuse
    row = conn.execute(
        "SELECT contact_id FROM contact_phones WHERE e164 = ?", (e164,)
    ).fetchone()
    if row:
        if row["contact_id"] != args.contact_id:
            _err(f"phone {e164} already belongs to {row['contact_id']} — "
                 f"use merge-contacts to combine", code=1)
        _out(db.get_contact(args.contact_id), pretty=args.pretty)
        return

    conn.execute(
        """INSERT INTO contact_phones (contact_id, e164, created_at)
           VALUES (?, ?, unixepoch('subsec'))""",
        (args.contact_id, e164),
    )
    conn.commit()
    _out(db.get_contact(args.contact_id), pretty=args.pretty)


def cmd_add_email(args):
    db = _db(args)
    c = db.get_contact(args.contact_id)
    if not c:
        _err(f"contact not found: {args.contact_id}")
    addr = args.email.strip().lower()
    conn = db._conn()
    row = conn.execute(
        "SELECT contact_id FROM contact_emails WHERE address = ?", (addr,)
    ).fetchone()
    if row:
        if row["contact_id"] != args.contact_id:
            _err(f"email {addr} already belongs to {row['contact_id']} — "
                 f"use merge-contacts to combine", code=1)
        _out(db.get_contact(args.contact_id), pretty=args.pretty)
        return

    conn.execute(
        """INSERT INTO contact_emails (contact_id, address, created_at)
           VALUES (?, ?, unixepoch('subsec'))""",
        (args.contact_id, addr),
    )
    conn.commit()
    _out(db.get_contact(args.contact_id), pretty=args.pretty)


# --- Search ---


def cmd_search(args):
    """Find contacts and incidents matching a query string.

    Matches partial phone digits, email substrings, contact display_name,
    and incident subject.
    """
    db = _db(args)
    q = args.query.strip()
    if not q:
        _err("empty query")

    q_lower = q.lower()
    q_digits = "".join(c for c in q if c.isdigit())

    conn = db._conn()
    results: dict = {"contacts": [], "incidents": []}

    # Contact phone match
    if q_digits:
        rows = conn.execute(
            """SELECT DISTINCT c.id, c.display_name
               FROM contacts c JOIN contact_phones p ON c.id = p.contact_id
               WHERE p.e164 LIKE ? LIMIT 20""",
            (f"%{q_digits}%",),
        ).fetchall()
        for r in rows:
            results["contacts"].append({"id": r["id"], "display_name": r["display_name"], "match": "phone"})

    # Contact name/email match
    rows = conn.execute(
        """SELECT DISTINCT c.id, c.display_name
           FROM contacts c
           LEFT JOIN contact_emails e ON c.id = e.contact_id
           WHERE LOWER(c.display_name) LIKE ? OR LOWER(e.address) LIKE ?
           LIMIT 20""",
        (f"%{q_lower}%", f"%{q_lower}%"),
    ).fetchall()
    seen = {c["id"] for c in results["contacts"]}
    for r in rows:
        if r["id"] not in seen:
            results["contacts"].append({"id": r["id"], "display_name": r["display_name"], "match": "name_or_email"})
            seen.add(r["id"])

    # Incident subject match
    rows = conn.execute(
        "SELECT id, subject, status FROM incidents WHERE LOWER(subject) LIKE ? LIMIT 20",
        (f"%{q_lower}%",),
    ).fetchall()
    for r in rows:
        results["incidents"].append(dict(r))

    if args.pretty:
        if results["contacts"]:
            print("Contacts:")
            for c in results["contacts"]:
                print(f"  {c['id']}  {c['display_name'] or '(unnamed)'}  ({c['match']})")
        if results["incidents"]:
            print("Incidents:")
            for i in results["incidents"]:
                print(f"  {i['id']}  [{i['status']}]  {i['subject']}")
        if not results["contacts"] and not results["incidents"]:
            print("(no matches)")
    else:
        _out(results)


# --- Outbound call (hits the running Callen's REST endpoint) ---


def cmd_originate(args):
    """Originate an outbound call via the running Callen's REST API.

    The CLI process has no SIP stack, so it delegates to the running
    Callen instance via HTTP. Callen must be running with the web server
    enabled (which is the default).
    """
    import urllib.request
    import urllib.error

    db = _db(args)
    inc = db.get_incident(args.incident_id)
    if not inc:
        _err(f"incident not found: {args.incident_id}")

    # Resolve destination phone from the contact unless explicitly given
    destination = args.destination
    display_name = args.display_name or ""
    if not destination:
        if not inc.get("contact_id"):
            _err("incident has no contact and no --destination given")
        contact = db.get_contact(inc["contact_id"])
        if not contact or not contact.get("phones"):
            _err("contact has no phone number on file")
        destination = contact["phones"][0]["e164"]
        display_name = display_name or contact.get("display_name") or ""

    # Load web config for the endpoint URL
    config = load_config(args.config)
    host = config.web.host if config.web.host != "0.0.0.0" else "127.0.0.1"
    url = f"http://{host}:{config.web.port}/api/call/originate"

    body = json.dumps({
        "incident_id": args.incident_id,
        "destination": destination,
        "display_name": display_name,
    }).encode()

    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        _err(f"could not reach running Callen at {url}: {e}", code=2)

    _out(result, pretty=args.pretty)


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

    # operator status
    pp = sub.add_parser("get-operator-status", help="Current operator status")
    pp.set_defaults(func=cmd_get_operator_status)

    pp = sub.add_parser("set-operator-status", help="Set operator status")
    pp.add_argument("status", choices=["available", "busy", "dnd"])
    pp.set_defaults(func=cmd_set_operator_status)

    # merges
    pp = sub.add_parser("merge-contacts", help="Merge one contact into another")
    pp.add_argument("source", help="source contact id (will be removed)")
    pp.add_argument("destination", help="destination contact id (kept)")
    pp.set_defaults(func=cmd_merge_contacts)

    pp = sub.add_parser("merge-incidents", help="Merge one incident into another")
    pp.add_argument("source", help="source incident id (will be closed)")
    pp.add_argument("destination", help="destination incident id (kept)")
    pp.set_defaults(func=cmd_merge_incidents)

    # add phone/email to contact
    pp = sub.add_parser("add-phone", help="Attach a phone number to an existing contact")
    pp.add_argument("contact_id")
    pp.add_argument("phone")
    pp.set_defaults(func=cmd_add_phone)

    pp = sub.add_parser("add-email", help="Attach an email to an existing contact")
    pp.add_argument("contact_id")
    pp.add_argument("email")
    pp.set_defaults(func=cmd_add_email)

    # search
    pp = sub.add_parser("search", help="Fuzzy search contacts and incidents")
    pp.add_argument("query", help="partial name, phone digits, email, or subject")
    pp.set_defaults(func=cmd_search)

    # originate
    pp = sub.add_parser(
        "originate",
        help="Originate an outbound call: rings operator first, then bridges to contact",
    )
    pp.add_argument("incident_id", help="incident to attach the outbound call to")
    pp.add_argument(
        "--destination",
        help="destination phone (defaults to the contact's first phone on file)",
    )
    pp.add_argument(
        "--display-name", dest="display_name",
        help="name to announce to the operator (defaults to contact name)",
    )
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
