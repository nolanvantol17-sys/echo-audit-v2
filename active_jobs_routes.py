"""
active_jobs_routes.py — Unified persistent-dock feed.

GET /api/active-jobs returns the current user's in-flight + recently-
completed work across two underlying tables (grade_jobs + scheduled_calls)
in one merged shape. Powers the persistent dock in base.html.

Active = non-terminal status. Recently-completed = terminal AND updated
within ACTIVE_WINDOW_HOURS (24 by default). Older terminal rows naturally
fall off the dock via the time window — no explicit dismiss step needed
for AI Shop rows (scheduled_calls has no dismissed_at column today).

Tenancy: each branch enforces (company_id, requesting_user_id) in its
WHERE clause. The UNION never crosses tenant boundaries. Auth via
@login_required.
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from audit_log import (ACTION_DISMISSED, ENTITY_GRADE_JOB,
                       ENTITY_SCHEDULED_CALL, write_audit_log)
from db import IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id

logger = logging.getLogger(__name__)

active_jobs_bp = Blueprint("active_jobs", __name__, url_prefix="/api")

# How long to surface terminal (graded/failed/no_answer/timeout) rows in
# the dock after they complete. Longer = more click-through opportunity;
# shorter = less clutter. 24h is the lazy first cut.
ACTIVE_WINDOW_HOURS = 24

# Schedule_AI_Shop status polling endpoint uses 600s as the timeout cap.
# Mirror that here so a user staring at the dock sees the same terminal
# moment they'd see on the dedicated polling endpoint.
TIMEOUT_MINUTES = 10

# Cap the response. The dock UI doesn't render 1000 rows; if a user has
# more than 50 in-flight + recently-completed, something else is wrong.
MAX_ROWS = 50


# ── Helpers ─────────────────────────────────────────────────────


def _err(msg, code):
    return jsonify({"error": msg}), code


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _to_utc_datetime(v):
    """Coerce a DB-returned timestamp value to an aware UTC datetime.
    SQLite returns TEXT; Postgres returns aware datetimes. None passthrough."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


def _iso(v):
    """ISO-format a DB timestamp tolerantly. Returns None for None."""
    dt = _to_utc_datetime(v)
    return dt.isoformat() if dt else None


def _display_status_grade_job(raw_status):
    """grade_jobs gj_status → unified display_status enum."""
    if raw_status == "queued":                     return "queued"
    if raw_status in ("transcribing", "grading"):  return "in_progress"
    if raw_status == "graded":                     return "graded"
    if raw_status == "failed":                     return "failed"
    return raw_status  # defensive — chk_gj_status should prevent


def _display_status_scheduled_call(raw_status, created_at):
    """scheduled_calls sc_status → unified display_status enum.

    'initiated' splits into 'in_progress' or 'timeout' based on age. Mirrors
    the derivation in scheduled_calls_routes.schedule_ai_shop_status, just
    coarser-grained — the dock doesn't need webhook_received vs processing
    granularity (click the row for the dedicated polling endpoint if needed).
    """
    if raw_status == "graded":     return "graded"
    if raw_status == "no_answer":  return "no_answer"
    if raw_status == "failed":     return "failed"
    if raw_status == "initiated":
        dt = _to_utc_datetime(created_at)
        if dt is not None:
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
            if age_min > TIMEOUT_MINUTES:
                return "timeout"
        return "in_progress"
    return raw_status  # defensive


def _display_title(source, location_name):
    """Compose the dock-card primary label."""
    loc = location_name or "unknown location"
    if source == "grade_job":
        # No grade_jobs column distinguishes record vs upload today —
        # neutral label. Add a gj_source column if/when Mayfair asks
        # for the distinction (see followup_grade_jobs_source_column).
        return f"Submission at {loc}"
    return f"AI Shop at {loc}"


# ── Core query (also called from app.py context processor for first-paint) ──


