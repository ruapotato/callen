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
import logging
import shutil
import sys
import time
from pathlib import Path

log = logging.getLogger("callen.cli")

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


MAX_BODY = 8000  # chars — enough for any real email, caps runaway HTML


def _truncate_email_bodies(em: dict):
    """Keep email output manageable for the agent. If body_text has
    content, truncate body_html. If body_text is empty, fall back to
    body_html but cap it. Cap body_text too if it's huge."""
    text = em.get("body_text") or ""
    html = em.get("body_html") or ""

    if text:
        # We have clean text — truncate the HTML duplicate
        if len(html) > 2000:
            em["body_html"] = f"(truncated — {len(html)} bytes; use body_text)"
        if len(text) > MAX_BODY:
            em["body_text"] = text[:MAX_BODY] + f"\n... (truncated at {MAX_BODY} chars of {len(text)})"
    elif html:
        # No text version — keep HTML but cap it
        if len(html) > MAX_BODY:
            em["body_html"] = html[:MAX_BODY] + f"\n... (truncated at {MAX_BODY} chars of {len(html)})"
    # else: both empty, nothing to do


def _db(args) -> Database:
    config = load_config(args.config)
    db = Database(config.general.db_path)
    db.initialize()
    return db


def _db_and_config(args):
    config = load_config(args.config)
    db = Database(config.general.db_path)
    db.initialize()
    return db, config


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
    inc["emails"] = db.list_emails_for_incident(args.incident_id)
    inc["todos"] = db.list_todos(args.incident_id)
    if inc.get("contact_id"):
        inc["contact"] = db.get_contact(inc["contact_id"])
    for em in inc.get("emails", []):
        _truncate_email_bodies(em)
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


def cmd_delete_incident(args):
    db = _db(args)
    ok = db.delete_incident(args.incident_id)
    if not ok:
        _err(f"incident {args.incident_id} not found")
    _out({"deleted": args.incident_id}, pretty=args.pretty)


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
    privacy = None
    if hasattr(args, 'privacy') and args.privacy is not None:
        privacy = args.privacy.lower() in ('true', '1', 'on', 'yes')
    db.update_contact(
        args.contact_id,
        display_name=args.name,
        notes=args.notes,
        privacy_mode=privacy,
        nickname=getattr(args, 'nickname', None),
    )
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


# --- Remove / rename phone or email ---


def cmd_remove_phone(args):
    db = _db(args)
    from callen.storage.db import normalize_phone
    e164 = normalize_phone(args.phone) or args.phone
    if not db.remove_contact_phone(args.contact_id, e164):
        _err(f"{e164} not attached to {args.contact_id}")
    _out(db.get_contact(args.contact_id), pretty=args.pretty)


def cmd_remove_email(args):
    db = _db(args)
    if not db.remove_contact_email(args.contact_id, args.email):
        _err(f"{args.email} not attached to {args.contact_id}")
    _out(db.get_contact(args.contact_id), pretty=args.pretty)


def cmd_rename_phone(args):
    db = _db(args)
    from callen.storage.db import normalize_phone
    new_e164 = normalize_phone(args.new_phone) or args.new_phone
    if not db.rename_contact_phone(args.contact_id, args.old_phone, new_e164):
        _err(f"could not rename {args.old_phone} -> {new_e164} on {args.contact_id} (not found or conflict)")
    _out(db.get_contact(args.contact_id), pretty=args.pretty)


def cmd_rename_email(args):
    db = _db(args)
    if not db.rename_contact_email(args.contact_id, args.old_email, args.new_email):
        _err(f"could not rename {args.old_email} -> {args.new_email} on {args.contact_id}")
    _out(db.get_contact(args.contact_id), pretty=args.pretty)


def cmd_delete_contact(args):
    db = _db(args)
    result = db.delete_contact(args.contact_id, cascade=args.cascade)
    if result.get("error") == "not found":
        _err(f"contact {args.contact_id} not found")
    if result.get("error") == "contact has incidents":
        _err(
            f"contact has {len(result['incidents'])} incident(s); "
            f"re-run with --cascade to delete them too, or reassign them first",
            code=1,
        )
    _out(result, pretty=args.pretty)


