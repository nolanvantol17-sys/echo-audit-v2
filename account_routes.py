"""
account_routes.py — Echo Audit V2 Phase 6 self-service account routes.

Every user can view and update their own profile and change their own
password here. No admin-only routes — the super-admin surface for managing
other users lives in platform_admin_routes.py.
"""

import logging

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required, login_user
from werkzeug.security import check_password_hash

import auth
from db import IS_POSTGRES, get_conn, q

logger = logging.getLogger(__name__)

account_bp = Blueprint("account", __name__, url_prefix="/api")


def _err(msg, code):
    return jsonify({"error": msg}), code


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


# ═══════════════════════════════════════════════════════════════
# GET /api/account
# ═══════════════════════════════════════════════════════════════


@account_bp.route("/account", methods=["GET"])
@login_required
def get_account():
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT u.user_id, u.user_email, u.user_first_name,
                        u.user_last_name, u.user_created_at, r.role_name
                 FROM users u
                 LEFT JOIN user_roles ur ON ur.user_role_id = u.user_role_id
                 LEFT JOIN roles      r  ON r.role_id       = ur.role_id
                 WHERE u.user_id = ?"""),
            (current_user.user_id,),
        )
        row = _row_to_dict(cur.fetchone())
    finally:
        conn.close()
    if not row:
        return _err("Account not found", 404)
    return jsonify(row)


# ═══════════════════════════════════════════════════════════════
# PUT /api/account
# ═══════════════════════════════════════════════════════════════


@account_bp.route("/account", methods=["PUT"])
@login_required
def update_account():
    body = request.get_json(silent=True) or {}
    allowed = ("user_first_name", "user_last_name", "user_email")
    fields = {k: body[k] for k in allowed if k in body and body[k] is not None}

    if not fields:
        return _err("No fields to update", 400)

    new_email = (fields.get("user_email") or "").strip()
    if "user_email" in fields:
        if not new_email:
            return _err("user_email cannot be blank", 400)
        fields["user_email"] = new_email

    conn = get_conn()
    try:
        if new_email:
            # Uniqueness check — case-insensitive, ignoring this user
            cur = conn.execute(
                q("""SELECT 1 FROM users
                     WHERE LOWER(user_email) = LOWER(?)
                       AND user_id <> ?
                       AND user_deleted_at IS NULL
                     LIMIT 1"""),
                (new_email, current_user.user_id),
            )
            if cur.fetchone():
                return _err("Email already taken", 409)

        sets = ", ".join(f"{k} = ?" for k in fields)
        params = list(fields.values()) + [current_user.user_id]
        conn.execute(q(f"UPDATE users SET {sets} WHERE user_id = ?"), params)
        conn.commit()

        cur = conn.execute(
            q("""SELECT u.user_id, u.user_email, u.user_first_name,
                        u.user_last_name, u.user_created_at, r.role_name
                 FROM users u
                 LEFT JOIN user_roles ur ON ur.user_role_id = u.user_role_id
                 LEFT JOIN roles      r  ON r.role_id       = ur.role_id
                 WHERE u.user_id = ?"""),
            (current_user.user_id,),
        )
        return jsonify(_row_to_dict(cur.fetchone()))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# POST /api/account/password
# ═══════════════════════════════════════════════════════════════


@account_bp.route("/account/password", methods=["POST"])
@login_required
def change_own_password():
    body = request.get_json(silent=True) or {}
    current_password  = body.get("current_password") or ""
    new_password      = body.get("new_password") or ""
    confirm_password  = body.get("confirm_password") or ""

    if not current_password or not new_password or not confirm_password:
        return _err("current_password, new_password, confirm_password are all required", 400)
    if new_password != confirm_password:
        return _err("new_password and confirm_password do not match", 400)
    if len(new_password) < 8:
        return _err("new_password must be at least 8 characters", 400)

    # Verify current password by looking up the hash ourselves — avoid relying
    # on the cached Flask-Login object in case the hash was rotated concurrently.
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT user_password_hash FROM users WHERE user_id = ?"),
            (current_user.user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return _err("Account not found", 404)
    try:
        current_hash = row["user_password_hash"]
    except (KeyError, TypeError, IndexError):
        current_hash = row[0]
    if not current_hash or not check_password_hash(current_hash, current_password):
        return _err("Current password is incorrect", 401)

    try:
        auth.update_password(
            user_id=current_user.user_id,
            new_password=new_password,
            clear_must_change=True,
        )
    except ValueError as e:
        return _err(str(e), 400)
    except Exception:
        logger.exception("Password update failed for user %s", current_user.user_id)
        return _err("Password update failed", 500)

    # Refresh the Flask-Login session so the in-memory user object picks up the
    # cleared must_change_password flag. Avoids a stale redirect loop after the
    # PW change.
    try:
        refreshed = auth.load_user(current_user.user_id)
        if refreshed is not None:
            login_user(refreshed)
    except Exception:
        logger.exception("Failed to refresh session after password change")

    return jsonify({"ok": True})
