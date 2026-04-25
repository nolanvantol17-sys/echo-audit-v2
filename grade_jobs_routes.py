"""
grade_jobs_routes.py — HTTP endpoints for the async grading queue.

Routes:
    POST   /api/grade-jobs                — enqueue a call for background grading
    GET    /api/grade-jobs                — list current user's active queue
    POST   /api/grade-jobs/<id>/dismiss   — soft-hide a job from the UI

POST mirrors the multipart form contract of /api/grade today (audio file +
project_id + location_id + optional caller/date/campaign/timestamps), but
returns immediately with {grade_job_id, interaction_id} after persisting
the audio + spawning the daemon thread.

GET is user-scoped (per Q4): returns jobs submitted by current_user, joined
with their interaction's status + score so the queue UI can render
phase + final state in one call. Excludes dismissed rows.

DELETE/dismiss is author-OR-admin gated (mirrors location_notes pattern).
"""

import logging
from datetime import date
from pathlib import Path

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import grader
from db import IS_POSTGRES, get_conn, q
from grade_jobs import enqueue_grade_job, process_grade_job_async
from helpers import check_rate_limit, get_effective_company_id
from interactions_routes import (
    _campaign_belongs_to_project,
    _get_project_in_company,
)

logger = logging.getLogger(__name__)

grade_jobs_bp = Blueprint("grade_jobs", __name__, url_prefix="/api")


# ── Local helpers ──


def _err(msg, code):
    return jsonify({"error": msg}), code


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _rows(cur):
    return [_row_to_dict(r) for r in cur.fetchall()]


def _require_company():
    cid = get_effective_company_id()
    if cid is None:
        return None, _err(
            "No company context. Super admins must select an organization first.",
            400,
        )
    return cid, None


def _parse_date(s, default):
    if not s:
        return default
    try:
        return date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return default


# ── POST /api/grade-jobs ──


@grade_jobs_bp.route("/grade-jobs", methods=["POST"])
@login_required
def create_grade_job():
    company_id, err = _require_company()
    if err: return err

    audio_file = request.files.get("audio")
    if not audio_file or not audio_file.filename:
        return _err("Missing audio file", 400)

    ext = Path(audio_file.filename).suffix.lower()
    if ext not in grader.AUDIO_EXTENSIONS:
        return _err(f"Unsupported audio format: {ext or '(none)'}", 400)

    try:
        project_id = int(request.form.get("project_id") or 0)
    except (TypeError, ValueError):
        return _err("Invalid project_id", 400)
    if not project_id:
        return _err("Missing project_id", 400)

    try:
        location_id = int(request.form.get("location_id") or 0)
    except (TypeError, ValueError):
        return _err("Invalid location_id", 400)
    if not location_id:
        return _err("Missing location_id", 400)

    caller_user_id     = request.form.get("caller_user_id") or None
    respondent_user_id = request.form.get("respondent_user_id") or None
    interaction_date   = _parse_date(request.form.get("interaction_date"), date.today())

    raw_campaign = request.form.get("campaign_id")
    campaign_id = None
    if raw_campaign not in (None, ""):
        try:
            campaign_id = int(raw_campaign)
        except (TypeError, ValueError):
            return _err("Invalid campaign_id", 400)

    call_start_time       = (request.form.get("call_start_time") or "").strip() or None
    call_end_time         = (request.form.get("call_end_time")   or "").strip() or None
    call_duration_seconds = request.form.get("call_duration_seconds")
    try:
        call_duration_seconds = int(call_duration_seconds) if call_duration_seconds else None
    except (TypeError, ValueError):
        call_duration_seconds = None

    # Rate-limit gates — fail fast before queuing.
    ok, msg = check_rate_limit(company_id, "assemblyai")
    if not ok: return _err(msg, 429)
    ok, msg = check_rate_limit(company_id, "anthropic")
    if not ok: return _err(msg, 429)

    # Tenant-verify project + location + campaign before queuing.
    conn = get_conn()
    try:
        project = _get_project_in_company(conn, project_id, company_id)
        if not project:
            return _err("Project not found", 404)
        loc_row = conn.execute(
            q("""SELECT 1 FROM locations
                 WHERE location_id = ? AND company_id = ?
                   AND location_deleted_at IS NULL"""),
            (location_id, company_id),
        ).fetchone()
        if not loc_row:
            return _err("Invalid location_id for this company", 400)
        if campaign_id is not None and not _campaign_belongs_to_project(
            conn, campaign_id, project_id
        ):
            return _err("Invalid campaign_id for this project", 400)
    finally:
        conn.close()

    # Read the upload bytes now (before the request closes).
    audio_bytes = audio_file.read()
    if not audio_bytes:
        return _err("Empty audio file", 400)

    actor_user_id = current_user.user_id

    job_id, interaction_id = enqueue_grade_job(
        company_id=company_id,
        submitted_by_user_id=actor_user_id,
        project_id=project_id,
        location_id=location_id,
        audio_bytes=audio_bytes,
        audio_ext=ext,
        caller_user_id=caller_user_id,
        respondent_user_id=respondent_user_id,
        interaction_date=interaction_date,
        campaign_id=campaign_id,
        call_start_time=call_start_time,
        call_end_time=call_end_time,
        call_duration_seconds=call_duration_seconds,
    )

    process_grade_job_async(job_id, actor_user_id)

    return jsonify({
        "grade_job_id":   job_id,
        "interaction_id": interaction_id,
    }), 202