def cmd_reassign_incident(args):
    db = _db(args)
    if not db.reassign_incident(args.incident_id, args.contact):
        _err(f"could not reassign {args.incident_id} -> {args.contact} (not found)")
    _out(db.get_incident(args.incident_id), pretty=args.pretty)


# --- Sites (GitHub Pages + Cloudflare) ---


def _site_manager(args):
    from callen.config import load_config
    from callen.sites.manager import SiteManager
    config = load_config(args.config)
    return SiteManager(config.sites)


def _site_db_and_manager(args):
    from callen.sites.manager import SiteManager
    db, config = _db_and_config(args)
    return db, SiteManager(config.sites)


def _require_site_ownership(db, contact_id: str, subdomain: str):
    if not db.verify_site_ownership(contact_id, subdomain):
        _err(f"contact {contact_id} does not own site {subdomain}")


def cmd_site_create(args):
    db, mgr = _site_db_and_manager(args)
    result = mgr.create_site(args.subdomain, template=args.template)
    # Persist ownership in the DB
    if args.contact:
        try:
            db.create_managed_site(
                args.subdomain, args.contact,
                repo_url=result.get("repo", ""),
                fqdn=result.get("fqdn", ""),
            )
            result["contact_id"] = args.contact
        except Exception as e:
            result["db_error"] = str(e)
    _out(result, pretty=args.pretty)


def cmd_site_delete(args):
    db, mgr = _site_db_and_manager(args)
    if args.contact:
        _require_site_ownership(db, args.contact, args.subdomain)
    result = mgr.delete_site(args.subdomain)
    db.delete_managed_site(args.subdomain)
    _out(result, pretty=args.pretty)


def cmd_site_list(args):
    db, mgr = _site_db_and_manager(args)
    if args.contact:
        sites = db.get_sites_by_contact(args.contact)
    else:
        sites = db.list_managed_sites(limit=200)
    _out(sites, pretty=args.pretty)


def cmd_site_edit(args):
    db, mgr = _site_db_and_manager(args)
    if args.contact:
        _require_site_ownership(db, args.contact, args.subdomain)
    repo_full = f"{mgr.github_org}/{args.subdomain}"
    if not mgr.repo_exists(args.subdomain):
        _err(f"site {args.subdomain} not found")

    content = args.content
    if content == "-":
        import sys as _sys
        content = _sys.stdin.read()

    mgr._upsert_file(repo_full, args.file, content, args.message or f"Update {args.file}")
    _out({
        "site": args.subdomain,
        "file": args.file,
        "message": args.message or f"Update {args.file}",
        "status": "pushed",
    }, pretty=args.pretty)


def cmd_site_upload_video(args):
    db, mgr = _site_db_and_manager(args)
    _require_site_ownership(db, args.contact, args.subdomain)
    from callen.sites.video import process_and_upload_video
    result = process_and_upload_video(
        video_path=args.video,
        site_subdomain=args.subdomain,
        manager=mgr,
        dest_path=args.dest,
        max_height=args.max_height,
        crf=args.crf,
        strip_audio=args.strip_audio,
        max_duration=args.max_duration,
        commit_message=args.message or f"Upload video {args.video}",
    )
    _out(result, pretty=args.pretty)


def cmd_site_upload_image(args):
    db, mgr = _site_db_and_manager(args)
    _require_site_ownership(db, args.contact, args.subdomain)
    from callen.sites.image import process_and_upload_image
    result = process_and_upload_image(
        image_path=args.image,
        site_subdomain=args.subdomain,
        manager=mgr,
        dest_path=args.dest,
        max_width=args.max_width,
        commit_message=args.message or f"Upload image {args.image}",
    )
    _out(result, pretty=args.pretty)


