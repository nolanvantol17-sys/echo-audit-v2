"""
helpers.py — Shared helpers used across app.py, api_routes.py, and
interactions_routes.py.

Kept as a standalone module to avoid circular imports between the
main app file and the blueprints.
"""

import logging
import re
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

from flask import request, session
from flask_login import current_user

from db import get_conn, q, IS_POSTGRES

logger = logging.getLogger(__name__)


def safe_next_url(target):
    """Validate a post-login redirect target ('next') and return a SAFE
    site-relative path (including query string), or None.

    Lets an emailed deep link — e.g. a shared dashboard view with
    ?location_ids=... — survive the login round-trip (password OR SSO)
    instead of dumping the user at the default home. Guards against
    open-redirect: only same-host site-relative paths are accepted;
    absolute URLs, protocol-relative (//evil.com), and backslash tricks
    are rejected, as are the auth pages themselves (avoids redirect loops).
    Shared by app.py (password login) and sso_routes.py (Microsoft SSO).
    """
    if not target:
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    test = urlparse(urljoin(request.host_url, target))
    if test.netloc != urlparse(request.host_url).netloc:
        return None
    rel = test.path + (("?" + test.query) if test.query else "")
    if rel.startswith(("/login", "/logout", "/signup", "/auth/")):
        return None
    return rel


def to_iso_date(v):
    """Normalize a DATE value to a 'YYYY-MM-DD' string (or None).

    Postgres returns datetime.date objects, which Flask's JSON serializer would
    otherwise emit as an HTTP/GMT string ("Wed, 01 Apr 2026 00:00:00 GMT") —
    breaking <input type="date"> round-trips. SQLite returns the stored text
    already. Use this on any date column before jsonify so the API always
    speaks plain ISO dates.
    """
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


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


def verify_attribution_tenancy(
    conn,
    company_id: int,
    project_id: int,
    location_id: int,
    caller_user_id: int,
    campaign_id: Optional[int] = None,
) -> Optional[str]:
    """Verify all attribution IDs resolve to the given company.

    Used by external-attribution providers (ElevenLabs today; any future
    provider that supplies project/location/caller IDs via webhook) to
    ensure the caller can't address another tenant's data even if their
    config is buggy or compromised.

    Returns None on success, a "tenant_mismatch: ..." error string on any
    failure. Each ID is checked independently against its canonical
    company source. The function never raises — bad input returns an
    error string the caller writes to voip_queue_error.
    """
    # project_id → projects.company_id
    cur = conn.execute(
        q("""SELECT company_id FROM projects
              WHERE project_id = ? AND project_deleted_at IS NULL"""),
        (project_id,),
    )
    row = cur.fetchone()
    if not row:
        return f"tenant_mismatch: project_id {project_id} not found or deleted"
    if dict(row)["company_id"] != company_id:
        return (f"tenant_mismatch: project_id {project_id} belongs to "
                f"company {dict(row)['company_id']}, expected {company_id}")

    # location_id → locations.company_id
    cur = conn.execute(
        q("""SELECT company_id FROM locations
              WHERE location_id = ? AND location_deleted_at IS NULL"""),
        (location_id,),
    )
    row = cur.fetchone()
    if not row:
        return f"tenant_mismatch: location_id {location_id} not found or deleted"
    if dict(row)["company_id"] != company_id:
        return (f"tenant_mismatch: location_id {location_id} belongs to "
                f"company {dict(row)['company_id']}, expected {company_id}")

    # caller_user_id → users.department_id → departments.company_id.
    # Super_admins are exempt: they're cross-org by design and have no
    # department binding, so the department→company chain doesn't apply.
    # LEFT JOIN so the user row comes back even when they have no department;
    # the role check below decides whether that's allowed.
    cur = conn.execute(
        q("""SELECT d.company_id, r.role_name
               FROM users u
               LEFT JOIN departments d  ON d.department_id   = u.department_id
               LEFT JOIN user_roles  ur ON ur.user_role_id   = u.user_role_id
               LEFT JOIN roles       r  ON r.role_id         = ur.role_id
              WHERE u.user_id = ? AND u.user_deleted_at IS NULL"""),
        (caller_user_id,),
    )
    row = cur.fetchone()
    if not row:
        return (f"tenant_mismatch: caller_user_id {caller_user_id} not found "
                "or deleted")
    row_dict = dict(row)
    is_super_admin = row_dict.get("role_name") == "super_admin"
    if not is_super_admin:
        if row_dict.get("company_id") is None:
            return (f"tenant_mismatch: caller_user_id {caller_user_id} has "
                    "no department")
        if row_dict["company_id"] != company_id:
            return (f"tenant_mismatch: caller_user_id {caller_user_id} belongs "
                    f"to company {row_dict['company_id']}, expected {company_id}")

    # Optional campaign_id → must belong to the (already-verified) project
    if campaign_id is not None:
        cur = conn.execute(
            q("""SELECT project_id FROM campaigns
                  WHERE campaign_id = ? AND campaign_deleted_at IS NULL"""),
            (campaign_id,),
        )
        row = cur.fetchone()
        if not row:
            return f"tenant_mismatch: campaign_id {campaign_id} not found or deleted"
        if dict(row)["project_id"] != project_id:
            return (f"tenant_mismatch: campaign_id {campaign_id} belongs to "
                    f"project {dict(row)['project_id']}, expected {project_id}")

    return None


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


