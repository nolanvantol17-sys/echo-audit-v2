"""Simulator — admin "view as user" (full act-as).

From the admin UI: pick a user → the app logs you in AS that user, so role,
menu, and row-scoping all render exactly as they'd see it (simulate an RM →
you land in their sealed read-only portal). A persistent banner shows you're
simulating, with a Terminate button that restores your admin account.

Implementation: full identity swap via flask_login.login_user(target). The
REAL admin's user_id is stashed in session[SIM_KEY] so Terminate can log back
in as the admin. This is DISTINCT from the legacy super-admin company-context
impersonation (session['impersonating_user_id'] in platform_admin_routes), which
only switches company context and keeps the super-admin's identity. Starting a
simulation clears those legacy keys so the two never overlap.

Safety:
  - Only admin / super_admin can START a simulation (role-gated).
  - A company admin may only simulate users in their OWN company; super_admins
    may simulate any user, any org. Enforced server-side.
  - Cannot start while already simulating (must Terminate first) — prevents
    losing the real admin id.
  - STOP only ever restores the stashed admin id — it cannot escalate to an
    arbitrary user. It is allowlisted in the manager seal so a simulated RM can
    still terminate.
"""
import logging

from flask import (Blueprint, abort, redirect, render_template, request,
                   session, url_for)
from flask_login import current_user, login_required, login_user, logout_user

import auth
from auth import role_required
from db import get_conn, q

logger = logging.getLogger(__name__)

simulator_bp = Blueprint("simulator", __name__)

SIM_KEY = "sim_real_user_id"          # real admin's user_id while simulating
STOP_PATH = "/app/simulator/stop"     # mirror in app.py _RM_ALWAYS_ALLOWED


def _audit(actor_id, target_id, action_label):
    """Best-effort audit log; never blocks the flow."""
    try:
        from audit_log import write_audit_log, ACTION_UPDATED, ENTITY_USER
        write_audit_log(actor_id, ACTION_UPDATED, ENTITY_USER, target_id,
                        metadata={"action": action_label, "via": "simulator"})
    except Exception:
        logger.info("[simulator] %s actor=%s target=%s", action_label, actor_id, target_id)


def _dict(row):
    keys = ("user_id", "user_email", "user_first_name", "user_last_name",
            "role_name", "company_name", "company_id")
    out = {}
    for k in keys:
        try:
            out[k] = row[k]
        except (KeyError, IndexError, TypeError):
            out[k] = None
    out["full_name"] = " ".join(p for p in (out["user_first_name"], out["user_last_name"]) if p) or out["user_email"]
    return out


@simulator_bp.route("/app/simulator")
@login_required
@role_required("admin", "super_admin")
def simulator_page():
    # Only a REAL admin (not mid-simulation) should reach the picker.
    if session.get(SIM_KEY):
        return redirect(url_for("app_home"))

    conn = get_conn()
    try:
        base = """
            SELECT u.user_id, u.user_email, u.user_first_name, u.user_last_name,
                   r.role_name, d.company_id, c.company_name
            FROM users u
            LEFT JOIN user_roles ur ON ur.user_role_id = u.user_role_id
            LEFT JOIN roles      r  ON r.role_id        = ur.role_id
            {dept_join} departments d ON d.department_id = u.department_id
            LEFT JOIN companies  c  ON c.company_id     = d.company_id
            WHERE u.user_deleted_at IS NULL AND u.status_id = 1
              AND u.user_id <> ?
        """
        if current_user.is_super_admin:
            sql = base.format(dept_join="LEFT JOIN") + \
                " ORDER BY c.company_name, r.role_name, u.user_first_name"
            cur = conn.execute(q(sql), (current_user.user_id,))
        else:
            # company admin: own org only (INNER JOIN on dept to enforce company),
            # and never list super-admins (they can't be simulated by a non-
            # super-admin — keeps the picker consistent with the start guard).
            sql = base.format(dept_join="JOIN") + \
                " AND d.company_id = ?" \
                " AND (r.role_name IS NULL OR r.role_name <> 'super_admin')" \
                " ORDER BY r.role_name, u.user_first_name"
            cur = conn.execute(q(sql), (current_user.user_id, current_user.company_id))
        users = [_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return render_template("simulator.html", sim_users=users)


@simulator_bp.route("/app/simulator/start/<int:user_id>", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def simulator_start(user_id):
    # Re-entrancy guard: must terminate an active simulation first.
    if session.get(SIM_KEY):
        return redirect(url_for("app_home"))
    if user_id == current_user.user_id:
        return redirect(url_for("simulator.simulator_page"))

    conn = get_conn()
    try:
        row = auth._load_user_row(conn, user_id=user_id)
    finally:
        conn.close()
    if not row:
        abort(404)
    target = auth.User(row)

    # Privilege guard: a non-super-admin must NEVER become a super-admin, even
    # if one were somehow linked to their company (defense-in-depth — no
    # privilege escalation via the simulator regardless of data anomalies).
    if not current_user.is_super_admin and target.is_super_admin:
        abort(403)
    # Tenant guard: company admins may only simulate within their own company.
    if not current_user.is_super_admin:
        if target.company_id is None or target.company_id != current_user.company_id:
            abort(403)
    # Only active users can be simulated (login_user rejects inactive anyway).
    if not target.is_active:
        return redirect(url_for("simulator.simulator_page"))

    real_admin_id = current_user.user_id        # capture BEFORE the swap

    # Clear legacy super-admin company-impersonation so state is unambiguous.
    session.pop("impersonating_user_id", None)
    session.pop("impersonator_id", None)
    session.pop("active_org_id", None)

    login_user(target)                          # current_user becomes target
    session[SIM_KEY] = real_admin_id
    _audit(real_admin_id, user_id, "simulation_started")
    logger.info("[simulator] admin %s now simulating user %s (role=%s)",
                real_admin_id, user_id, target.role)
    return redirect(url_for("app_home"))


@simulator_bp.route("/app/simulator/stop", methods=["POST"])
@login_required
def simulator_stop():
    real_id = session.pop(SIM_KEY, None)
    if not real_id:
        return redirect(url_for("app_home"))

    conn = get_conn()
    try:
        row = auth._load_user_row(conn, user_id=real_id)
    finally:
        conn.close()

    if row:
        # force=True so Terminate always works, even if the admin account was
        # deactivated mid-simulation — they must never get stuck as the target.
        login_user(auth.User(row), force=True)  # restore the real admin
        _audit(real_id, real_id, "simulation_stopped")
        logger.info("[simulator] simulation stopped; restored admin %s", real_id)
        return redirect(url_for("simulator.simulator_page"))
    # Admin row vanished mid-simulation — fail safe by logging out entirely.
    logout_user()
    return redirect(url_for("login"))
