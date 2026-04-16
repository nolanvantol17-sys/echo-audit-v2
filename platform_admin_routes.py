"""
platform_admin_routes.py — Echo Audit V2 Phase 6 super-admin platform surface.

Every route here is super_admin only. Operations span all tenants — there is
no company scoping on reads, and writes are enforced at the database level by
careful DELETE/UPDATE filters.

See also: platform_routes.py (usage) which was enhanced in this phase with
today + month counters and an 80% flag. The org/users/health/impersonation
routes live here.
"""

import json
import logging
import secrets
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request, session
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

import auth
from audit_log import (
    ACTION_CREATED, ACTION_DELETED, ACTION_UPDATED,
    ENTITY_COMPANY, ENTITY_USER, write_audit_log,
)
from auth import role_required
from db import IS_POSTGRES, get_conn, q, seed_company_defaults

logger = logging.getLogger(__name__)

platform_admin_bp = Blueprint("platform_admin", __name__, url_prefix="/api/platform")

PASSWORD_METHOD = "pbkdf2:sha256:260000"
STATUS_ACTIVE    = 1
STATUS_INACTIVE  = 2
STATUS_SUSPENDED = 10


def _err(msg, code):
    return jsonify({"error": msg}), code


def _body():
    return request.get_json(silent=True) or {}


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _rows(cur):
    return [_row_to_dict(r) for r in cur.fetchall()]


# ═══════════════════════════════════════════════════════════════
# ORG MANAGEMENT
# ═══════════════════════════════════════════════════════════════


@platform_admin_bp.route("/orgs", methods=["GET"])
@login_required
@role_required("super_admin")
def list_orgs():
    conn = get_conn()
    try:
        cur = conn.execute(q("""
            SELECT
                c.company_id,
                c.company_name,
                c.status_id,
                s.status_name,
                c.company_created_at,
                (SELECT COUNT(*) FROM users u
                    JOIN departments d ON d.department_id = u.department_id
                    WHERE d.company_id = c.company_id
                      AND u.user_deleted_at IS NULL)         AS user_count,
                (SELECT COUNT(*) FROM interactions i
                    JOIN projects p ON p.project_id = i.project_id
                    WHERE p.company_id = c.company_id
                      AND i.interaction_deleted_at IS NULL)  AS interaction_count,
                (SELECT u.user_email FROM users u
                    JOIN departments d  ON d.department_id = u.department_id
                    JOIN user_roles ur  ON ur.user_role_id = u.user_role_id
                    JOIN roles r        ON r.role_id       = ur.role_id
                    WHERE d.company_id = c.company_id
                      AND r.role_name = 'admin'
                      AND u.user_deleted_at IS NULL
                    ORDER BY u.user_id ASC
                    LIMIT 1)                                 AS admin_email
            FROM companies c
            LEFT JOIN statuses s ON s.status_id = c.status_id
            WHERE c.company_deleted_at IS NULL
            ORDER BY c.company_created_at DESC
        """))
        return jsonify(_rows(cur))
    finally:
        conn.close()