def cmd_site_get(args):
    db, mgr = _site_db_and_manager(args)
    site = db.get_site_by_subdomain(args.subdomain)
    if not site:
        exists = mgr.repo_exists(args.subdomain)
        site = {
            "subdomain": args.subdomain,
            "fqdn": f"{args.subdomain}.{mgr.domain}",
            "url": f"https://{args.subdomain}.{mgr.domain}",
            "repo_exists": exists,
            "tracked": False,
        }
    else:
        site["url"] = f"https://{site['fqdn']}"
        site["tracked"] = True
    _out(site, pretty=args.pretty)


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


# --- Bad actor quarantine ---


def cmd_block_sender(args):
    """Quarantine a sender so future inbound email/calls are rejected
    at the front door — no OCR, no preflight, no agent exposure."""
    db, config = _db_and_config(args)
    reason = args.reason or "manual"
    blocked_any = False
    result: dict = {"blocked": [], "reason": reason}

    if args.email:
        ok = db.block_email(args.email, reason=reason)
        if ok:
            result["blocked"].append({"type": "email", "address": args.email})
            blocked_any = True
            # Also flip the owning contact to 'suspect' and send the
            # lockout notice so the sender learns they're blocked and
            # how to reach a human to appeal.
            try:
                contact_id = db.upsert_contact_by_email(args.email)
                if contact_id:
                    db.set_contact_trust(contact_id, "suspect")
            except Exception:
                log.exception("Failed to flip contact suspect for %s", args.email)
            try:
                from callen.notify.email import send_lockout_notice
                mid = send_lockout_notice(
                    config.email, args.email,
                    config.operator.support_phone,
                )
                if mid:
                    result.setdefault("notices", []).append(
                        {"to": args.email, "message_id": mid}
                    )
            except Exception:
                log.exception("Failed to send lockout notice to %s", args.email)
    if args.phone:
        from callen.storage.db import normalize_phone
        e164 = normalize_phone(args.phone) or args.phone
        ok = db.block_phone(e164, reason=reason)
        if ok:
            result["blocked"].append({"type": "phone", "e164": e164})
            blocked_any = True

    if not blocked_any:
        _err("no records matched. Use create-contact / add-email / add-phone first, or check --email / --phone arguments.")
    _out(result, pretty=args.pretty)


def cmd_unblock_sender(args):
    db = _db(args)
    result: dict = {"unblocked": []}
    if args.email:
        if db.unblock_email(args.email):
            result["unblocked"].append({"type": "email", "address": args.email})
    if args.phone:
        from callen.storage.db import normalize_phone
        e164 = normalize_phone(args.phone) or args.phone
        if db.unblock_phone(e164):
            result["unblocked"].append({"type": "phone", "e164": e164})
    _out(result, pretty=args.pretty)


def cmd_list_blocked(args):
    db = _db(args)
    result = db.list_blocked()
    if args.pretty:
        if not result["emails"] and not result["phones"]:
            print("(no blocked senders)")
            return
        if result["emails"]:
            print("Blocked emails:")
            for e in result["emails"]:
                when = time.strftime('%Y-%m-%d %H:%M',
                                     time.localtime(e['blocked_at']))
                print(f"  {e['address']:<40s}  {when}  {e.get('blocked_reason','')}")
        if result["phones"]:
            print("Blocked phones:")
            for p in result["phones"]:
                when = time.strftime('%Y-%m-%d %H:%M',
                                     time.localtime(p['blocked_at']))
                print(f"  {p['e164']:<20s}  {when}  {p.get('blocked_reason','')}")
        return
    _out(result)


# --- Todos (actionable checklist per incident) ---


def cmd_list_todos(args):
    db = _db(args)
    rows = db.list_todos(args.incident_id)
    if args.pretty:
        if not rows:
            print("(no todos)")
            return
        for r in rows:
            mark = "[x]" if r["done"] else "[ ]"
            print(f"  {r['id']:4d} {mark} {r['text']}")
        return
    _out(rows)


