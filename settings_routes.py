"""
settings_routes.py — Echo Audit V2 Phase 6 company settings routes.

Every route is company-scoped via get_effective_company_id(). Writes require
admin or super_admin. Keys are whitelisted in db.COMPANY_SETTING_KEYS —
unknown keys are rejected with 400. Values are always stored as TEXT; the
frontend interprets booleans and numbers.
"""

import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from audit_log import (
    ACTION_CREATED, ACTION_DELETED, ACTION_UPDATED,
    ENTITY_COMPANY, ENTITY_TRANSCRIPTION_HINT, write_audit_log,
)
from auth import role_required
from db import COMPANY_SETTING_KEYS, IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id
import grader

logger = logging.getLogger(__name__)

settings_bp = Blueprint("settings", __name__, url_prefix="/api")


def _err(msg, code):
    return jsonify({"error": msg}), code


def _require_company():
    cid = get_effective_company_id()
    if cid is None:
        return None, _err(
            "No company context. Super admins must select an organization first.",
            400,
        )
    return cid, None


def _upsert_setting(conn, company_id, key, value):
    if IS_POSTGRES:
        conn.execute(
            """INSERT INTO company_settings
                   (company_id, company_setting_key, company_setting_value)
               VALUES (%s, %s, %s)
               ON CONFLICT (company_id, company_setting_key) DO UPDATE
               SET company_setting_value      = EXCLUDED.company_setting_value,
                   company_setting_updated_at = NOW()""",
            (company_id, key, value),
        )
    else:
        conn.execute(
            """INSERT INTO company_settings
                   (company_id, company_setting_key, company_setting_value)
               VALUES (?, ?, ?)
               ON CONFLICT (company_id, company_setting_key) DO UPDATE
               SET company_setting_value = excluded.company_setting_value,
                   company_setting_updated_at = CURRENT_TIMESTAMP""",
            (company_id, key, value),
        )


# ═══════════════════════════════════════════════════════════════
# GET /api/settings
# ═══════════════════════════════════════════════════════════════


@settings_bp.route("/settings", methods=["GET"])
@login_required
def list_settings():
    company_id, err = _require_company()
    if err: return err
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT company_setting_key, company_setting_value
                 FROM company_settings
                 WHERE company_id = ?"""),
            (company_id,),
        )
        settings = {}
        for row in cur.fetchall():
            try:
                k = row["company_setting_key"]
                v = row["company_setting_value"]
            except (KeyError, TypeError, IndexError):
                k, v = row[0], row[1]
            settings[k] = v
        return jsonify(settings)
    finally:
        conn.close()


@settings_bp.route("/settings", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def bulk_update_settings():
    company_id, err = _require_company()
    if err: return err

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body:
        return _err("Request body must be a non-empty JSON object", 400)

    invalid = [k for k in body if k not in COMPANY_SETTING_KEYS]
    if invalid:
        return _err(f"Unknown setting key(s): {', '.join(invalid)}", 400)

    conn = get_conn()
    try:
        for key, value in body.items():
            if value is None:
                return _err(f"Setting '{key}' value cannot be null", 400)
            _upsert_setting(conn, company_id, key, str(value))
        write_audit_log(
            current_user.user_id, ACTION_UPDATED, ENTITY_COMPANY, company_id,
            metadata={"action": "settings_updated", "keys": list(body.keys())},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True, "updated": len(body)})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@settings_bp.route("/settings/<string:key>", methods=["GET"])
@login_required
def get_single_setting(key):
    company_id, err = _require_company()
    if err: return err
    if key not in COMPANY_SETTING_KEYS:
        return _err("Unknown setting key", 400)

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT company_setting_value FROM company_settings
                 WHERE company_id = ? AND company_setting_key = ?"""),
            (company_id, key),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"key": key, "value": None})
    try:
        value = row["company_setting_value"]
    except (KeyError, TypeError, IndexError):
        value = row[0]
    return jsonify({"key": key, "value": value})


@settings_bp.route("/settings/<string:key>", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def set_single_setting(key):
    company_id, err = _require_company()
    if err: return err
    if key not in COMPANY_SETTING_KEYS:
        return _err("Unknown setting key", 400)

    body = request.get_json(silent=True) or {}
    if "value" not in body:
        return _err("Missing 'value' field", 400)
    value = body["value"]
    if value is None:
        return _err("value cannot be null", 400)

    conn = get_conn()
    try:
        _upsert_setting(conn, company_id, key, str(value))
        write_audit_log(
            current_user.user_id, ACTION_UPDATED, ENTITY_COMPANY, company_id,
            metadata={"action": "settings_updated", "keys": [key]},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True, "key": key})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Transcription hints (per-tenant custom vocabulary)