@platform_admin_bp.route("/orgs", methods=["POST"])
@login_required
@role_required("super_admin")
def create_org_with_admin():
    body = _body()
    required = ("company_name", "industry_id", "admin_email",
                "admin_first_name", "admin_last_name", "admin_password")
    missing = [f for f in required if not body.get(f)]
    if missing:
        return _err(f"Missing required fields: {', '.join(missing)}", 400)

    admin_password = body["admin_password"]
    if len(admin_password) < 8:
        return _err("admin_password must be at least 8 characters", 400)

    # Use the email pre-check so we can fail fast before creating a company.
    if auth.email_exists(body["admin_email"]):
        return _err("A user with that email already exists", 409)

    conn = get_conn()
    try:
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO companies (company_name, industry_id, status_id)
                   VALUES (%s, %s, %s) RETURNING company_id""",
                (body["company_name"], body["industry_id"], STATUS_ACTIVE),
            )
            company_id = cur.fetchone()["company_id"]
        else:
            conn.execute(
                "INSERT INTO companies (company_name, industry_id, status_id) "
                "VALUES (?, ?, ?)",
                (body["company_name"], body["industry_id"], STATUS_ACTIVE),
            )
            company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Seed defaults + Leadership department in the same txn
        seed_company_defaults(company_id, conn=conn)

        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO departments (company_id, department_name, status_id)
                   VALUES (%s, %s, %s) RETURNING department_id""",
                (company_id, "Leadership", STATUS_ACTIVE),
            )
            dept_id = cur.fetchone()["department_id"]
        else:
            conn.execute(
                "INSERT INTO departments (company_id, department_name, status_id) "
                "VALUES (?, ?, ?)",
                (company_id, "Leadership", STATUS_ACTIVE),
            )
            dept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        write_audit_log(
            current_user.user_id, ACTION_CREATED, ENTITY_COMPANY, company_id,
            metadata={"company_name": body["company_name"],
                      "industry_id": body["industry_id"],
                      "source": "platform_admin"},
            conn=conn,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # auth.create_user manages its own connection + txn; company is already
    # persisted at this point so a user-creation failure leaves the company
    # visible (the admin can retry with POST /api/team once they log in).
    try:
        new_user_id = auth.create_user(
            email=body["admin_email"],
            password=admin_password,
            role_name="admin",
            first_name=body["admin_first_name"],
            last_name=body["admin_last_name"],
            department_id=dept_id,
        )
    except ValueError as e:
        msg = str(e)
        code = 409 if "already exists" in msg.lower() else 400
        return _err(msg, code)

    # Force must_change_password on the freshly created admin
    conn = get_conn()
    try:
        conn.execute(
            q("UPDATE users SET user_must_change_password = ? WHERE user_id = ?"),
            (True, new_user_id),
        )
        write_audit_log(
            current_user.user_id, ACTION_CREATED, ENTITY_USER, new_user_id,
            metadata={"email": body["admin_email"], "role_name": "admin",
                      "source": "platform_admin"},
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        "ok":            True,
        "company_id":    company_id,
        "user_id":       new_user_id,
        "temp_password": admin_password,
    }), 201