def cmd_add_todo(args):
    db = _db(args)
    if not db.get_incident(args.incident_id):
        _err(f"incident not found: {args.incident_id}")
    text = args.text
    if text == "-":
        text = sys.stdin.read().strip()
    if not text:
        _err("empty todo text")
    tid = db.add_todo(args.incident_id, text, author=args.author)
    db.add_incident_entry(
        args.incident_id, "todo_added", author=args.author,
        payload={"todo_id": tid, "text": text},
    )
    _out({"todo_id": tid, "incident_id": args.incident_id, "text": text})


def cmd_complete_todo(args):
    db = _db(args)
    todo = db.get_todo(args.todo_id)
    if not todo:
        _err(f"todo not found: {args.todo_id}")
    db.update_todo(args.todo_id, done=True)
    db.add_incident_entry(
        todo["incident_id"], "todo_done", author=args.author,
        payload={"todo_id": args.todo_id, "text": todo["text"]},
    )
    _out(db.get_todo(args.todo_id))


def cmd_uncomplete_todo(args):
    db = _db(args)
    todo = db.get_todo(args.todo_id)
    if not todo:
        _err(f"todo not found: {args.todo_id}")
    db.update_todo(args.todo_id, done=False)
    _out(db.get_todo(args.todo_id))


def cmd_update_todo(args):
    db = _db(args)
    todo = db.get_todo(args.todo_id)
    if not todo:
        _err(f"todo not found: {args.todo_id}")
    db.update_todo(args.todo_id, text=args.text)
    _out(db.get_todo(args.todo_id))


def cmd_delete_todo(args):
    db = _db(args)
    todo = db.get_todo(args.todo_id)
    if not todo:
        _err(f"todo not found: {args.todo_id}")
    db.delete_todo(args.todo_id)
    _out({"deleted": args.todo_id, "incident_id": todo["incident_id"]})


# --- Emails (triage queue + agent-sent replies) ---


def _render_email_list(rows: list[dict], pretty: bool, empty_label: str = "(empty)"):
    if pretty:
        if not rows:
            print(empty_label)
            return
        for r in rows:
            when = time.strftime('%Y-%m-%d %H:%M', time.localtime(r['received_at']))
            subj = r['subject'][:55] if r['subject'] else '(no subject)'
            reason = f"  [{r['status_reason']}]" if r.get('status_reason') else ''
            print(f"  {r['id']:4d}  {when}  {r['from_addr']:<30s}  {subj}{reason}")
        return
    _out(rows)


def cmd_list_pending_emails(args):
    db = _db(args)
    rows = db.list_pending_emails(limit=args.limit)
    _render_email_list(rows, args.pretty, "(no pending emails)")


def cmd_list_flagged_emails(args):
    db = _db(args)
    rows = db.list_emails_by_status("flagged", limit=args.limit)
    _render_email_list(rows, args.pretty, "(no flagged emails)")


def cmd_list_rejected_emails(args):
    db = _db(args)
    rows = db.list_emails_by_status("rejected", limit=args.limit)
    _render_email_list(rows, args.pretty, "(no rejected emails)")


def cmd_mark_safe(args):
    """Move a flagged or rejected email back to the pending queue."""
    db = _db(args)
    em = db.get_email(args.email_id)
    if not em:
        _err(f"email not found: {args.email_id}")
    if em.get("incident_id"):
        _err(f"email is already attached to {em['incident_id']}")
    ok = db.set_email_status(args.email_id, "pending", "marked_safe_by_agent")
    if not ok:
        _err("failed to update email status")
    _out({
        "email_id": args.email_id,
        "status": "pending",
        "previous_status": em.get("status"),
        "previous_reason": em.get("status_reason"),
    })


def cmd_get_email(args):
    db = _db(args)
    em = db.get_email(args.email_id)
    if not em:
        _err(f"email not found: {args.email_id}")
    em["attachments"] = db.list_email_attachments(args.email_id)
    # Keep output manageable for the agent. If body_text has content,
    # truncate body_html (it's usually a styled duplicate). If body_text
    # is empty, fall back to body_html but cap it. Cap body_text too if
    # it's absurdly long.
    _truncate_email_bodies(em)
    _out(em, pretty=args.pretty)


