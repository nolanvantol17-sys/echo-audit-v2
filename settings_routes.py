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

from audit_log import ACTION_UPDATED, ENTITY_COMPANY, write_audit_log
from auth import role_required
from db import COMPANY_SETTING_KEYS, IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id

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