# ── Feature flags + permission filtering ───────────────────────
# Feature flags ride on company_settings (EAV) with an `ff_` key prefix.
# Convention: value "1" / "true" / "yes" / "on" = ON. Anything else = OFF.
# Request-cached so a single page doesn't pay N DB roundtrips for repeated
# flag checks across the call stack.


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def is_feature_enabled(company_id, key, default=False):
    """Return True/False for a company-scoped feature flag.

    Reads company_settings where company_setting_key = `key`. Missing rows
    return `default`. Per-request cached on flask.g._ff_cache so the same
    flag check across a route + helpers + template doesn't fan out into
    multiple DB calls.

    Safe to call outside a request context (CLI, sync jobs) — falls back
    to a one-shot uncached read.
    """
    if not company_id:
        return default

    cache = None
    cache_key = None
    try:
        from flask import g, has_request_context  # local import: avoid
        # forcing Flask context at module import time on the CLI path.
        if has_request_context():
            cache = getattr(g, "_ff_cache", None)
            if cache is None:
                cache = {}
                g._ff_cache = cache
            cache_key = (company_id, key)
            if cache_key in cache:
                return cache[cache_key]
    except Exception:
        cache = None

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

    if row is None:
        result = default
    else:
        try:
            val = row["company_setting_value"]
        except (KeyError, TypeError, IndexError):
            val = row[0]
        result = _truthy(val)

    if cache is not None and cache_key is not None:
        cache[cache_key] = result
    return result


