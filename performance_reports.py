"""
performance_reports.py — Echo Audit V2 Phase 4 per-respondent reports.

Exposes two HTTP routes:
    GET /api/performance-reports                  — list, grouped by location
    GET /api/performance-reports/<id>             — detail

Plus the background job the grading flow fires after every successful grade:
    update_performance_report_async(interaction_id, company_id,
                                    respondent_user_id=None,
                                    respondent_id=None)

Each call contributes to exactly ONE rolling report — either the known-user
report (subject_user_id) or the detected-respondent report (respondent_id,
from the respondents table). The secret-shopping flow uses respondent_id;
the legacy "grade a known user" flow still uses subject_user_id.

The background function:
  - skips if this interaction_id is already tracked in pr_processed_interaction_ids
  - runs Claude to produce / update strengths, weaknesses, coaching
  - updates pr_data, pr_average_score, pr_call_count, pr_processed_interaction_ids
  - checks + increments the anthropic rate limit before the Claude call

All work runs in a daemon thread so the grading request never blocks on it.
"""

import json
import logging
import os
import threading

import anthropic
from flask import Blueprint, jsonify
from flask_login import login_required

from db import IS_POSTGRES, get_conn, q
from helpers import check_rate_limit, get_effective_company_id, increment_usage

logger = logging.getLogger(__name__)

reports_bp = Blueprint("reports", __name__, url_prefix="/api")

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── HTTP helpers ──────────────────────────────────────────────


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
# GET /api/performance-reports   (list, grouped by location)
# ═══════════════════════════════════════════════════════════════
#
# Location derivation for the list view:
#   user → departments (u.department_id = d.department_id)
#     → companies (d.company_id = c.company_id)
#     → locations (l.company_id = c.company_id)  ← MULTIPLE possible
#
# A company can own several locations but a user's "home" location is not
# explicitly modeled in V2. We report the first active location for the
# user's company as a pragmatic grouping key, or NULL when the company has
# no locations at all. This is the same shape V1 used.


@reports_bp.route("/performance-reports", methods=["GET"])
@login_required
def list_performance_reports():
    company_id, err = _require_company()
    if err: return err

    # Two groups of reports per company:
    #   1. Known-user reports — scoped by users.department_id → companies
    #   2. Detected-respondent reports — scoped by respondents.company_id
    # Location source differs:
    #   - known users: first active location for the company (same as V1)
    #   - respondents: the respondent row's own location_id (more accurate)
    nulls_last = "NULLS LAST" if IS_POSTGRES else "IS NULL ASC"

    known_user_sql = f"""
        SELECT
            pr.performance_report_id,
            pr.subject_user_id AS respondent_user_id,
            NULL::int        AS respondent_id,
            (u.user_first_name || ' ' || u.user_last_name) AS respondent_name,
            pr.pr_average_score,
            pr.pr_call_count,
            pr.pr_updated_at,
            (
                SELECT l.location_name FROM locations l
                WHERE l.company_id = d.company_id
                  AND l.location_deleted_at IS NULL
                ORDER BY l.location_id ASC LIMIT 1
            ) AS location_name,
            (
                SELECT l.location_id FROM locations l
                WHERE l.company_id = d.company_id
                  AND l.location_deleted_at IS NULL
                ORDER BY l.location_id ASC LIMIT 1
            ) AS location_id,
            NULL::int AS project_id
        FROM performance_reports pr
        JOIN users       u ON u.user_id       = pr.subject_user_id
        JOIN departments d ON d.department_id = u.department_id
        WHERE pr.subject_user_id IS NOT NULL
          AND d.company_id = ?
          AND u.user_deleted_at IS NULL
    """ if IS_POSTGRES else """
        SELECT
            pr.performance_report_id,
            pr.subject_user_id AS respondent_user_id,
            NULL               AS respondent_id,
            (u.user_first_name || ' ' || u.user_last_name) AS respondent_name,
            pr.pr_average_score,
            pr.pr_call_count,
            pr.pr_updated_at,
            (
                SELECT l.location_name FROM locations l
                WHERE l.company_id = d.company_id
                  AND l.location_deleted_at IS NULL
                ORDER BY l.location_id ASC LIMIT 1
            ) AS location_name,
            (
                SELECT l.location_id FROM locations l
                WHERE l.company_id = d.company_id
                  AND l.location_deleted_at IS NULL
                ORDER BY l.location_id ASC LIMIT 1
            ) AS location_id,
            NULL AS project_id
        FROM performance_reports pr
        JOIN users       u ON u.user_id       = pr.subject_user_id
        JOIN departments d ON d.department_id = u.department_id
        WHERE pr.subject_user_id IS NOT NULL
          AND d.company_id = ?
          AND u.user_deleted_at IS NULL
    """

    respondent_sql = """
        SELECT
            pr.performance_report_id,
            NULL                 AS respondent_user_id,
            r.respondent_id      AS respondent_id,
            r.respondent_name    AS respondent_name,
            pr.pr_average_score,
            pr.pr_call_count,
            pr.pr_updated_at,
            l.location_name      AS location_name,
            l.location_id        AS location_id,
            (
                SELECT i.project_id FROM interactions i
                WHERE i.respondent_id = pr.respondent_id
                  AND i.interaction_deleted_at IS NULL
                ORDER BY i.interaction_date DESC, i.interaction_id DESC
                LIMIT 1
            ) AS project_id
        FROM performance_reports pr
        JOIN respondents r ON r.respondent_id = pr.respondent_id
        LEFT JOIN locations l ON l.location_id = r.location_id
        WHERE pr.respondent_id IS NOT NULL
          AND r.company_id = ?
    """

    union_sql = f"""
        {known_user_sql}
        UNION ALL
        {respondent_sql}
        ORDER BY location_name {nulls_last}, respondent_name
    """

    conn = get_conn()
    try:
        cur = conn.execute(q(union_sql), (company_id, company_id))
        rows = _rows(cur)
        # Group the flat list into {location_name: [reports]}
        grouped = {}
        for r in rows:
            key = r.get("location_name") or "(no location)"
            grouped.setdefault(key, []).append(r)
        return jsonify({"groups": grouped, "reports": rows})
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# GET /api/performance-reports/<id>  (detail)
# ═══════════════════════════════════════════════════════════════


