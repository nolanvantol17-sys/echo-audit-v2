"""
location_notes_routes.py — Per-location free-form notes ("post-it notes").

Surfaces on the Grade page so the next caller picking a location sees notes
left by previous callers ("phone line doesn't work", etc).

Routes:
    GET    /api/locations/<id>/notes  — list active notes (any user with company access)
    POST   /api/locations/<id>/notes  — create note (any user with company access)
    PUT    /api/location-notes/<id>   — edit text (author OR admin/super_admin)
    DELETE /api/location-notes/<id>   — soft-delete (author OR admin/super_admin)

Tenant scope: location_notes.location_id → locations.company_id. Every endpoint
verifies the location belongs to the current effective company; cross-tenant
attempts return 404 (not 403) to avoid existence info-leak.

Audit logging: skipped intentionally. Notes are user-generated content; the
ln_author_user_id + ln_created_at + ln_updated_at + ln_deleted_at columns
themselves are the audit trail. (Per design discussion 2026-04-24.)
"""

import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from db import IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id

logger = logging.getLogger(__name__)

location_notes_bp = Blueprint("location_notes", __name__, url_prefix="/api")


# ── Local helpers (mirrors the per-blueprint inline-helper pattern) ──

def _err(msg, code):
    return jsonify({"error": msg}), code


def _body():
    return request.get_json(silent=True) or {}


def _require_company():
    cid = get_effective_company_id()
    if cid is None:
        return None, _err(
            "No company context. Super admins must select an organization first.",
            400,
        )
    return cid, None


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _rows(cur):
    return [_row_to_dict(r) for r in cur.fetchall()]


def _validate_text(text):
    """Returns error message string if invalid, None if OK."""
    if not text:
        return "Note text is required"
    if len(text) > 500:
        return "Note text exceeds 500 character limit"
    return None


def _build_author_display(first, last):
    """First name + last initial: 'Nolan V.' Returns 'Unknown user' if both blank
    (e.g. when the author user was deleted, ln_author_user_id is NULL via SET NULL)."""
    first = (first or "").strip()
    last = (last or "").strip()
    if not first and not last:
        return "Unknown user"
    if first and last:
        return f"{first} {last[0]}."
    return first or last


def _decorate(row):
    """Mutate-in-place: pop the joined user_first_name / user_last_name and
    add author_display, is_author, is_edited."""
    fn = row.pop("user_first_name", None) or ""
    ln = row.pop("user_last_name",  None) or ""
    row["author_display"] = _build_author_display(fn, ln)
    row["is_author"]      = (row.get("ln_author_user_id") == current_user.user_id)
    row["is_edited"]      = (row["ln_updated_at"] != row["ln_created_at"])
    return row


def _location_in_company(conn, location_id, company_id):
    """True iff the location exists, isn't soft-deleted, and belongs to company."""
    cur = conn.execute(
        q("""SELECT 1 FROM locations
             WHERE location_id = ? AND company_id = ?
               AND location_deleted_at IS NULL"""),
        (location_id, company_id),
    )
    return cur.fetchone() is not None


def _get_note_for_modify(conn, note_id, company_id):
    """Fetch a non-deleted note + verify its location belongs to company.
    Returns the row dict or None (treat None as 404 to avoid info-leak)."""
    cur = conn.execute(
        q("""SELECT ln.location_note_id, ln.location_id, ln.ln_author_user_id,
                    ln.ln_text, ln.ln_deleted_at,
                    l.company_id AS loc_company_id
               FROM location_notes ln
               JOIN locations l ON l.location_id = ln.location_id
              WHERE ln.location_note_id = ?
                AND ln.ln_deleted_at IS NULL
                AND l.location_deleted_at IS NULL"""),
        (note_id,),
    )
    row = _row_to_dict(cur.fetchone())
    if not row or row["loc_company_id"] != company_id:
        return None
    return row


def _user_can_modify(note):
    """Author OR admin/super_admin only."""
    if note["ln_author_user_id"] == current_user.user_id:
        return True
    return current_user.role in ("admin", "super_admin")