# ═══════════════════════════════════════════════════════════════


def _validate_term(term):
    """Return (cleaned, error). cleaned preserves caller casing; whitespace trimmed."""
    if not isinstance(term, str):
        return None, "term must be a string"
    cleaned = term.strip()
    if len(cleaned) < grader.KEYTERM_MIN_LENGTH:
        return None, f"Each term must be at least {grader.KEYTERM_MIN_LENGTH} characters."
    if len(cleaned) > grader.KEYTERM_MAX_LENGTH:
        return None, f"Each term must be no more than {grader.KEYTERM_MAX_LENGTH} characters."
    return cleaned, None


def _active_count(conn, company_id):
    row = conn.execute(
        q("""SELECT COUNT(*) AS n FROM transcription_hints
             WHERE company_id = ? AND th_deleted_at IS NULL"""),
        (company_id,),
    ).fetchone()
    try:
        return row["n"]
    except (KeyError, TypeError, IndexError):
        return row[0]


def _existing_terms_lower(conn, company_id):
    rows = conn.execute(
        q("""SELECT th_term FROM transcription_hints
             WHERE company_id = ? AND th_deleted_at IS NULL"""),
        (company_id,),
    ).fetchall()
    return {r["th_term"].lower() for r in rows}


@settings_bp.route("/transcription-hints", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def list_transcription_hints():
    company_id, err = _require_company()
    if err: return err
    conn = get_conn()
    try:
        rows = conn.execute(
            q("""SELECT transcription_hint_id, th_term, th_created_at, th_updated_at
                 FROM transcription_hints
                 WHERE company_id = ? AND th_deleted_at IS NULL
                 ORDER BY th_term"""),
            (company_id,),
        ).fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r["transcription_hint_id"],
                "term": r["th_term"],
                "created_at": str(r["th_created_at"]) if r["th_created_at"] else None,
                "updated_at": str(r["th_updated_at"]) if r["th_updated_at"] else None,
            })
        return jsonify({
            "items": items,
            "count": len(items),
            "max_terms": grader.KEYTERMS_PROMPT_MAX_TERMS,
        })
    finally:
        conn.close()