# ── GET /api/grade-jobs ──


@grade_jobs_bp.route("/grade-jobs", methods=["GET"])
@login_required
def list_grade_jobs():
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT j.grade_job_id,
                        j.gj_status,
                        j.gj_phase_started_at,
                        j.gj_error,
                        j.gj_created_at,
                        j.interaction_id,
                        i.status_id,
                        i.interaction_overall_score,
                        i.interaction_responder_name,
                        i.project_id,
                        p.project_name
                   FROM grade_jobs j
                   LEFT JOIN interactions i ON i.interaction_id = j.interaction_id
                   LEFT JOIN projects p     ON p.project_id     = i.project_id
                  WHERE j.company_id = ?
                    AND j.submitted_by_user_id = ?
                    AND j.gj_dismissed_at IS NULL
                  ORDER BY j.gj_created_at DESC, j.grade_job_id DESC"""),
            (company_id, current_user.user_id),
        )
        return jsonify(_rows(cur))
    finally:
        conn.close()


# ── POST /api/grade-jobs/<id>/dismiss ──


@grade_jobs_bp.route("/grade-jobs/<int:job_id>/dismiss", methods=["POST"])
@login_required
def dismiss_grade_job(job_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT grade_job_id, submitted_by_user_id, company_id, gj_dismissed_at
                   FROM grade_jobs
                  WHERE grade_job_id = ?"""),
            (job_id,),
        )
        row = _row_to_dict(cur.fetchone())
        if not row or row["company_id"] != company_id:
            return _err("Grade job not found", 404)
        if row["gj_dismissed_at"] is not None:
            return jsonify({"ok": True, "grade_job_id": job_id})

        is_author = row["submitted_by_user_id"] == current_user.user_id
        is_admin  = current_user.role in ("admin", "super_admin")
        if not (is_author or is_admin):
            return _err("Forbidden", 403)

        try:
            if IS_POSTGRES:
                conn.execute(
                    "UPDATE grade_jobs SET gj_dismissed_at = NOW() "
                    "WHERE grade_job_id = %s",
                    (job_id,),
                )
            else:
                conn.execute(
                    "UPDATE grade_jobs SET gj_dismissed_at = CURRENT_TIMESTAMP "
                    "WHERE grade_job_id = ?",
                    (job_id,),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return jsonify({"ok": True, "grade_job_id": job_id})
    finally:
        conn.close()
