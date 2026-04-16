"""
audit_log_routes.py — Echo Audit V2 Phase 4 audit log read route.

Write helper lives in audit_log.py. This module only exposes the read side.
Scoped to the current company via the actor user's department → company chain.
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required

from auth import role_required
from db import get_conn, q
from helpers import get_effective_company_id

audit_log_bp = Blueprint("audit_log", __name__, url_prefix="/api")


def _err(msg, code):
    return jsonify({"error": msg}), code


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


@audit_log_bp.route("/audit-log", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def list_audit_log():
    """Last 100 audit events scoped to the current company.

    Scope is derived from the acting user's department → company. Events
    performed by users in a different company (or by a NULL actor) are
    filtered out. Super admins should use platform-level routes to inspect
    cross-tenant audit trails.
    """
    company_id = get_effective_company_id()
    if company_id is None:
        return _err(
            "No company context. Super admins must select an organization first.",
            400,
        )

    args = request.args
    filters = [
        "d.company_id = ?",
        "al.actor_user_id IS NOT NULL",
    ]
    params = [company_id]

    if args.get("action_type_id"):
        filters.append("al.audit_log_action_type_id = ?")
        params.append(args["action_type_id"])
    if args.get("target_entity_type_id"):
        filters.append("al.audit_log_target_entity_type_id = ?")
        params.append(args["target_entity_type_id"])
    if args.get("from_date"):
        filters.append("al.al_created_at >= ?")
        params.append(args["from_date"])
    if args.get("to_date"):
        filters.append("al.al_created_at <= ?")
        params.append(args["to_date"])

    where = " AND ".join(filters)
    sql = f"""
        SELECT
            al.audit_log_id,
            al.actor_user_id,
            (u.user_first_name || ' ' || u.user_last_name) AS actor_name,
            u.user_email AS actor_email,
            al.audit_log_action_type_id,
            at.audit_log_action_type_name,
            al.audit_log_target_entity_type_id,
            tt.audit_log_target_entity_type_name,
            al.al_target_entity_id,
            al.al_metadata,
            al.al_created_at
        FROM audit_log al
        JOIN users       u  ON u.user_id       = al.actor_user_id
        JOIN departments d  ON d.department_id = u.department_id
        JOIN audit_log_action_types         at ON at.audit_log_action_type_id        = al.audit_log_action_type_id
        LEFT JOIN audit_log_target_entity_types tt ON tt.audit_log_target_entity_type_id = al.audit_log_target_entity_type_id
        WHERE {where}
        ORDER BY al.al_created_at DESC
        LIMIT 100
    """

    conn = get_conn()
    try:
        cur = conn.execute(q(sql), params)
        return jsonify([_row_to_dict(r) for r in cur.fetchall()])
    finally:
        conn.close()