@settings_bp.route("/transcription-hints", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def create_transcription_hint():
    company_id, err = _require_company()
    if err: return err

    body = request.get_json(silent=True) or {}
    term = body.get("term")
    cleaned, verr = _validate_term(term or "")
    if verr:
        return _err(verr, 400)

    conn = get_conn()
    try:
        if _active_count(conn, company_id) >= grader.KEYTERMS_PROMPT_MAX_TERMS:
            return _err(
                f"Limit reached: at most {grader.KEYTERMS_PROMPT_MAX_TERMS} active terms.",
                409,
            )
        existing = _existing_terms_lower(conn, company_id)
        if cleaned.lower() in existing:
            return _err("That term already exists.", 409)

        if IS_POSTGRES:
            row = conn.execute(
                """INSERT INTO transcription_hints (company_id, th_term)
                   VALUES (%s, %s) RETURNING transcription_hint_id""",
                (company_id, cleaned),
            ).fetchone()
            new_id = row["transcription_hint_id"]
        else:
            cur = conn.execute(
                q("""INSERT INTO transcription_hints (company_id, th_term)
                     VALUES (?, ?)"""),
                (company_id, cleaned),
            )
            new_id = cur.lastrowid

        write_audit_log(
            current_user.user_id, ACTION_CREATED, ENTITY_TRANSCRIPTION_HINT, new_id,
            metadata={"company_id": company_id, "term": cleaned},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True, "id": new_id, "term": cleaned})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@settings_bp.route("/transcription-hints/bulk", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def bulk_create_transcription_hints():
    """Bulk-add. Body: {terms: [str, ...]}. Returns categorized counts and would-exceed flag."""
    company_id, err = _require_company()
    if err: return err

    body = request.get_json(silent=True) or {}
    raw_terms = body.get("terms")
    if not isinstance(raw_terms, list):
        return _err("Body must include 'terms' array.", 400)

    valid = []
    invalid = []
    seen_in_payload = set()
    for raw in raw_terms:
        if not isinstance(raw, str):
            invalid.append({"term": str(raw), "reason": "not a string"})
            continue
        cleaned, verr = _validate_term(raw)
        if verr:
            invalid.append({"term": raw.strip(), "reason": verr})
            continue
        key = cleaned.lower()
        if key in seen_in_payload:
            invalid.append({"term": cleaned, "reason": "duplicate within paste"})
            continue
        seen_in_payload.add(key)
        valid.append(cleaned)

    conn = get_conn()
    try:
        existing = _existing_terms_lower(conn, company_id)
        duplicates = [t for t in valid if t.lower() in existing]
        to_add = [t for t in valid if t.lower() not in existing]

        current = _active_count(conn, company_id)
        would_total = current + len(to_add)
        if would_total > grader.KEYTERMS_PROMPT_MAX_TERMS:
            return jsonify({
                "ok": False,
                "would_exceed_cap": True,
                "current_count": current,
                "would_add": len(to_add),
                "max_terms": grader.KEYTERMS_PROMPT_MAX_TERMS,
                "duplicates": duplicates,
                "invalid": invalid,
            }), 409

        added_ids = []
        for term in to_add:
            if IS_POSTGRES:
                row = conn.execute(
                    """INSERT INTO transcription_hints (company_id, th_term)
                       VALUES (%s, %s) RETURNING transcription_hint_id""",
                    (company_id, term),
                ).fetchone()
                added_ids.append(row["transcription_hint_id"])
            else:
                cur = conn.execute(
                    q("""INSERT INTO transcription_hints (company_id, th_term)
                         VALUES (?, ?)"""),
                    (company_id, term),
                )
                added_ids.append(cur.lastrowid)

        if to_add:
            write_audit_log(
                current_user.user_id, ACTION_CREATED, ENTITY_TRANSCRIPTION_HINT, None,
                metadata={
                    "company_id": company_id,
                    "bulk_added": to_add,
                    "ids": added_ids,
                },
                conn=conn,
            )
        conn.commit()
        return jsonify({
            "ok": True,
            "added": len(to_add),
            "added_terms": to_add,
            "duplicates": duplicates,
            "invalid": invalid,
            "current_count": current + len(to_add),
            "max_terms": grader.KEYTERMS_PROMPT_MAX_TERMS,
        })
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@settings_bp.route("/transcription-hints/<int:hint_id>", methods=["PUT"])
@login_required
@role_required("admin", "super_admin")
def update_transcription_hint(hint_id):
    company_id, err = _require_company()
    if err: return err

    body = request.get_json(silent=True) or {}
    cleaned, verr = _validate_term(body.get("term") or "")
    if verr:
        return _err(verr, 400)

    conn = get_conn()
    try:
        row = conn.execute(
            q("""SELECT th_term FROM transcription_hints
                 WHERE transcription_hint_id = ? AND company_id = ?
                   AND th_deleted_at IS NULL"""),
            (hint_id, company_id),
        ).fetchone()
        if not row:
            return _err("Term not found.", 404)
        old_term = row["th_term"]

        if cleaned.lower() != old_term.lower():
            existing = _existing_terms_lower(conn, company_id)
            if cleaned.lower() in existing:
                return _err("That term already exists.", 409)

        conn.execute(
            q("""UPDATE transcription_hints
                 SET th_term = ?
                 WHERE transcription_hint_id = ? AND company_id = ?"""),
            (cleaned, hint_id, company_id),
        )
        write_audit_log(
            current_user.user_id, ACTION_UPDATED, ENTITY_TRANSCRIPTION_HINT, hint_id,
            metadata={"company_id": company_id, "before": old_term, "after": cleaned},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True, "id": hint_id, "term": cleaned})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@settings_bp.route("/transcription-hints/<int:hint_id>", methods=["DELETE"])
@login_required
@role_required("admin", "super_admin")
def delete_transcription_hint(hint_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        row = conn.execute(
            q("""SELECT th_term FROM transcription_hints
                 WHERE transcription_hint_id = ? AND company_id = ?
                   AND th_deleted_at IS NULL"""),
            (hint_id, company_id),
        ).fetchone()
        if not row:
            return _err("Term not found.", 404)
        term = row["th_term"]

        if IS_POSTGRES:
            conn.execute(
                """UPDATE transcription_hints SET th_deleted_at = NOW()
                   WHERE transcription_hint_id = %s AND company_id = %s""",
                (hint_id, company_id),
            )
        else:
            conn.execute(
                """UPDATE transcription_hints SET th_deleted_at = CURRENT_TIMESTAMP
                   WHERE transcription_hint_id = ? AND company_id = ?""",
                (hint_id, company_id),
            )
        write_audit_log(
            current_user.user_id, ACTION_DELETED, ENTITY_TRANSCRIPTION_HINT, hint_id,
            metadata={"company_id": company_id, "term": term},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True, "id": hint_id})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