def cmd_get_attachment(args):
    db = _db(args)
    att = db.get_email_attachment(args.attachment_id)
    if not att:
        _err(f"attachment not found: {args.attachment_id}")

    if args.out:
        import shutil
        from pathlib import Path as _P
        src = _P(att["file_path"])
        if not src.exists():
            _err(f"attachment file missing on disk: {src}")
        dst = _P(args.out)
        shutil.copy2(src, dst)
        _out({"copied_to": str(dst), "size_bytes": dst.stat().st_size})
    elif args.text:
        print(att.get("extracted_text") or "")
    else:
        _out(att, pretty=args.pretty)


def cmd_assign_email(args):
    """Route a pending email to an incident (existing or new).

    If --incident is given, attach to that incident.
    If --create-incident is given, create a new incident from the email's
    sender and subject, optionally overriding --subject / --priority.
    """
    db = _db(args)
    em = db.get_email(args.email_id)
    if not em:
        _err(f"email not found: {args.email_id}")
    if em.get("incident_id"):
        _err(f"email already attached to {em['incident_id']}")

    incident_id = args.incident
    if args.create_incident:
        # Look up contact by the email's sender address
        from_addr = (em["from_addr"] or "").strip().lower()
        if not from_addr:
            _err("email has no from address")
        contact_id = db.upsert_contact_by_email(from_addr)
        subject = args.subject or em.get("subject") or f"Email from {from_addr}"
        incident_id = db.create_incident(
            contact_id=contact_id,
            subject=subject,
            channel="email",
            status=args.status or "open",
            priority=args.priority or "normal",
        )
        log.info("Created %s from email %s", incident_id, args.email_id)

    if not incident_id:
        _err("must pass --incident or --create-incident")

    # Validate incident exists
    if not db.get_incident(incident_id):
        _err(f"incident not found: {incident_id}")

    if not db.attach_email_to_incident(args.email_id, incident_id):
        _err("failed to attach email to incident")

    # Log on the timeline
    db.add_incident_entry(
        incident_id, "email",
        author=em.get("from_addr") or "unknown",
        linked_email_id=args.email_id,
        payload={
            "direction": "in",
            "from": em.get("from_addr"),
            "subject": em.get("subject"),
            "preview": (em.get("body_text") or "")[:300],
            "routed_by": "agent",
        },
    )

    _out({
        "email_id": args.email_id,
        "incident_id": incident_id,
        "status": "attached",
    }, pretty=args.pretty)


def cmd_reject_email(args):
    """Soft-reject a pending or flagged email.

    By default the email row is KEPT in the database with status='rejected'
    so you have an audit trail. Pass --hard-delete to actually remove the
    row (use sparingly — rejection history is useful for debugging the
    auto-filters).
    """
    db = _db(args)
    em = db.get_email(args.email_id)
    if not em:
        _err(f"email not found: {args.email_id}")
    if em.get("incident_id"):
        _err(f"email is already attached to {em['incident_id']} — cannot reject")

    if args.hard_delete:
        ok = db.delete_email(args.email_id)
        if not ok:
            _err("failed to delete email")
        _out({
            "email_id": args.email_id,
            "status": "deleted",
            "reason": args.reason or "",
        })
        return

    reason = args.reason or "manual_reject"
    ok = db.set_email_status(args.email_id, "rejected", reason)
    if not ok:
        _err("failed to reject email")
    _out({
        "email_id": args.email_id,
        "status": "rejected",
        "reason": reason,
    })