def _fetch_decorated(conn, note_id):
    """Re-fetch a single note + author for response shape consistency."""
    cur = conn.execute(
        q("""SELECT ln.location_note_id, ln.location_id, ln.ln_author_user_id,
                    ln.ln_text, ln.ln_created_at, ln.ln_updated_at,
                    u.user_first_name, u.user_last_name
               FROM location_notes ln
               LEFT JOIN users u ON u.user_id = ln.ln_author_user_id
              WHERE ln.location_note_id = ?"""),
        (note_id,),
    )
    return _decorate(_row_to_dict(cur.fetchone()))


# ── GET /api/locations/<id>/notes ──

@location_notes_bp.route("/locations/<int:location_id>/notes", methods=["GET"])
@login_required
def list_location_notes(location_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        if not _location_in_company(conn, location_id, company_id):
            return _err("Location not found", 404)

        cur = conn.execute(
            q("""SELECT ln.location_note_id, ln.location_id, ln.ln_author_user_id,
                        ln.ln_text, ln.ln_created_at, ln.ln_updated_at,
                        u.user_first_name, u.user_last_name
                   FROM location_notes ln
                   LEFT JOIN users u ON u.user_id = ln.ln_author_user_id
                  WHERE ln.location_id = ?
                    AND ln.ln_deleted_at IS NULL
                  ORDER BY ln.ln_created_at DESC, ln.location_note_id DESC"""),
            (location_id,),
        )
        return jsonify([_decorate(r) for r in _rows(cur)])
    finally:
        conn.close()


# ── POST /api/locations/<id>/notes ──

@location_notes_bp.route("/locations/<int:location_id>/notes", methods=["POST"])
@login_required
def create_location_note(location_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    text = (body.get("ln_text") or "").strip()
    verr = _validate_text(text)
    if verr:
        return _err(verr, 400)

    conn = get_conn()
    try:
        if not _location_in_company(conn, location_id, company_id):
            return _err("Location not found", 404)

        try:
            if IS_POSTGRES:
                cur = conn.execute(
                    """INSERT INTO location_notes
                           (location_id, ln_author_user_id, ln_text)
                       VALUES (%s, %s, %s) RETURNING location_note_id""",
                    (location_id, current_user.user_id, text),
                )
                note_id = cur.fetchone()["location_note_id"]
            else:
                conn.execute(
                    """INSERT INTO location_notes
                           (location_id, ln_author_user_id, ln_text)
                       VALUES (?, ?, ?)""",
                    (location_id, current_user.user_id, text),
                )
                note_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return jsonify(_fetch_decorated(conn, note_id)), 201
    finally:
        conn.close()


# ── PUT /api/location-notes/<id> ──

@location_notes_bp.route("/location-notes/<int:note_id>", methods=["PUT"])
@login_required
def update_location_note(note_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    text = (body.get("ln_text") or "").strip()
    verr = _validate_text(text)
    if verr:
        return _err(verr, 400)

    conn = get_conn()
    try:
        note = _get_note_for_modify(conn, note_id, company_id)
        if not note:
            return _err("Note not found", 404)
        if not _user_can_modify(note):
            return _err("Forbidden", 403)

        try:
            conn.execute(
                q("UPDATE location_notes SET ln_text = ? WHERE location_note_id = ?"),
                (text, note_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return jsonify(_fetch_decorated(conn, note_id))
    finally:
        conn.close()


# ── DELETE /api/location-notes/<id> (soft) ──

@location_notes_bp.route("/location-notes/<int:note_id>", methods=["DELETE"])
@login_required
def delete_location_note(note_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        note = _get_note_for_modify(conn, note_id, company_id)
        if not note:
            return _err("Note not found", 404)
        if not _user_can_modify(note):
            return _err("Forbidden", 403)

        try:
            if IS_POSTGRES:
                conn.execute(
                    "UPDATE location_notes SET ln_deleted_at = NOW() "
                    "WHERE location_note_id = %s",
                    (note_id,),
                )
            else:
                conn.execute(
                    "UPDATE location_notes SET ln_deleted_at = CURRENT_TIMESTAMP "
                    "WHERE location_note_id = ?",
                    (note_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return jsonify({"ok": True, "location_note_id": note_id})
    finally:
        conn.close()