def location_scope_for_user(user_id, role, company_id,
                             column="i.interaction_location_id"):
    """Return (sql_fragment, params) to scope a query to the locations the
    requesting user is authorized to see.

    Contract:
      - sql_fragment is either "" (no restriction) or a SQL predicate that
        assumes `<column>` is in scope of the outer WHERE clause. Caller
        composes via:
            scope_sql, scope_params = location_scope_for_user(...)
            sql = f"... WHERE base ... {(' AND ' + scope_sql) if scope_sql else ''}"
            params = [..., *scope_params]
      - `column` lets callers point the IN-clause at a different table
        column. Defaults to `i.interaction_location_id` (the original
        callers, all scoping interactions). For locations-list surfaces
        pass `column="l.location_id"`.

    Rules (only apply when company_settings.ff_permission_filtering is on):
      - manager → restrict to locations where the requesting user's
        users.mayfair_user_id is the property's RM (locations.mayfair_rm_user_id)
        OR its Sr. Asset Manager (locations.mayfair_am_user_id) OR there is an
        explicit manual grant in location_portal_grants (external sponsors/owners
        scoped to a single property). A manager who hasn't been linked to a
        Mayfair user (mayfair_user_id IS NULL) returns the deny-all fragment
        '1=0' so they see nothing rather than the whole company. The remedy
        is admin: run the Mayfair sync + confirm the user's email matches a
        Mayfair RM/AM email, or add a location_portal_grants row.
      - admin, super_admin, caller → unrestricted (empty fragment).

    When the flag is off → unrestricted for all roles (preserves today's
    behavior; this is the default).

    Per-request cached on flask.g._rm_scope_cache keyed by (user_id, column)
    so a single request that scopes both interactions and locations doesn't
    serve the wrong fragment from cache.
    """
    if not is_feature_enabled(company_id, "ff_permission_filtering", default=False):
        return "", []
    if role != "manager":
        return "", []

    cache_key = (user_id, column)

    # Manager (RM) — resolve their mayfair_user_id. Cache once per request.
    cache = None
    try:
        from flask import g, has_request_context
        if has_request_context():
            cache = getattr(g, "_rm_scope_cache", None)
            if cache is None:
                cache = {}
                g._rm_scope_cache = cache
            if cache_key in cache:
                return cache[cache_key]
    except Exception:
        cache = None

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT mayfair_user_id, user_all_locations_readonly FROM users
                  WHERE user_id = ? AND user_deleted_at IS NULL"""),
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    mayfair_uid = None
    all_locations = False
    if row is not None:
        try:
            mayfair_uid = row["mayfair_user_id"]
            all_locations = bool(row["user_all_locations_readonly"])
        except (KeyError, TypeError, IndexError):
            mayfair_uid = row[0]
            all_locations = bool(row[1])

    if all_locations:
        # Company-wide read-only viewer (e.g. an executive / President): sees
        # every non-deleted location in their company, current AND future — no
        # per-property RM/AM/grant needed. Read-only is enforced by the manager
        # seal (deny-by-default gate in app.py), not here. Tenant-scoped to
        # company_id; company_id is always real here (the ff gate above is False
        # when it's None).
        result = (
            f"{column} IN ("
            "SELECT location_id FROM locations "
            "WHERE company_id = ? AND location_deleted_at IS NULL)",
            [company_id],
        )
    elif not mayfair_uid:
        logger.warning(
            "[location_scope] manager user_id=%s has no mayfair_user_id — "
            "denying all rows under ff_permission_filtering. "
            "Run the Mayfair sync + confirm the user's email matches a "
            "Mayfair RM email.", user_id,
        )
        result = ("1=0", [])
    else:
        # A manager sees a property if they are its Regional Manager, its
        # Sr. Asset Manager, OR they have an explicit manual portal grant
        # (e.g. an external sponsor scoped to a single property). All three
        # match on Mayfair's user-id space; UNION de-dupes overlaps.
        # Both arms are tenant-scoped to company_id so the fragment is
        # self-contained (defense-in-depth: callers also constrain company,
        # but a manual grant must never resolve a location in another company
        # that happens to share a user-id namespace). company_id is always a
        # real value here — the ff_permission_filtering gate above is False
        # when company_id is None, so this branch never runs without it.
        result = (
            f"{column} IN ("
            "SELECT location_id FROM locations "
            "WHERE (mayfair_rm_user_id = ? OR mayfair_am_user_id = ?) "
            "AND company_id = ? AND location_deleted_at IS NULL"
            " UNION "
            "SELECT g.location_id FROM location_portal_grants g "
            "JOIN locations l ON l.location_id = g.location_id "
            "AND l.location_deleted_at IS NULL AND l.company_id = ? "
            "WHERE g.mayfair_user_id = ?"
            ")",
            [mayfair_uid, mayfair_uid, company_id, company_id, mayfair_uid],
        )

    if cache is not None:
        cache[cache_key] = result
    return result


# ── Per-project access restriction ──────────────────────────────────────────
# A project can be marked "restricted" (projects.project_is_restricted). A
# restricted project is visible ONLY to admins / super_admins and the users
# explicitly listed in the project_access allowlist; everyone else is denied as
# if it didn't exist. Enforcement composes into the SAME WHERE-clause channel as
# location_scope_for_user() at every interaction/aggregate read, and gates the
# single-project ownership getters. All lookups FAIL OPEN (treat nothing as
# restricted) if the column/table aren't present yet — so a deploy that lands
# before the additive migration can't break project access. No project is
# actually restricted until an admin sets the flag, which happens after migrate.

def restricted_project_ids_for_user(user_id, role, company_id, conn=None):
    """Return restricted project_ids in `company_id` this user may NOT see.

    [] for admin/super_admin (they bypass) and when nothing applies. Compose via
    project_hide_clause()/add_project_hide(); for a single project use
    user_can_access_project()."""
    if role in ("admin", "super_admin"):
        return []
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cur = conn.execute(q("""
            SELECT p.project_id FROM projects p
            WHERE p.company_id = ?
              AND p.project_is_restricted
              AND p.project_deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM project_access pa
                  WHERE pa.project_id = p.project_id AND pa.user_id = ?
              )"""), (company_id, user_id))
        rows = cur.fetchall()
    except Exception:
        logger.warning("[project_access] restriction lookup failed; allowing "
                       "(infra not ready?)", exc_info=True)
        return []
    finally:
        if own:
            conn.close()
    out = []
    for r in rows:
        try:
            out.append(r["project_id"])
        except (KeyError, TypeError, IndexError):
            out.append(r[0])
    return out


def user_can_access_project(user_id, role, project_id, conn=None):
    """True if the user may access `project_id` under the restriction layer.

    Admins/super_admins always may. Others may unless the project is restricted
    and they're not on its allowlist. Unknown / not-restricted → True. Fails open
    on missing infra (returns True)."""
    if role in ("admin", "super_admin"):
        return True
    own = conn is None
    if own:
        conn = get_conn()
    try:
        row = conn.execute(
            q("""SELECT project_is_restricted FROM projects
                 WHERE project_id = ? AND project_deleted_at IS NULL"""),
            (project_id,),
        ).fetchone()
        if not row:
            return True
        try:
            restricted = bool(row["project_is_restricted"])
        except (KeyError, TypeError, IndexError):
            restricted = bool(row[0])
        if not restricted:
            return True
        granted = conn.execute(
            q("""SELECT 1 FROM project_access
                 WHERE project_id = ? AND user_id = ?"""),
            (project_id, user_id),
        ).fetchone()
        return bool(granted)
    except Exception:
        logger.warning("[project_access] access check failed for project %s; "
                       "allowing (infra not ready?)", project_id, exc_info=True)
        return True
    finally:
        if own:
            conn.close()


def project_hide_clause(hidden_project_ids, column="i.project_id"):
    """Return (sql_fragment, params) excluding restricted projects, mirroring
    location_scope_for_user()'s contract: "" or a bare predicate to AND into the
    WHERE. `column` is the project_id column in the caller's query."""
    if not hidden_project_ids:
        return "", []
    placeholders = ",".join(["?"] * len(hidden_project_ids))
    return f"{column} NOT IN ({placeholders})", list(hidden_project_ids)