@reports_bp.route("/performance-reports/<int:performance_report_id>", methods=["GET"])
@login_required
def get_performance_report(performance_report_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        # Tenant scope is satisfied by either branch: known-user reports via
        # users.department_id → companies, respondent reports via respondents.company_id.
        cur = conn.execute(
            q("""SELECT
                    pr.*,
                    COALESCE(
                        r.respondent_name,
                        NULLIF(TRIM(u.user_first_name || ' ' || u.user_last_name), '')
                    ) AS respondent_name
                 FROM performance_reports pr
                 LEFT JOIN users       u ON u.user_id       = pr.subject_user_id
                 LEFT JOIN departments d ON d.department_id = u.department_id
                 LEFT JOIN respondents r ON r.respondent_id = pr.respondent_id
                 WHERE pr.performance_report_id = ?
                   AND (
                        (pr.subject_user_id IS NOT NULL AND d.company_id = ?)
                     OR (pr.respondent_id   IS NOT NULL AND r.company_id = ?)
                   )"""),
            (performance_report_id, company_id, company_id),
        )
        row = _row_to_dict(cur.fetchone())
        if not row:
            return _err("Performance report not found", 404)

        # Materialize JSON fields for SQLite (psycopg2 auto-decodes JSONB).
        for key in ("pr_data", "pr_processed_interaction_ids"):
            val = row.get(key)
            if isinstance(val, str):
                try:
                    row[key] = json.loads(val)
                except Exception:
                    pass

        # Fetch summary of processed interactions for context in the UI.
        ids = row.get("pr_processed_interaction_ids") or []
        if isinstance(ids, list) and ids:
            placeholders = ",".join(["?"] * len(ids))
            cur = conn.execute(
                q(f"""SELECT interaction_id, interaction_date,
                             interaction_call_start_time,
                             interaction_call_duration_seconds,
                             interaction_uploaded_at,
                             interaction_overall_score, interaction_flags,
                             interaction_strengths, interaction_weaknesses,
                             interaction_overall_assessment
                      FROM interactions
                      WHERE interaction_id IN ({placeholders})
                        AND interaction_deleted_at IS NULL
                      ORDER BY interaction_date DESC, interaction_id DESC"""),
                ids,
            )
            row["interactions"] = _rows(cur)
        else:
            row["interactions"] = []

        return jsonify(row)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Background update
# ═══════════════════════════════════════════════════════════════


def update_performance_report_async(interaction_id, company_id,
                                    respondent_user_id=None,
                                    respondent_id=None):
    """Kick off _update_performance_report in a daemon thread. Never raises.

    Called from the grading flow routes after a successful grade. Returns
    immediately so the HTTP request is not blocked on Claude latency.

    Pass respondent_id for detected-respondent (secret-shopping) reports, or
    respondent_user_id for known-user reports. If both are passed, respondent_id
    wins — detected respondents are the primary V2 subject.
    """
    if not interaction_id:
        return
    if not respondent_id and not respondent_user_id:
        return
    t = threading.Thread(
        target=_update_performance_report_safe,
        args=(interaction_id, company_id, respondent_user_id, respondent_id),
        daemon=True,
    )
    t.start()


def _update_performance_report_safe(interaction_id, company_id,
                                    respondent_user_id, respondent_id):
    try:
        update_performance_report(interaction_id, company_id,
                                  respondent_user_id=respondent_user_id,
                                  respondent_id=respondent_id)
    except Exception:
        logger.exception(
            "Background performance report update failed "
            "(interaction=%s user=%s respondent=%s)",
            interaction_id, respondent_user_id, respondent_id,
        )


def update_performance_report(interaction_id, company_id,
                              respondent_user_id=None, respondent_id=None):
    """Synchronous version of the update. Runs in the caller's thread.

    Exactly one of respondent_id / respondent_user_id determines the subject.
    respondent_id wins if both are provided. Skips if the interaction is
    already tracked in pr_processed_interaction_ids.
    """
    use_respondent = respondent_id is not None
    if not use_respondent and not respondent_user_id:
        return
    subject_label = (
        f"respondent #{respondent_id}" if use_respondent
        else f"user #{respondent_user_id}"
    )

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT interaction_id, interaction_date, interaction_overall_score,
                        interaction_strengths, interaction_weaknesses,
                        interaction_overall_assessment, interaction_flags
                 FROM interactions
                 WHERE interaction_id = ? AND interaction_deleted_at IS NULL"""),
            (interaction_id,),
        )
        interaction = _row_to_dict(cur.fetchone())
        if not interaction:
            return

        if use_respondent:
            cur = conn.execute(
                q("""SELECT * FROM performance_reports WHERE respondent_id = ?"""),
                (respondent_id,),
            )
        else:
            cur = conn.execute(
                q("""SELECT * FROM performance_reports WHERE subject_user_id = ?"""),
                (respondent_user_id,),
            )
        existing = _row_to_dict(cur.fetchone())
    finally:
        conn.close()

    existing_ids = []
    existing_data = {}
    if existing:
        raw_ids = existing.get("pr_processed_interaction_ids")
        if isinstance(raw_ids, str):
            try:
                existing_ids = json.loads(raw_ids)
            except Exception:
                existing_ids = []
        elif isinstance(raw_ids, list):
            existing_ids = raw_ids

        raw_data = existing.get("pr_data")
        if isinstance(raw_data, str):
            try:
                existing_data = json.loads(raw_data)
            except Exception:
                existing_data = {}
        elif isinstance(raw_data, dict):
            existing_data = raw_data

        if interaction_id in existing_ids:
            return  # already processed

    ok, _msg = check_rate_limit(company_id, "anthropic")
    if not ok:
        logger.warning(
            "Skipping performance report update for %s — anthropic rate limit hit",
            subject_label,
        )
        return

    call_summary = (
        f"Date: {interaction.get('interaction_date')} | "
        f"Score: {interaction.get('interaction_overall_score')}/10\n"
        f"Strengths: {interaction.get('interaction_strengths') or ''}\n"
        f"Weaknesses: {interaction.get('interaction_weaknesses') or ''}\n"
        f"Assessment: {interaction.get('interaction_overall_assessment') or ''}\n"
        f"Flags: {interaction.get('interaction_flags') or ''}"
    )

    if existing:
        prompt = (
            f"Here is the existing compiled performance report for {subject_label}:\n\n"
            f"CONSISTENT STRENGTHS:\n{existing_data.get('strengths', '')}\n\n"
            f"AREAS FOR IMPROVEMENT:\n{existing_data.get('weaknesses', '')}\n\n"
            f"COACHING RECOMMENDATIONS:\n{existing_data.get('coaching', '')}\n\n"
            f"A new call was just graded. Summary:\n{call_summary}\n\n"
            "Update the report incrementally. Keep prior insights that still apply; "
            "add new themes where the new call reveals them. Do NOT regenerate from "
            "scratch. Respond with valid JSON only, no extra prose:\n"
            "{\n"
            '  "strengths": "• bullet\\n• bullet",\n'
            '  "weaknesses": "• bullet\\n• bullet",\n'
            '  "coaching": "• bullet\\n• bullet"\n'
            "}"
        )
    else:
        prompt = (
            f"Build the first performance report for {subject_label} from "
            f"this single graded call:\n\n{call_summary}\n\n"
            "Respond with valid JSON only, no extra prose:\n"
            "{\n"
            '  "strengths": "• bullet\\n• bullet",\n'
            '  "weaknesses": "• bullet\\n• bullet",\n'
            '  "coaching": "• bullet\\n• bullet"\n'
            "}"
        )

    try:
        resp = _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            timeout=90.0,
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw.strip())
    except Exception:
        logger.exception("Performance report Claude call failed")
        return

    increment_usage(company_id, "anthropic")

    new_ids = list(existing_ids) + [interaction_id]
    new_call_count = (existing["pr_call_count"] if existing else 0) + 1

    # Recompute pr_average_score from the full set of processed interactions
    # rather than trusting the incoming score in isolation.
    conn = get_conn()
    try:
        placeholders = ",".join(["?"] * len(new_ids))
        cur = conn.execute(
            q(f"""SELECT AVG(interaction_overall_score) AS avg_score
                  FROM interactions
                  WHERE interaction_id IN ({placeholders})
                    AND interaction_overall_score IS NOT NULL
                    AND interaction_deleted_at IS NULL"""),
            new_ids,
        )
        avg_row = _row_to_dict(cur.fetchone()) or {}
        avg_raw = avg_row.get("avg_score")
        avg_score = round(float(avg_raw), 2) if avg_raw is not None else None

        pr_data = dict(existing_data)
        pr_data.update({
            "strengths":  parsed.get("strengths", "") or pr_data.get("strengths", ""),
            "weaknesses": parsed.get("weaknesses", "") or pr_data.get("weaknesses", ""),
            "coaching":   parsed.get("coaching", "") or pr_data.get("coaching", ""),
        })
        pr_data_json = json.dumps(pr_data)
        ids_json = json.dumps(new_ids)

        if existing:
            if IS_POSTGRES:
                conn.execute(
                    """UPDATE performance_reports SET
                           pr_data = %s::jsonb,
                           pr_average_score = %s,
                           pr_call_count = %s,
                           pr_processed_interaction_ids = %s::jsonb
                       WHERE performance_report_id = %s""",
                    (pr_data_json, avg_score, new_call_count, ids_json,
                     existing["performance_report_id"]),
                )
            else:
                conn.execute(
                    """UPDATE performance_reports SET
                           pr_data = ?,
                           pr_average_score = ?,
                           pr_call_count = ?,
                           pr_processed_interaction_ids = ?
                       WHERE performance_report_id = ?""",
                    (pr_data_json, avg_score, new_call_count, ids_json,
                     existing["performance_report_id"]),
                )
        else:
            # Exactly one of subject_user_id / respondent_id is populated.
            subj_user = None if use_respondent else respondent_user_id
            subj_resp = respondent_id if use_respondent else None
            if IS_POSTGRES:
                conn.execute(
                    """INSERT INTO performance_reports
                           (subject_user_id, respondent_id, pr_data,
                            pr_average_score, pr_call_count,
                            pr_processed_interaction_ids)
                       VALUES (%s, %s, %s::jsonb, %s, %s, %s::jsonb)""",
                    (subj_user, subj_resp, pr_data_json, avg_score,
                     new_call_count, ids_json),
                )
            else:
                conn.execute(
                    """INSERT INTO performance_reports
                           (subject_user_id, respondent_id, pr_data,
                            pr_average_score, pr_call_count,
                            pr_processed_interaction_ids)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (subj_user, subj_resp, pr_data_json, avg_score,
                     new_call_count, ids_json),
                )

        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Persisting performance report failed")
        raise
    finally:
        conn.close()