def get_active_jobs_for_user(company_id, user_id):
    """Return the unified active-jobs list for a (company_id, user_id).

    Used by both:
      - GET /api/active-jobs (route handler below)
      - app.py context processor (server-side initial paint for the dock)

    Returns a list of dicts in the public response shape. Returns [] if
    company_id or user_id is None — safe to call before auth resolution
    completes.
    """
    if company_id is None or user_id is None:
        return []

    # Inline stuck-job sweep — every dock poll generalizes the at-boot
    # recovery so jobs whose daemon thread died (OOM, exception in a
    # non-instrumented branch, etc.) flip to 'failed' instead of lingering
    # in the dock indefinitely. Per-tenant scope; cheap (3 small UPDATEs
    # filtered by partial index idx_grade_jobs_company_status).
    from db import sweep_stuck_grade_jobs
    sweep_stuck_grade_jobs(company_id=company_id)

    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=ACTIVE_WINDOW_HOURS)
    ).isoformat()

    sql = """
        SELECT
            'grade_job'                  AS source,
            j.grade_job_id               AS id,
            j.gj_status                  AS raw_status,
            CAST(NULL AS TEXT)           AS sc_conversation_id,
            CAST(NULL AS TEXT)           AS sc_phone_number,
            j.gj_error                   AS error_text,
            j.interaction_id             AS interaction_id,
            i.interaction_overall_score  AS interaction_overall_score,
            j.gj_created_at              AS created_at,
            j.gj_updated_at              AS updated_at,
            p.project_name               AS project_name,
            l.location_name              AS location_name
        FROM grade_jobs j
        LEFT JOIN interactions i ON i.interaction_id = j.interaction_id
        LEFT JOIN projects     p ON p.project_id     = i.project_id
        LEFT JOIN locations    l ON l.location_id    = i.interaction_location_id
        WHERE j.company_id = ?
          AND j.submitted_by_user_id = ?
          AND j.gj_dismissed_at IS NULL
          AND (
            j.gj_status NOT IN ('graded', 'failed')
            OR j.gj_updated_at > ?
          )

        UNION ALL

        SELECT
            'scheduled_call'             AS source,
            sc.sc_id                     AS id,
            sc.sc_status                 AS raw_status,
            sc.sc_conversation_id        AS sc_conversation_id,
            sc.sc_phone_number           AS sc_phone_number,
            sc.sc_status_message         AS error_text,
            i.interaction_id             AS interaction_id,
            i.interaction_overall_score  AS interaction_overall_score,
            sc.sc_requested_at           AS created_at,
            COALESCE(sc.sc_completed_at, sc.sc_requested_at) AS updated_at,
            p.project_name               AS project_name,
            l.location_name              AS location_name
        FROM scheduled_calls sc
        JOIN locations l ON l.location_id = sc.sc_location_id
        LEFT JOIN projects        p   ON p.project_id            = sc.sc_project_id
        LEFT JOIN voip_call_queue vcq ON vcq.voip_queue_call_id  = sc.sc_conversation_id
        LEFT JOIN interactions    i   ON i.interaction_id        = vcq.voip_queue_interaction_id
        WHERE l.company_id = ?
          AND sc.sc_requested_by_user_id = ?
          AND sc.sc_dismissed_at IS NULL
          AND (
            sc.sc_status = 'initiated'
            OR COALESCE(sc.sc_completed_at, sc.sc_requested_at) > ?
          )

        ORDER BY updated_at DESC
        LIMIT ?
    """

    params = (
        company_id, user_id, cutoff_iso,    # grade_jobs branch
        company_id, user_id, cutoff_iso,    # scheduled_calls branch
        MAX_ROWS,
    )

    conn = get_conn()
    try:
        cur = conn.execute(q(sql), params)
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    results = []
    for r in rows:
        source     = r["source"]
        raw_status = r["raw_status"]
        created    = r.get("created_at")

        if source == "grade_job":
            display_status = _display_status_grade_job(raw_status)
        else:
            display_status = _display_status_scheduled_call(raw_status, created)

        score = r.get("interaction_overall_score")
        score = float(score) if score is not None else None

        results.append({
            "source":         source,
            "id":             r["id"],
            "display_status": display_status,
            "display_title":  _display_title(source, r.get("location_name")),
            "interaction_id": r.get("interaction_id"),
            "interaction_overall_score": score,
            "created_at":     _iso(created),
            "updated_at":     _iso(r.get("updated_at")),
            "meta": {
                "project_name":    r.get("project_name"),
                "location_name":   r.get("location_name"),
                "error":           r.get("error_text"),
                "conversation_id": r.get("sc_conversation_id"),
                "phone_number":    r.get("sc_phone_number"),
                "raw_status":      raw_status,
            },
        })

    return results


# ── GET /api/active-jobs ─────────────────────────────────────────


@active_jobs_bp.route("/active-jobs", methods=["GET"])
@login_required
def list_active_jobs():
    company_id = get_effective_company_id()
    if company_id is None:
        return _err("No company context", 400)
    return jsonify(get_active_jobs_for_user(company_id, current_user.user_id))


# ── POST /api/active-jobs/dismiss ────────────────────────────────


_TERMINAL_DISPLAY_STATUSES = {"graded", "no_answer", "failed", "timeout"}