def add_project_hide(scope_sql, scope_params, user_id, role, company_id,
                     column="i.project_id"):
    """Fold the restricted-project hide predicate into an existing
    (scope_sql, scope_params) pair (typically from location_scope_for_user).
    Returns a new (sql, params). No-op for admins/super_admins or when nothing is
    hidden — so it's safe to call unconditionally at every interaction read."""
    hide_sql, hide_params = project_hide_clause(
        restricted_project_ids_for_user(user_id, role, company_id), column)
    if not hide_sql:
        return scope_sql, list(scope_params)
    combined = (scope_sql + " AND " + hide_sql) if scope_sql else hide_sql
    return combined, [*scope_params, *hide_params]


def current_user_blocked_from_project(project_id):
    """True if the current request's user is blocked from this restricted
    project. False when there's no request/auth context (trusted server code) or
    the user is allowed. Wrapper for the single-project ownership getters; uses a
    fresh connection so a missing-infra error can't poison a caller transaction."""
    try:
        from flask import has_request_context
        if not has_request_context() or not getattr(current_user, "is_authenticated", False):
            return False
        return not user_can_access_project(
            current_user.user_id, current_user.role, project_id)
    except Exception:
        logger.warning("[project_access] gate errored; allowing", exc_info=True)
        return False


def ai_caller_user_id_for_company(company_id):
    """Return the user_id of this company's AI Caller bot user, or None.

    Convention: a single user_id per company with role='caller' and
    name 'AI Caller'. There is no DB flag distinguishing the bot from
    a human caller today — when this becomes load-bearing (e.g., we
    need to disambiguate from a human named "AI Caller"), add
    `companies.company_ai_caller_user_id` and migrate this lookup.

    Per-request cached on flask.g._ai_caller_cache.
    """
    if not company_id:
        return None

    cache = None
    try:
        from flask import g, has_request_context
        if has_request_context():
            cache = getattr(g, "_ai_caller_cache", None)
            if cache is None:
                cache = {}
                g._ai_caller_cache = cache
            if company_id in cache:
                return cache[company_id]
    except Exception:
        cache = None

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT u.user_id FROM users u
                 JOIN user_roles ur ON ur.user_role_id = u.user_role_id
                 JOIN roles r       ON r.role_id        = ur.role_id
                 JOIN departments d ON d.department_id  = u.department_id
                 WHERE u.user_first_name = 'AI'
                   AND u.user_last_name  = 'Caller'
                   AND r.role_name       = 'caller'
                   AND d.company_id      = ?
                   AND u.user_deleted_at IS NULL
                 LIMIT 1"""),
            (company_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    result = None
    if row is not None:
        try:
            result = row["user_id"]
        except (KeyError, TypeError, IndexError):
            result = row[0]

    if cache is not None:
        cache[company_id] = result
    return result


def validate_caller_user_id_for_user(caller_user_id, user_id, role,
                                      company_id):
    """Enforce caller-attribution scope on a submitted caller_user_id.

    Today's rule (only under ff_permission_filtering, only for managers):
      a manager may attribute a call to themselves or to the company's
      AI Caller bot user — nothing else. Admins / super_admins / callers
      bypass this check. Other tenants (flag off) bypass too.

    Returns (ok: bool, err: str|None). caller_user_id may be None (form
    field omitted) — that's fine for everyone, including managers.
    """
    if caller_user_id is None or caller_user_id == "":
        return True, None
    if not is_feature_enabled(company_id, "ff_permission_filtering", default=False):
        return True, None
    if role != "manager":
        return True, None

    try:
        cid = int(caller_user_id)
    except (TypeError, ValueError):
        return False, "Invalid caller_user_id"

    if cid == int(user_id):
        return True, None

    ai_uid = ai_caller_user_id_for_company(company_id)
    if ai_uid is not None and cid == int(ai_uid):
        return True, None

    return False, ("As a Regional Manager you can only log calls as "
                   "yourself or the AI Caller — pick one of those.")


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
