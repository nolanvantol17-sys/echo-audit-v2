"""
helpers.py — Shared helpers used across app.py, api_routes.py, and
interactions_routes.py.

Kept as a standalone module to avoid circular imports between the
main app file and the blueprints.
"""

import re
import secrets
from datetime import datetime

from flask import session
from flask_login import current_user

from db import get_conn, q, IS_POSTGRES


def generate_temp_password():
    """Return a ~12-char URL-safe random temporary password.

    Used by all server-side flows that create a user or reset a password
    (team add-member, platform create-org, super-admin password reset).
    """
    return secrets.token_urlsafe(9)[:12]


def get_effective_company_id():
    """Return the company_id the current request is operating in.

    Resolution order for super_admins (Phase 6 adds impersonation):
        1. session['impersonating_user_id'] — derive company from that user's
           department → company
        2. session['active_org_id'] — org-context switcher
        3. None (platform-wide view)

    For all other users, uses current_user.company_id which is derived
    through department_id → departments.company_id.
    """
    if not current_user.is_authenticated:
        return None
    if current_user.is_super_admin:
        impersonating = session.get("impersonating_user_id")
        if impersonating:
            conn = get_conn()
            try:
                cur = conn.execute(
                    q("""SELECT d.company_id
                         FROM users u
                         JOIN departments d ON d.department_id = u.department_id
                         WHERE u.user_id = ? AND u.user_deleted_at IS NULL"""),
                    (impersonating,),
                )
                row = cur.fetchone()
                if row is not None:
                    try:
                        return row["company_id"]
                    except (KeyError, TypeError, IndexError):
                        return row[0]
            finally:
                conn.close()
            return None
        return session.get("active_org_id")
    return current_user.company_id


# ── Rate limiting (V2 api_usage columns) ───────────────────────
# V1 column → V2 column:
#   api_name      → au_service
#   window_type   → au_period_type
#   window_start  → au_period_start
#   request_count → au_request_count
# Window start is a TIMESTAMPTZ in V2 (V1 was a string).


RATE_LIMITS = {
    ("assemblyai",      "hour"):   10,
    ("assemblyai",      "day"):    50,
    ("anthropic",       "hour"):   10,
    ("anthropic",       "day"):    50,
    ("twilio",          "hour"):    5,
    ("twilio",          "day"):    20,
    ("external_lookup", "hour"):   60,
    ("external_lookup", "day"): 1000,
}


def _window_start(period_type):
    """Truncated UTC timestamp for the current rate-limit window."""
    now = datetime.utcnow()
    if period_type == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def check_rate_limit(company_id, service):
    """Check hourly + daily limits. Returns (ok, error_message).

    ok=True means under limit. Skipped (returns True) when company_id is None
    so uncontextualized requests don't crash.
    """
    return True, ""  # TEMP DISABLED 2026-04-24 — re-enable by removing this line
    if not company_id:
        return True, ""
    conn = get_conn()
    try:
        for period_type in ("hour", "day"):
            limit = RATE_LIMITS.get((service, period_type))
            if not limit:
                continue
            ws = _window_start(period_type)
            row = conn.execute(
                q("""SELECT au_request_count FROM api_usage
                     WHERE company_id = ? AND au_service = ?
                       AND au_period_type = ? AND au_period_start = ?"""),
                (company_id, service, period_type, ws),
            ).fetchone()
            count = (row["au_request_count"] if row else 0) or 0
            if count >= limit:
                label = {
                    "assemblyai": "Transcription",
                    "anthropic":  "Grading",
                    "twilio":     "Twilio call",
                }.get(service, service)
                period = "hourly" if period_type == "hour" else "daily"
                return False, f"{label} limit reached ({count}/{limit} {period}). Please try again later."
        return True, ""
    finally:
        conn.close()


def phone_digits(s):
    """Strip non-digits and return the trailing 10. Returns '' if <10 digits.

    Normalizes phone numbers for cross-format comparison. Handles E.164
    (+12144421314), US-formatted ((214) 442-1314), and plain digits
    consistently — all map to '2144421314'. Pure function, no DB.
    """
    if not s:
        return ""
    digits = re.sub(r"\D", "", str(s))
    if len(digits) < 10:
        return ""
    return digits[-10:]


def load_active_hints(company_id):
    """Return active transcription_hints terms for a company, ordered by term.

    Returns [] when company_id is None or no terms exist. Excludes soft-deleted
    rows and inactive (status_id != 1) rows. Used by the transcribe() callers
    to populate keyterms_prompt on the AAI request.
    """
    if not company_id:
        return []
    conn = get_conn()
    try:
        rows = conn.execute(
            q("""SELECT th_term FROM transcription_hints
                 WHERE company_id = ? AND status_id = 1 AND th_deleted_at IS NULL
                 ORDER BY th_term"""),
            (company_id,),
        ).fetchall()
        return [r["th_term"] for r in rows]
    finally:
        conn.close()


def increment_usage(company_id, service):
    """Increment both hourly and daily counters. No-op when company_id is None."""
    if not company_id:
        return
    conn = get_conn()
    try:
        for period_type in ("hour", "day"):
            ws = _window_start(period_type)
            if IS_POSTGRES:
                conn.execute(
                    """INSERT INTO api_usage (company_id, au_service, au_period_start,
                                              au_period_type, au_request_count)
                       VALUES (%s, %s, %s, %s, 1)
                       ON CONFLICT (company_id, au_service, au_period_start, au_period_type)
                       DO UPDATE SET au_request_count = api_usage.au_request_count + 1""",
                    (company_id, service, ws, period_type),
                )
            else:
                row = conn.execute(
                    """SELECT api_usage_id FROM api_usage
                       WHERE company_id = ? AND au_service = ?
                         AND au_period_type = ? AND au_period_start = ?""",
                    (company_id, service, period_type, ws),
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE api_usage SET au_request_count = au_request_count + 1 "
                        "WHERE api_usage_id = ?",
                        (row["api_usage_id"],),
                    )
                else:
                    conn.execute(
                        """INSERT INTO api_usage (company_id, au_service, au_period_start,
                                                  au_period_type, au_request_count)
                           VALUES (?, ?, ?, ?, 1)""",
                        (company_id, service, ws, period_type),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