@platform_admin_bp.route("/orgs/<int:company_id>", methods=["GET"])
@login_required
@role_required("super_admin")
def get_org_detail(company_id):
    conn = get_conn()
    try:
        # Company
        cur = conn.execute(
            q("""SELECT c.*, s.status_name
                 FROM companies c
                 LEFT JOIN statuses s ON s.status_id = c.status_id
                 WHERE c.company_id = ?"""),
            (company_id,),
        )
        company = _row_to_dict(cur.fetchone())
        if not company:
            return _err("Organization not found", 404)

        # Users
        cur = conn.execute(
            q("""SELECT u.user_id, u.user_email, u.user_first_name,
                        u.user_last_name, u.status_id, s.status_name,
                        r.role_name, u.user_created_at
                 FROM users u
                 JOIN departments d  ON d.department_id = u.department_id
                 LEFT JOIN user_roles ur ON ur.user_role_id = u.user_role_id
                 LEFT JOIN roles      r  ON r.role_id       = ur.role_id
                 LEFT JOIN statuses   s  ON s.status_id     = u.status_id
                 WHERE d.company_id = ? AND u.user_deleted_at IS NULL
                 ORDER BY u.user_created_at DESC"""),
            (company_id,),
        )
        users = _rows(cur)

        # Projects
        cur = conn.execute(
            q("""SELECT project_id, project_name, status_id, project_start_date
                 FROM projects
                 WHERE company_id = ? AND project_deleted_at IS NULL
                 ORDER BY project_name"""),
            (company_id,),
        )
        projects = _rows(cur)

        # Interaction count
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 WHERE p.company_id = ? AND i.interaction_deleted_at IS NULL"""),
            (company_id,),
        )
        row = _row_to_dict(cur.fetchone()) or {"cnt": 0}
        interaction_count = row.get("cnt") or 0

        # VoIP config (presence only; never surface credentials)
        cur = conn.execute(
            q("""SELECT voip_config_provider, voip_config_is_active,
                        voip_config_auto_grade
                 FROM voip_configs WHERE company_id = ?"""),
            (company_id,),
        )
        voip_row = _row_to_dict(cur.fetchone())
        voip_status = {
            "connected":  bool(voip_row and voip_row.get("voip_config_is_active")),
            "provider":   voip_row["voip_config_provider"] if voip_row else None,
            "auto_grade": bool(voip_row and voip_row.get("voip_config_auto_grade")),
        }

        # Current-month API usage
        month_start = datetime.utcnow().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        cur = conn.execute(
            q("""SELECT au_service, SUM(au_request_count) AS total
                 FROM api_usage
                 WHERE company_id = ? AND au_period_type = 'day'
                   AND au_period_start >= ?
                 GROUP BY au_service"""),
            (company_id, month_start),
        )
        usage = {}
        for row in cur.fetchall():
            d = _row_to_dict(row)
            usage[d["au_service"]] = int(d["total"] or 0)
    finally:
        conn.close()

    return jsonify({
        "company":           company,
        "users":             users,
        "projects":          projects,
        "interaction_count": interaction_count,
        "voip_status":       voip_status,
        "monthly_usage":     usage,
    })


@platform_admin_bp.route("/orgs/<int:company_id>/deactivate", methods=["POST"])
@login_required
@role_required("super_admin")
def deactivate_org(company_id):
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT company_id FROM companies WHERE company_id = ?"),
            (company_id,),
        )
        if not cur.fetchone():
            return _err("Organization not found", 404)

        conn.execute(
            q("UPDATE companies SET status_id = ? WHERE company_id = ?"),
            (STATUS_SUSPENDED, company_id),
        )
        # Inactivate every user in the company in the same txn
        conn.execute(
            q("""UPDATE users SET status_id = ?
                 WHERE department_id IN (
                    SELECT department_id FROM departments WHERE company_id = ?
                 )"""),
            (STATUS_INACTIVE, company_id),
        )
        write_audit_log(
            current_user.user_id, ACTION_DELETED, ENTITY_COMPANY, company_id,
            metadata={"action": "deactivate_org_cascade"}, conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@platform_admin_bp.route("/orgs/<int:company_id>/reactivate", methods=["POST"])
@login_required
@role_required("super_admin")
def reactivate_org(company_id):
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT company_id FROM companies WHERE company_id = ?"),
            (company_id,),
        )
        if not cur.fetchone():
            return _err("Organization not found", 404)

        conn.execute(
            q("UPDATE companies SET status_id = ? WHERE company_id = ?"),
            (STATUS_ACTIVE, company_id),
        )
        conn.execute(
            q("""UPDATE users SET status_id = ?
                 WHERE department_id IN (
                    SELECT department_id FROM departments WHERE company_id = ?
                 )"""),
            (STATUS_ACTIVE, company_id),
        )
        write_audit_log(
            current_user.user_id, ACTION_UPDATED, ENTITY_COMPANY, company_id,
            metadata={"action": "reactivate_org_cascade"}, conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@platform_admin_bp.route("/orgs/<int:company_id>", methods=["DELETE"])
@login_required
@role_required("super_admin")
def hard_delete_org(company_id):
    """Hard delete — only allowed if zero interactions exist for this company.

    Deletion order is deliberate: child → parent so FK CASCADEs can't fire
    into audit_log or interactions accidentally. `companies` cascades to most
    children automatically but we still wipe the explicit ones first to get
    deterministic row counts back in the response.
    """
    conn = get_conn()
    try:
        # Existence check
        cur = conn.execute(
            q("SELECT company_id FROM companies WHERE company_id = ?"),
            (company_id,),
        )
        if not cur.fetchone():
            return _err("Organization not found", 404)

        # Zero-interactions guard — hard rule, no exceptions.
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 WHERE p.company_id = ?"""),
            (company_id,),
        )
        row = _row_to_dict(cur.fetchone()) or {"cnt": 0}
        if (row.get("cnt") or 0) > 0:
            return _err(
                "Cannot delete a company with interaction history. Deactivate instead.",
                409,
            )

        # Per spec: settings, labels, voip_config, voip_queue, api_keys,
        # api_usage, api_call_log, campaigns, departments, locations, users,
        # company. (Users are scoped through departments so we delete users
        # BEFORE departments.)
        conn.execute(q("DELETE FROM company_settings  WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM company_labels    WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM voip_configs      WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM voip_call_queue   WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM api_keys          WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM api_usage         WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM api_call_log      WHERE company_id = ?"), (company_id,))

        # Campaigns are scoped through locations.
        conn.execute(
            q("""DELETE FROM campaigns WHERE location_id IN (
                    SELECT location_id FROM locations WHERE company_id = ?
                 )"""),
            (company_id,),
        )
        # Users before departments (CASCADE from departments would lose them otherwise)
        conn.execute(
            q("""DELETE FROM users WHERE department_id IN (
                    SELECT department_id FROM departments WHERE company_id = ?
                 )"""),
            (company_id,),
        )
        conn.execute(q("DELETE FROM departments WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM locations   WHERE company_id = ?"), (company_id,))
        conn.execute(q("DELETE FROM companies   WHERE company_id = ?"), (company_id,))

        write_audit_log(
            current_user.user_id, ACTION_DELETED, ENTITY_COMPANY, company_id,
            metadata={"action": "hard_delete"}, conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ═══════════════════════════════════════════════════════════════


@platform_admin_bp.route("/users", methods=["GET"])
@login_required
@role_required("super_admin")
def list_all_users():
    filters = ["u.user_deleted_at IS NULL"]
    params = []
    if request.args.get("company_id"):
        filters.append("d.company_id = ?")
        params.append(request.args["company_id"])
    if request.args.get("role_name"):
        filters.append("r.role_name = ?")
        params.append(request.args["role_name"])
    if request.args.get("status_id"):
        filters.append("u.status_id = ?")
        params.append(request.args["status_id"])
    where = " AND ".join(filters)

    conn = get_conn()
    try:
        cur = conn.execute(
            q(f"""SELECT u.user_id, u.user_email, u.user_first_name,
                         u.user_last_name, r.role_name,
                         c.company_id, c.company_name,
                         u.status_id, s.status_name, u.user_created_at
                  FROM users u
                  JOIN departments d  ON d.department_id = u.department_id
                  JOIN companies c    ON c.company_id    = d.company_id
                  LEFT JOIN user_roles ur ON ur.user_role_id = u.user_role_id
                  LEFT JOIN roles      r  ON r.role_id       = ur.role_id
                  LEFT JOIN statuses   s  ON s.status_id     = u.status_id
                  WHERE {where}
                  ORDER BY u.user_created_at DESC"""),
            params,
        )
        return jsonify(_rows(cur))
    finally:
        conn.close()


@platform_admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@role_required("super_admin")
def reset_user_password(user_id):
    # Verify user exists + isn't deleted
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT user_id FROM users WHERE user_id = ? AND user_deleted_at IS NULL"),
            (user_id,),
        )
        if not cur.fetchone():
            return _err("User not found", 404)
    finally:
        conn.close()

    temp_password = secrets.token_urlsafe(9)[:12]  # ~12-char URL-safe random
    password_hash = generate_password_hash(temp_password, method=PASSWORD_METHOD)

    conn = get_conn()
    try:
        conn.execute(
            q("""UPDATE users SET user_password_hash = ?,
                                  user_must_change_password = ?
                 WHERE user_id = ?"""),
            (password_hash, True, user_id),
        )
        write_audit_log(
            current_user.user_id, ACTION_UPDATED, ENTITY_USER, user_id,
            metadata={"action": "password_reset_by_super_admin"},
            conn=conn,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify({"ok": True, "user_id": user_id, "temp_password": temp_password})


# ═══════════════════════════════════════════════════════════════
# IMPERSONATION
# ═══════════════════════════════════════════════════════════════


@platform_admin_bp.route("/users/<int:user_id>/impersonate", methods=["POST"])
@login_required
@role_required("super_admin")
def start_impersonation(user_id):
    # Verify target exists and is in some company
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT u.user_id, d.company_id
                 FROM users u
                 JOIN departments d ON d.department_id = u.department_id
                 WHERE u.user_id = ? AND u.user_deleted_at IS NULL"""),
            (user_id,),
        )
        row = _row_to_dict(cur.fetchone())
    finally:
        conn.close()

    if not row:
        return _err("User not found or not in a department", 404)
    if row["user_id"] == current_user.user_id:
        return _err("Cannot impersonate yourself", 400)

    session["impersonator_id"] = current_user.user_id
    session["impersonating_user_id"] = user_id
    # Clear any active_org_id so the impersonation context is unambiguous
    session.pop("active_org_id", None)

    write_audit_log(
        current_user.user_id, ACTION_UPDATED, ENTITY_USER, user_id,
        metadata={"action": "impersonation_started",
                  "impersonator_id": current_user.user_id},
    )
    return jsonify({"ok": True, "impersonating_user_id": user_id})


@platform_admin_bp.route("/users/impersonate/stop", methods=["POST"])
@login_required
def stop_impersonation():
    """Either: the super_admin (still in session) calls this to release an
    impersonation, OR the impersonated user hits it. Both are safe."""
    if not session.get("impersonating_user_id"):
        return jsonify({"ok": True, "was_impersonating": False})

    impersonating_user_id = session.pop("impersonating_user_id", None)
    impersonator_id = session.pop("impersonator_id", None)

    write_audit_log(
        impersonator_id or current_user.user_id,
        ACTION_UPDATED, ENTITY_USER, impersonating_user_id,
        metadata={"action": "impersonation_stopped",
                  "impersonator_id": impersonator_id},
    )
    return jsonify({"ok": True, "was_impersonating": True})


# ═══════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════


@platform_admin_bp.route("/health", methods=["GET"])
@login_required
@role_required("super_admin")
def platform_health():
    try:
        conn = get_conn()
        db_ok = True
    except Exception:
        return jsonify({"database": "unreachable"}), 200

    try:
        cur = conn.execute(q("""SELECT
                (SELECT COUNT(*) FROM companies)   AS total_companies,
                (SELECT COUNT(*) FROM users WHERE user_deleted_at IS NULL)
                                                   AS total_users,
                (SELECT COUNT(*) FROM interactions WHERE interaction_deleted_at IS NULL)
                                                   AS total_interactions,
                (SELECT COUNT(*) FROM voip_configs WHERE voip_config_is_active = TRUE)
                                                   AS voip_connected,
                (SELECT COUNT(*) FROM voip_configs
                    WHERE voip_config_is_active = TRUE
                      AND voip_config_auto_grade = TRUE)
                                                   AS voip_auto_grade_enabled
        """))
        totals = _row_to_dict(cur.fetchone()) or {}

        stuck_threshold = datetime.utcnow() - timedelta(minutes=10)
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM voip_call_queue
                 WHERE voip_queue_status = 'processing'
                   AND voip_queue_updated_at < ?"""),
            (stuck_threshold,),
        )
        stuck = _row_to_dict(cur.fetchone()) or {"cnt": 0}

        last_24h = datetime.utcnow() - timedelta(hours=24)
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM voip_call_queue
                 WHERE voip_queue_status = 'failed'
                   AND voip_queue_updated_at >= ?"""),
            (last_24h,),
        )
        failed_24h = _row_to_dict(cur.fetchone()) or {"cnt": 0}
    finally:
        conn.close()

    return jsonify({
        "database":                "ok" if db_ok else "unreachable",
        "total_companies":         int(totals.get("total_companies") or 0),
        "total_users":             int(totals.get("total_users") or 0),
        "total_interactions":      int(totals.get("total_interactions") or 0),
        "voip_connected":          int(totals.get("voip_connected") or 0),
        "voip_auto_grade_enabled": int(totals.get("voip_auto_grade_enabled") or 0),
        "stuck_processing_jobs":   int(stuck.get("cnt") or 0),
        "failed_queue_24h":        int(failed_24h.get("cnt") or 0),
    })


# ═══════════════════════════════════════════════════════════════
# ORG CONTEXT SWITCHER
# ═══════════════════════════════════════════════════════════════


@platform_admin_bp.route("/switch-org", methods=["POST"])
@login_required
@role_required("super_admin")
def switch_org():
    body = _body()
    company_id = body.get("company_id")
    if not company_id:
        return _err("company_id is required", 400)

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT company_id, company_name FROM companies
                 WHERE company_id = ?"""),
            (company_id,),
        )
        row = _row_to_dict(cur.fetchone())
    finally:
        conn.close()
    if not row:
        return _err("Company not found", 404)

    # Switching orgs clears any active impersonation — they're mutually exclusive.
    session.pop("impersonating_user_id", None)
    session.pop("impersonator_id", None)
    session["active_org_id"] = int(company_id)
    return jsonify({"ok": True, "company_name": row["company_name"]})


@platform_admin_bp.route("/clear-org", methods=["POST"])
@login_required
@role_required("super_admin")
def clear_org():
    session.pop("active_org_id", None)
    return jsonify({"ok": True})
