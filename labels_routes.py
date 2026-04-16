"""
labels_routes.py — Echo Audit V2 Phase 6 company_labels routes.

Arbitrary key/value metadata per company (billing codes, contract tiers,
account manager names, etc.). Keys must match ^[A-Za-z0-9_]+$ — enforced at
the route layer so the UI can surface a clean error.
"""

import logging
import re

from flask import Blueprint, jsonify, request
from flask_login import login_required

from auth import role_required
from db import IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id

logger = logging.getLogger(__name__)

labels_bp = Blueprint("labels", __name__, url_prefix="/api")

_LABEL_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


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


def _rows(cur):
    out = []
    for row in cur.fetchall():
        try:
            out.append(dict(row))
        except Exception:
            out.append({k: row[k] for k in row.keys()})
    return out


@labels_bp.route("/labels", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def list_labels():
    company_id, err = _require_company()
    if err: return err
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT cl_key, cl_value, cl_created_at, cl_updated_at
                 FROM company_labels WHERE company_id = ?
                 ORDER BY cl_key"""),
            (company_id,),
        )
        return jsonify(_rows(cur))
    finally:
        conn.close()


@labels_bp.route("/labels", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def upsert_label():
    company_id, err = _require_company()
    if err: return err

    body = request.get_json(silent=True) or {}
    cl_key = (body.get("cl_key") or "").strip()
    cl_value = body.get("cl_value")

    if not cl_key or cl_value is None:
        return _err("cl_key and cl_value are required", 400)
    if not _LABEL_KEY_RE.match(cl_key):
        return _err("cl_key must contain only letters, digits, and underscores", 400)

    cl_value = str(cl_value)

    conn = get_conn()
    try:
        if IS_POSTGRES:
            conn.execute(
                """INSERT INTO company_labels (company_id, cl_key, cl_value)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (company_id, cl_key) DO UPDATE
                   SET cl_value      = EXCLUDED.cl_value,
                       cl_updated_at = NOW()""",
                (company_id, cl_key, cl_value),
            )
        else:
            conn.execute(
                """INSERT INTO company_labels (company_id, cl_key, cl_value)
                   VALUES (?, ?, ?)
                   ON CONFLICT (company_id, cl_key) DO UPDATE
                   SET cl_value = excluded.cl_value,
                       cl_updated_at = CURRENT_TIMESTAMP""",
                (company_id, cl_key, cl_value),
            )
        conn.commit()
        return jsonify({"ok": True, "cl_key": cl_key, "cl_value": cl_value})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@labels_bp.route("/labels/<string:key>", methods=["DELETE"])
@login_required
@role_required("admin", "super_admin")
def delete_label(key):
    company_id, err = _require_company()
    if err: return err
    if not _LABEL_KEY_RE.match(key):
        return _err("Invalid cl_key", 400)

    conn = get_conn()
    try:
        # Verify ownership by bounding the DELETE to this company
        cur = conn.execute(
            q("""DELETE FROM company_labels
                 WHERE company_id = ? AND cl_key = ?"""),
            (company_id, key),
        )
        # rowcount differs across backends; try and fall back.
        try:
            count = cur.rowcount
        except Exception:
            count = None
        conn.commit()
        if count == 0:
            return _err("Label not found", 404)
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