@active_jobs_bp.route("/active-jobs/dismiss", methods=["POST"])
@login_required
def dismiss_active_job():
    """Soft-hide a single dock row. Body: {source, id}.

    source must be 'grade_job' or 'scheduled_call'. Tenant + author-or-admin
    gated. Rejects non-terminal rows with 409. Idempotent: returns ok if the
    row was already dismissed.
    """
    company_id = get_effective_company_id()
    if company_id is None:
        return _err("No company context", 400)

    body = request.get_json(silent=True) or {}
    source = body.get("source")
    raw_id = body.get("id")
    if source not in ("grade_job", "scheduled_call"):
        return _err("Invalid source", 400)
    try:
        rid = int(raw_id)
    except (TypeError, ValueError):
        return _err("Invalid id", 400)

    user_id  = current_user.user_id
    is_admin = current_user.role in ("admin", "super_admin")
    now_expr = "NOW()" if IS_POSTGRES else "CURRENT_TIMESTAMP"

    conn = get_conn()
    try:
        if source == "grade_job":
            cur = conn.execute(
                q("""SELECT grade_job_id, submitted_by_user_id, company_id,
                            gj_dismissed_at, gj_status
                       FROM grade_jobs WHERE grade_job_id = ?"""),
                (rid,),
            )
            row = _row_to_dict(cur.fetchone())
            if not row or row["company_id"] != company_id:
                return _err("Not found", 404)
            if not (row["submitted_by_user_id"] == user_id or is_admin):
                return _err("Forbidden", 403)
            if row["gj_dismissed_at"] is None:
                if row["gj_status"] not in ("graded", "failed"):
                    return _err("Row is not in a terminal status", 409)
                conn.execute(
                    q(f"UPDATE grade_jobs SET gj_dismissed_at = {now_expr} "
                      f"WHERE grade_job_id = ?"),
                    (rid,),
                )
                conn.commit()
            entity_type = ENTITY_GRADE_JOB
        else:  # scheduled_call
            cur = conn.execute(
                q("""SELECT sc.sc_id, sc.sc_requested_by_user_id, l.company_id,
                            sc.sc_dismissed_at, sc.sc_status, sc.sc_requested_at
                       FROM scheduled_calls sc
                       JOIN locations l ON l.location_id = sc.sc_location_id
                      WHERE sc.sc_id = ?"""),
                (rid,),
            )
            row = _row_to_dict(cur.fetchone())
            if not row or row["company_id"] != company_id:
                return _err("Not found", 404)
            if not (row["sc_requested_by_user_id"] == user_id or is_admin):
                return _err("Forbidden", 403)
            if row["sc_dismissed_at"] is None:
                display = _display_status_scheduled_call(
                    row["sc_status"], row["sc_requested_at"]
                )
                if display not in _TERMINAL_DISPLAY_STATUSES:
                    return _err("Row is not in a terminal status", 409)
                conn.execute(
                    q(f"UPDATE scheduled_calls SET sc_dismissed_at = {now_expr} "
                      f"WHERE sc_id = ?"),
                    (rid,),
                )
                conn.commit()
            entity_type = ENTITY_SCHEDULED_CALL
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    write_audit_log(user_id, ACTION_DISMISSED, entity_type, rid)
    return jsonify({"ok": True})


# ── POST /api/active-jobs/dismiss-all ────────────────────────────


@active_jobs_bp.route("/active-jobs/dismiss-all", methods=["POST"])
@login_required
def dismiss_all_active_jobs():
    """Soft-hide every terminal-display dock row currently visible to the user.

    Single-shot bulk dismiss for the panel-header 'Clear all' button. Same
    tenant + user scoping as GET /api/active-jobs.
    """
    company_id = get_effective_company_id()
    if company_id is None:
        return _err("No company context", 400)

    user_id = current_user.user_id
    rows    = get_active_jobs_for_user(company_id, user_id)
    gj_ids  = [r["id"] for r in rows
                if r["source"] == "grade_job"
                and r["display_status"] in _TERMINAL_DISPLAY_STATUSES]
    sc_ids  = [r["id"] for r in rows
                if r["source"] == "scheduled_call"
                and r["display_status"] in _TERMINAL_DISPLAY_STATUSES]

    if not gj_ids and not sc_ids:
        return jsonify({"ok": True, "dismissed_count": 0})

    now_expr = "NOW()" if IS_POSTGRES else "CURRENT_TIMESTAMP"
    conn = get_conn()
    try:
        if gj_ids:
            ph = ",".join(["?"] * len(gj_ids))
            conn.execute(
                q(f"UPDATE grade_jobs SET gj_dismissed_at = {now_expr} "
                  f"WHERE grade_job_id IN ({ph}) AND gj_dismissed_at IS NULL"),
                tuple(gj_ids),
            )
        if sc_ids:
            ph = ",".join(["?"] * len(sc_ids))
            conn.execute(
                q(f"UPDATE scheduled_calls SET sc_dismissed_at = {now_expr} "
                  f"WHERE sc_id IN ({ph}) AND sc_dismissed_at IS NULL"),
                tuple(sc_ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    write_audit_log(
        user_id, ACTION_DISMISSED, None, None,
        metadata={"bulk": True,
                  "grade_job_ids":      gj_ids,
                  "scheduled_call_ids": sc_ids},
    )
    return jsonify({"ok": True,
                    "dismissed_count": len(gj_ids) + len(sc_ids)})