def cmd_send_email(args):
    """Send an outbound email on an incident.

    The email is threaded as a reply to the most recent inbound message on
    the incident (if any). Stored in the emails table with direction=out and
    logged on the incident timeline.
    """
    db = _db(args)
    inc = db.get_incident(args.incident_id)
    if not inc:
        _err(f"incident not found: {args.incident_id}")

    # Resolve destination from the contact if not given
    to = args.to
    if not to:
        if not inc.get("contact_id"):
            _err("incident has no contact and no --to given")
        contact = db.get_contact(inc["contact_id"])
        if not contact or not contact.get("emails"):
            _err("contact has no email address on file")
        to = contact["emails"][0]["address"]

    # Find the most recent inbound email on this incident to thread against
    existing = db.list_emails_for_incident(args.incident_id)
    in_reply_to = None
    references = None
    for e in reversed(existing):
        if e.get("direction") == "in" and e.get("message_id"):
            in_reply_to = e["message_id"]
            references = e.get("in_reply_to") or in_reply_to
            break

    body = args.body
    if body == "-":
        body = sys.stdin.read()
    if not body:
        _err("empty body")

    # Subject — use incident's subject prefixed with Re: + [INC-NNNN]
    subject = args.subject or inc.get("subject", "") or "Support"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    if args.incident_id not in subject:
        subject = f"{subject} [{args.incident_id}]"

    config = load_config(args.config)
    if not config.email.enabled:
        _err("email is not enabled in config.toml")

    from callen.notify.email import send_mail
    from callen.storage.models import EmailMessage as EmailRec

    try:
        msg_id = send_mail(
            config.email,
            to=to,
            subject=subject,
            body_text=body,
            in_reply_to=in_reply_to,
            references=references,
        )
    except Exception as e:
        _err(f"send failed: {e}", code=2)

    record = EmailRec(
        message_id=msg_id,
        incident_id=args.incident_id,
        direction="out",
        from_addr=config.email.from_address,
        to_addr=to,
        subject=subject,
        body_text=body,
        received_at=time.time(),
        in_reply_to=in_reply_to or "",
    )
    email_id = db.save_email(record)

    db.add_incident_entry(
        args.incident_id, "email",
        author=args.author or "agent",
        linked_email_id=email_id,
        payload={
            "direction": "out",
            "to": to,
            "subject": subject,
            "preview": body[:300],
        },
    )

    _out({
        "email_id": email_id,
        "message_id": msg_id,
        "to": to,
        "subject": subject,
        "status": "sent",
    }, pretty=args.pretty)


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

    pp = sub.add_parser("delete-incident", help="Hard-delete an incident (calls/emails detached, not deleted)")
    pp.add_argument("incident_id")
    pp.set_defaults(func=cmd_delete_incident)

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
    pp.add_argument("--privacy", help="privacy mode: true/false (hides real name/phone in UI)")
    pp.add_argument("--nickname", help="display alias when privacy mode is on")
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

    pp = sub.add_parser("remove-phone", help="Detach a phone number from a contact")
    pp.add_argument("contact_id")
    pp.add_argument("phone")
    pp.set_defaults(func=cmd_remove_phone)

    pp = sub.add_parser("remove-email", help="Detach an email address from a contact")
    pp.add_argument("contact_id")
    pp.add_argument("email")
    pp.set_defaults(func=cmd_remove_email)

    pp = sub.add_parser("rename-phone", help="Rewrite a phone entry on a contact (e.g. fix a placeholder)")
    pp.add_argument("contact_id")
    pp.add_argument("old_phone", help="current value to replace")
    pp.add_argument("new_phone", help="new value")
    pp.set_defaults(func=cmd_rename_phone)

    pp = sub.add_parser("rename-email", help="Rewrite an email entry on a contact")
    pp.add_argument("contact_id")
    pp.add_argument("old_email")
    pp.add_argument("new_email")
    pp.set_defaults(func=cmd_rename_email)

    pp = sub.add_parser("delete-contact", help="Delete a contact; use --cascade to also delete all its incidents")
    pp.add_argument("contact_id")
    pp.add_argument("--cascade", action="store_true", help="also delete every incident attached to this contact")
    pp.set_defaults(func=cmd_delete_contact)

    pp = sub.add_parser("reassign-incident", help="Move an incident to a different contact")
    pp.add_argument("incident_id")
    pp.add_argument("contact", help="destination contact id")
    pp.set_defaults(func=cmd_reassign_incident)

    # search
    pp = sub.add_parser("search", help="Fuzzy search contacts and incidents")
    pp.add_argument("query", help="partial name, phone digits, email, or subject")
    pp.set_defaults(func=cmd_search)

    # bad actor quarantine
    pp = sub.add_parser("block-sender", help="Quarantine an email / phone so the pipeline rejects it at the door")
    pp.add_argument("--email", help="email address to block")
    pp.add_argument("--phone", help="phone number to block")
    pp.add_argument("--reason", help="why this sender is being blocked")
    pp.set_defaults(func=cmd_block_sender)

    pp = sub.add_parser("unblock-sender", help="Remove an email / phone from the block list")
    pp.add_argument("--email")
    pp.add_argument("--phone")
    pp.set_defaults(func=cmd_unblock_sender)

    pp = sub.add_parser("list-blocked", help="List all blocked senders")
    pp.set_defaults(func=cmd_list_blocked)

    # todos
    pp = sub.add_parser("list-todos", help="Show the todo checklist for an incident")
    pp.add_argument("incident_id")
    pp.set_defaults(func=cmd_list_todos)

    pp = sub.add_parser("add-todo", help="Add a todo item to an incident")
    pp.add_argument("incident_id")
    pp.add_argument("text", help="todo text (use - for stdin)")
    pp.add_argument("--author", default="agent")
    pp.set_defaults(func=cmd_add_todo)

    pp = sub.add_parser("complete-todo", help="Mark a todo as done")
    pp.add_argument("todo_id", type=int)
    pp.add_argument("--author", default="operator")
    pp.set_defaults(func=cmd_complete_todo)

    pp = sub.add_parser("uncomplete-todo", help="Mark a done todo as not done")
    pp.add_argument("todo_id", type=int)
    pp.set_defaults(func=cmd_uncomplete_todo)

    pp = sub.add_parser("update-todo", help="Edit a todo's text")
    pp.add_argument("todo_id", type=int)
    pp.add_argument("text")
    pp.set_defaults(func=cmd_update_todo)

    pp = sub.add_parser("delete-todo", help="Delete a todo")
    pp.add_argument("todo_id", type=int)
    pp.set_defaults(func=cmd_delete_todo)

    # emails — triage queue + outbound
    pp = sub.add_parser("list-pending-emails", help="Inbound emails not yet routed to an incident")
    pp.add_argument("--limit", type=int, default=100)
    pp.set_defaults(func=cmd_list_pending_emails)

    pp = sub.add_parser("get-email", help="Fetch one email (inbound or outbound)")
    pp.add_argument("email_id", type=int)
    pp.set_defaults(func=cmd_get_email)

    pp = sub.add_parser("get-attachment", help="Fetch / download an email attachment")
    pp.add_argument("attachment_id", type=int)
    pp.add_argument("--out", help="copy the raw file to this path")
    pp.add_argument("--text", action="store_true", help="print just the extracted text")
    pp.set_defaults(func=cmd_get_attachment)

    pp = sub.add_parser("assign-email", help="Route a pending email to an incident")
    pp.add_argument("email_id", type=int)
    g = pp.add_mutually_exclusive_group(required=True)
    g.add_argument("--incident", help="attach to an existing incident")
    g.add_argument("--create-incident", action="store_true", help="create a new incident from the email")
    pp.add_argument("--subject", help="override subject when creating a new incident")
    pp.add_argument("--priority", choices=["low", "normal", "high", "urgent"])
    pp.add_argument("--status", choices=["open", "in_progress", "waiting", "resolved", "closed"])
    pp.set_defaults(func=cmd_assign_email)

    pp = sub.add_parser("reject-email", help="Mark an email as rejected (soft-delete, kept for audit)")
    pp.add_argument("email_id", type=int)
    pp.add_argument("--reason", help="why it was rejected")
    pp.add_argument("--hard-delete", action="store_true", help="actually remove the row instead of marking")
    pp.set_defaults(func=cmd_reject_email)

    pp = sub.add_parser("list-flagged-emails", help="Emails flagged for security review (e.g. possible prompt injection)")
    pp.add_argument("--limit", type=int, default=100)
    pp.set_defaults(func=cmd_list_flagged_emails)

    pp = sub.add_parser("list-rejected-emails", help="Emails previously rejected (auto_bulk or manual)")
    pp.add_argument("--limit", type=int, default=100)
    pp.set_defaults(func=cmd_list_rejected_emails)

    pp = sub.add_parser("mark-safe", help="Move a flagged or rejected email back to the pending queue")
    pp.add_argument("email_id", type=int)
    pp.set_defaults(func=cmd_mark_safe)

    pp = sub.add_parser("send-email", help="Send an outbound email on an incident")
    pp.add_argument("incident_id")
    pp.add_argument("--to", help="override recipient (defaults to contact's first email)")
    pp.add_argument("--subject", help="override subject (defaults to incident subject with Re: prefix)")
    pp.add_argument("--body", required=True, help="message body (or - to read from stdin)")
    pp.add_argument("--author", default="agent")
    pp.set_defaults(func=cmd_send_email)

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

    # --- Sites (GitHub Pages + Cloudflare) ---
    pp = sub.add_parser("site-create", help="Create a new managed website (repo + DNS + Pages)")
    pp.add_argument("subdomain", help="subdomain name (e.g. 'laura' for laura.freesoft.page)")
    pp.add_argument("--contact", help="contact ID who will own this site")
    pp.add_argument("--template", help="GitHub template repo (default: config)")
    pp.set_defaults(func=cmd_site_create)

    pp = sub.add_parser("site-edit", help="Push a file to a managed site's repo")
    pp.add_argument("subdomain")
    pp.add_argument("file", help="path in repo (e.g. index.html, css/style.css)")
    pp.add_argument("content", help="file content (use - to read from stdin)")
    pp.add_argument("--contact", help="contact ID for ownership check")
    pp.add_argument("--message", "-m", help="commit message")
    pp.set_defaults(func=cmd_site_edit)

    pp = sub.add_parser("site-delete", help="Tear down a managed website (repo + DNS)")
    pp.add_argument("subdomain")
    pp.add_argument("--contact", help="contact ID for ownership check")
    pp.set_defaults(func=cmd_site_delete)

    pp = sub.add_parser("site-list", help="List all managed websites")
    pp.add_argument("--contact", help="filter to this contact's sites only")
    pp.set_defaults(func=cmd_site_list)

    pp = sub.add_parser("site-get", help="Show details for a managed website")
    pp.add_argument("subdomain")
    pp.set_defaults(func=cmd_site_get)

    pp = sub.add_parser("site-upload-video", help="Transcode and upload a video to a managed site (H.264, max 720p)")
    pp.add_argument("subdomain")
    pp.add_argument("video", help="local path to video file")
    pp.add_argument("--contact", required=True, help="contact ID for ownership check")
    pp.add_argument("--dest", help="destination path in repo (default: videos/<filename>.mp4)")
    pp.add_argument("--message", "-m", help="commit message")
    pp.add_argument("--max-height", type=int, default=720, help="max vertical resolution (default: 720)")
    pp.add_argument("--crf", type=int, default=28, help="quality (18=high, 28=default, 35=small)")
    pp.add_argument("--strip-audio", action="store_true", help="remove audio track")
    pp.add_argument("--max-duration", type=int, default=120, help="max seconds (default: 120)")
    pp.set_defaults(func=cmd_site_upload_video)

    pp = sub.add_parser("site-upload-image", help="Process and upload an image to a managed site")
    pp.add_argument("subdomain")
    pp.add_argument("image", help="local path to image file")
    pp.add_argument("--contact", required=True, help="contact ID for ownership check")
    pp.add_argument("--dest", help="destination path in repo (default: images/<filename>.webp)")
    pp.add_argument("--message", "-m", help="commit message")
    pp.add_argument("--max-width", type=int, default=1200)
    pp.set_defaults(func=cmd_site_upload_image)

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
