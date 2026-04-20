"""
interactions_routes.py — Echo Audit V2 Phase 3 API routes.

Scope: the grading flow. Submit a call, transcribe, grade with Claude, store
the result, serve the audio back, regrade with clarifying-question answers or
reviewer context, log no-answer calls, and soft-delete.

All routes emit JSON. Every interaction is tenant-scoped through the project
chain: interaction → project → projects.company_id.

The heavy AI/transcription work lives in grader.py (pure functions, no DB).
All persistence happens here.
"""

import io
import json
import logging
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file
from flask_login import current_user, login_required

import grader
from audit_log import (
    ACTION_DELETED, ACTION_GRADED, ACTION_REGRADED, ACTION_SUBMITTED,
    ENTITY_INTERACTION, write_audit_log,
)
from auth import role_required
from db import IS_POSTGRES, get_conn, q
from helpers import (
    check_rate_limit,
    get_effective_company_id,
    increment_usage,
    load_active_hints,
)
from intel import compute_location_intel_async
from performance_reports import update_performance_report_async

logger = logging.getLogger(__name__)

interactions_bp = Blueprint("interactions", __name__, url_prefix="/api")


# Filesystem root for audio storage on SQLite. Not used under PostgreSQL
# (audio lives in the interaction_audio_data BYTEA column instead).
_AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", "./audio_uploads")).resolve()

# Status IDs — must match statuses seed in db.py.
STATUS_TRANSCRIBING           = 40
STATUS_AWAITING_CLARIFICATION = 41
STATUS_GRADING                = 42
STATUS_GRADED                 = 43
STATUS_NO_ANSWER              = 44
STATUS_SUBMITTED              = 45


# ── Response helpers ────────────────────────────────────────────


def _err(msg, code):
    return jsonify({"error": msg}), code


def _body():
    return request.get_json(silent=True) or {}


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


def _parse_date(val, default=None):
    if not val:
        return default
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
    except ValueError:
        return default


# ── Ownership helpers ──────────────────────────────────────────


def _get_project_in_company(conn, project_id, company_id):
    """Return project row if it belongs to this company (soft-delete aware)."""
    cur = conn.execute(
        q("""SELECT * FROM projects
             WHERE project_id = ? AND company_id = ? AND project_deleted_at IS NULL"""),
        (project_id, company_id),
    )
    return cur.fetchone()


def _get_interaction_in_company(conn, interaction_id, company_id):
    """Return interaction row if its project belongs to this company.

    Includes soft-deleted interactions for admin recovery paths; callers that
    want to exclude them must check interaction_deleted_at themselves.
    """
    cur = conn.execute(
        q("""SELECT i.* FROM interactions i
             JOIN projects p ON p.project_id = i.project_id
             WHERE i.interaction_id = ? AND p.company_id = ?"""),
        (interaction_id, company_id),
    )
    return cur.fetchone()


def _load_rubric_items(conn, rubric_group_id):
    """Fetch active rubric_items for a group, ordered. Returns list of dicts."""
    cur = conn.execute(
        q("""SELECT rubric_item_id, ri_name, ri_score_type, ri_weight,
                    ri_scoring_guidance, ri_order
             FROM rubric_items
             WHERE rubric_group_id = ? AND ri_deleted_at IS NULL
             ORDER BY ri_order ASC, rubric_item_id ASC"""),
        (rubric_group_id,),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def _load_rubric_group(conn, rubric_group_id):
    cur = conn.execute(
        q("""SELECT rubric_group_id, rg_name, rg_grade_target
             FROM rubric_groups
             WHERE rubric_group_id = ? AND rg_deleted_at IS NULL"""),
        (rubric_group_id,),
    )
    return _row_to_dict(cur.fetchone())


# ── Respondent helpers ────────────────────────────────────────
# Respondents are external people detected from transcripts (secret-shopping
# model). The upsert is case-insensitive on respondent_name, scoped to
# (company_id, location_id). Names like "Unknown" or "Name not provided"
# are not meaningful; those calls fall back to a shared sentinel respondent
# "Name Not Detected" scoped to (company_id, location_id).

_RESPONDENT_SKIP_VALUES = {"", "unknown", "name not provided", "not provided", "n/a"}
_SENTINEL_RESPONDENT_NAME = "Name Not Detected"


def _project_location_id(conn, project_id):
    """Resolve a project's location via projects → campaigns → locations."""
    cur = conn.execute(
        q("""SELECT c.location_id
             FROM projects p
             LEFT JOIN campaigns c ON c.campaign_id = p.campaign_id
             WHERE p.project_id = ?"""),
        (project_id,),
    )
    row = _row_to_dict(cur.fetchone())
    if not row:
        return None
    return row.get("location_id")


def _project_location_id_via_either(project_id):
    """Resolve a project's location via campaigns OR rubric_groups.

    Single-location projects without a campaign get their location from
    rubric_groups.location_id. Used for the location-intel refresh hook
    where we want the location even when the campaigns join is null.
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT COALESCE(c.location_id, rg.location_id) AS location_id
                   FROM projects p
                   LEFT JOIN campaigns     c  ON c.campaign_id     = p.campaign_id
                   LEFT JOIN rubric_groups rg ON rg.rubric_group_id = p.rubric_group_id
                  WHERE p.project_id = ?"""),
            (project_id,),
        )
        row = _row_to_dict(cur.fetchone())
    finally:
        conn.close()
    return row.get("location_id") if row else None


def _is_meaningful_respondent_name(name):
    if name is None:
        return False
    s = str(name).strip()
    return bool(s) and s.lower() not in _RESPONDENT_SKIP_VALUES


def _upsert_respondent(conn, company_id, location_id, respondent_name):
    """Upsert a respondent row. Case-insensitive match on respondent_name
    within (company_id, location_id). Increments call count on match, inserts
    a new row on miss with count=1 and first_seen=today.

    Always upserts. Falls back to the sentinel _SENTINEL_RESPONDENT_NAME
    ("Name Not Detected") when the supplied name is not meaningful, so all
    unnamed calls at a location roll up into one shared respondent row.

    Returns (respondent_id, canonical_name).
    """
    if _is_meaningful_respondent_name(respondent_name):
        name = str(respondent_name).strip()
    else:
        name = _SENTINEL_RESPONDENT_NAME

    # location_id may be NULL — Postgres `=` against NULL is UNKNOWN, so use
    # IS NOT DISTINCT FROM to get the NULL-safe compare.
    if IS_POSTGRES:
        loc_match = "location_id IS NOT DISTINCT FROM %s"
        params_select = (company_id, location_id, name)
    else:
        loc_match = "(location_id IS ? OR location_id = ?)"
        params_select = (company_id, location_id, location_id, name)
        # SQLite path kept as a best-effort — no prod reliance.
    if IS_POSTGRES:
        cur = conn.execute(
            f"""SELECT respondent_id, respondent_name
                  FROM respondents
                 WHERE company_id = %s
                   AND {loc_match}
                   AND LOWER(respondent_name) = LOWER(%s)
                 LIMIT 1""",
            params_select,
        )
    else:
        cur = conn.execute(
            f"""SELECT respondent_id, respondent_name
                  FROM respondents
                 WHERE company_id = ?
                   AND {loc_match}
                   AND LOWER(respondent_name) = LOWER(?)
                 LIMIT 1""",
            params_select,
        )
    existing = _row_to_dict(cur.fetchone())
    if existing:
        rid = existing["respondent_id"]
        conn.execute(
            q("""UPDATE respondents
                    SET respondent_call_count = respondent_call_count + 1
                  WHERE respondent_id = ?"""),
            (rid,),
        )
        return rid, existing["respondent_name"]

    if IS_POSTGRES:
        cur = conn.execute(
            """INSERT INTO respondents
                   (company_id, location_id, respondent_name,
                    respondent_call_count, respondent_first_seen)
               VALUES (%s, %s, %s, 1, CURRENT_DATE)
               RETURNING respondent_id""",
            (company_id, location_id, name),
        )
        return cur.fetchone()["respondent_id"], name
    conn.execute(
        """INSERT INTO respondents
               (company_id, location_id, respondent_name,
                respondent_call_count, respondent_first_seen)
           VALUES (?, ?, ?, 1, date('now'))""",
        (company_id, location_id, name),
    )
    rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return rid, name


def _link_interaction_respondent(conn, interaction_id, respondent_id):
    conn.execute(
        q("UPDATE interactions SET respondent_id = ? WHERE interaction_id = ?"),
        (respondent_id, interaction_id),
    )


# ── Rubric conversion ──────────────────────────────────────────
# rubric_items use ri_score_type ∈ {'out_of_10','yes_no','yes_no_pending'}.
# grader.py expects legacy V1 shape with type ∈ {'numeric','yes_no','yes_no_pending'}.
# This translation layer lives at the route boundary so grader.py stays V1-shaped.


_SCORE_TYPE_V2_TO_V1 = {
    "out_of_10":      "numeric",
    "yes_no":         "yes_no",
    "yes_no_pending": "yes_no_pending",
}

_SCORE_TYPE_V1_TO_V2 = {v: k for k, v in _SCORE_TYPE_V2_TO_V1.items()}


def _items_to_criteria(items):
    """Convert rubric_items rows to grader.py criteria dicts."""
    criteria = []
    for it in items:
        v1_type = _SCORE_TYPE_V2_TO_V1.get(it["ri_score_type"], "numeric")
        criteria.append({
            "name":             it["ri_name"],
            "type":             v1_type,
            "scale":            10,
            "weight":           float(it["ri_weight"]) if it["ri_weight"] is not None else 1.0,
            "scoring_guidance": it.get("ri_scoring_guidance") or "",
            "_rubric_item_id":  it["rubric_item_id"],
        })
    return criteria


def _criteria_to_snapshot(criterion):
    """Extract snapshot columns for interaction_rubric_scores from a criterion dict."""
    return {
        "rubric_item_id":           criterion.get("_rubric_item_id"),
        "irs_snapshot_name":        criterion["name"],
        "irs_snapshot_score_type":  _SCORE_TYPE_V1_TO_V2.get(
            criterion.get("type", "numeric"), "out_of_10"
        ),
        "irs_snapshot_weight":      float(criterion.get("weight", 1.0)),
        "irs_snapshot_scoring_guidance": criterion.get("scoring_guidance") or None,
    }


def _score_to_numeric(value, score_type):
    """Normalize a raw score value to a NUMERIC(5,2) in [0,10] for storage."""
    if value is None:
        return 0.0
    if score_type == "numeric":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    # yes_no / yes_no_pending → map to 10 / 0 / 5
    s = str(value).strip().lower()
    if s == "yes":
        return 10.0
    if s == "no":
        return 0.0
    return 5.0  # Pending / unknown


def _build_criteria_from_request_rubric(rubric):
    """Accept a caller-provided rubric JSON and coerce it into criteria format.

    Supported shapes:
        {"criteria": [...], "script": "...", "context": "..."}
        {"items": [...]}
        [criterion, ...]
    """
    if rubric is None:
        return None, None, None
    if isinstance(rubric, list):
        return rubric, None, None
    if isinstance(rubric, dict):
        if "criteria" in rubric:
            return rubric["criteria"], rubric.get("script"), rubric.get("context")
        if "items" in rubric:
            return _items_to_criteria(rubric["items"]), rubric.get("script"), rubric.get("context")
    return None, None, None


# ── Persistence helpers ────────────────────────────────────────


def _insert_interaction_row(conn, *, project_id, caller_user_id, respondent_user_id,
                            interaction_date, status_id,
                            location_id=None,
                            call_start_time=None, call_end_time=None,
                            call_duration_seconds=None,
                            set_uploaded_at=False):
    """Insert a fresh interaction row. Returns interaction_id.

    Live recordings pass call_start/end/duration; uploads leave them None.
    set_uploaded_at=True stamps interaction_uploaded_at = NOW() — the grade
    submission path sets it; the no-answer log path does not.
    location_id is the property the call was placed to; callers that have it
    (live grade, no-answer) should pass it so it's the source-of-truth for
    downstream respondent upserts and report routing.
    """
    if IS_POSTGRES:
        cur = conn.execute(
            """INSERT INTO interactions
                   (project_id, caller_user_id, respondent_user_id,
                    interaction_location_id,
                    interaction_date, interaction_submitted_at, status_id,
                    interaction_call_start_time, interaction_call_end_time,
                    interaction_call_duration_seconds,
                    interaction_uploaded_at)
               VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s,
                       CASE WHEN %s THEN NOW() ELSE NULL END)
               RETURNING interaction_id""",
            (project_id, caller_user_id, respondent_user_id,
             location_id,
             interaction_date, status_id,
             call_start_time, call_end_time, call_duration_seconds,
             bool(set_uploaded_at)),
        )
        return cur.fetchone()["interaction_id"]
    conn.execute(
        """INSERT INTO interactions
               (project_id, caller_user_id, respondent_user_id,
                interaction_location_id,
                interaction_date, interaction_submitted_at, status_id,
                interaction_call_start_time, interaction_call_end_time,
                interaction_call_duration_seconds,
                interaction_uploaded_at)
           VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?,
                   CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)""",
        (project_id, caller_user_id, respondent_user_id,
         location_id,
         interaction_date, status_id,
         call_start_time, call_end_time, call_duration_seconds,
         1 if set_uploaded_at else 0),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _update_interaction_status(conn, interaction_id, status_id):
    conn.execute(
        q("UPDATE interactions SET status_id = ? WHERE interaction_id = ?"),
        (status_id, interaction_id),
    )
    conn.commit()


def _persist_grade_result(conn, interaction_id, *, grade_result, criteria,
                          final_status_id, audio_url, audio_bytes,
                          total_score, flags, reviewer_context=None,
                          regrade=False):
    """Write the full grade result for an interaction in one transaction.

    - Overwrites scalar grade fields on interactions
    - Deletes + rebuilds interaction_rubric_scores rows (snapshot pattern)
    - Replaces clarifying_questions rows with the new set

    Caller is responsible for COMMIT.
    """
    scores = grade_result.get("scores") or {}
    explanations = grade_result.get("explanations") or {}
    responder_name = grade_result.get("responder_name")
    strengths = grade_result.get("strengths") or ""
    weaknesses = grade_result.get("weaknesses") or ""
    overall_assessment = grade_result.get("overall_assessment") or ""

    if audio_url is not None or audio_bytes is not None:
        conn.execute(
            q("""UPDATE interactions SET
                    interaction_transcript         = ?,
                    interaction_audio_url          = ?,
                    interaction_audio_data         = ?,
                    interaction_overall_score      = ?,
                    interaction_flags              = ?,
                    interaction_strengths          = ?,
                    interaction_weaknesses         = ?,
                    interaction_overall_assessment = ?,
                    interaction_responder_name     = ?,
                    interaction_reviewer_context   = ?,
                    status_id                      = ?
                 WHERE interaction_id = ?"""),
            (
                grade_result.get("_transcript"),
                audio_url,
                audio_bytes,
                total_score,
                flags,
                strengths,
                weaknesses,
                overall_assessment,
                responder_name,
                reviewer_context,
                final_status_id,
                interaction_id,
            ),
        )
    else:
        # Regrade path: audio/transcript unchanged, update score fields only.
        conn.execute(
            q("""UPDATE interactions SET
                    interaction_overall_score      = ?,
                    interaction_flags              = ?,
                    interaction_strengths          = ?,
                    interaction_weaknesses         = ?,
                    interaction_overall_assessment = ?,
                    interaction_responder_name     = COALESCE(?, interaction_responder_name),
                    interaction_reviewer_context   = ?,
                    status_id                      = ?
                 WHERE interaction_id = ?"""),
            (
                total_score,
                flags,
                strengths,
                weaknesses,
                overall_assessment,
                responder_name,
                reviewer_context,
                final_status_id,
                interaction_id,
            ),
        )

    # Wipe + rewrite per-criterion snapshot rows.
    conn.execute(
        q("DELETE FROM interaction_rubric_scores WHERE interaction_id = ?"),
        (interaction_id,),
    )
    for c in criteria:
        snap = _criteria_to_snapshot(c)
        score_value = _score_to_numeric(scores.get(c["name"]), c.get("type", "numeric"))
        explanation = explanations.get(c["name"]) or ""
        conn.execute(
            q("""INSERT INTO interaction_rubric_scores (
                    interaction_id, rubric_item_id,
                    irs_snapshot_name, irs_snapshot_score_type,
                    irs_snapshot_weight, irs_snapshot_scoring_guidance,
                    irs_score_value, irs_score_ai_explanation
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""),
            (
                interaction_id,
                snap["rubric_item_id"],
                snap["irs_snapshot_name"],
                snap["irs_snapshot_score_type"],
                snap["irs_snapshot_weight"],
                snap["irs_snapshot_scoring_guidance"],
                score_value,
                explanation,
            ),
        )

    # Clarifying questions are managed separately — the two-step flow saves
    # them up front after get_clarifying_questions() and updates answers on
    # regrade. We only rewrite CQ rows here if the grade_result includes the
    # key (legacy single-shot path); V2 grade_with_claude doesn't return them.
    if "clarifying_questions" in grade_result:
        conn.execute(
            q("DELETE FROM clarifying_questions WHERE interaction_id = ?"),
            (interaction_id,),
        )
        for idx, cq in enumerate(grade_result.get("clarifying_questions") or []):
            conn.execute(
                q("""INSERT INTO clarifying_questions (
                        interaction_id, cq_text, cq_ai_reason, cq_response_format,
                        cq_answer_value, cq_order
                     ) VALUES (?, ?, ?, ?, NULL, ?)"""),
                (
                    interaction_id,
                    cq.get("question") or "",
                    cq.get("reason") or "",
                    cq.get("format") or "yes_no",
                    idx,
                ),
            )


def _save_transcript_and_audio(conn, interaction_id, *, transcript,
                               audio_url, audio_bytes, status_id):
    """Write the transcript + audio to the interaction row, no scores.

    Used by the two-step grade flow after transcription completes, before
    clarifying questions are answered. `status_id` should be
    STATUS_AWAITING_CLARIFICATION (or STATUS_GRADING if auto-grading).
    """
    conn.execute(
        q("""UPDATE interactions SET
                interaction_transcript  = ?,
                interaction_audio_url   = ?,
                interaction_audio_data  = ?,
                status_id               = ?
             WHERE interaction_id = ?"""),
        (transcript, audio_url, audio_bytes, status_id, interaction_id),
    )


def _save_audio(interaction_id, audio_bytes, filename_ext):
    """Return (audio_url, audio_data) tuple for storage.

    PostgreSQL: keeps bytes in interaction_audio_data and uses a db:// marker.
    SQLite: writes to disk under _AUDIO_DIR and returns the filesystem path.
    """
    if IS_POSTGRES:
        return (f"db://interactions/{interaction_id}", audio_bytes)
    _AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    ext = filename_ext or ".bin"
    fs_path = _AUDIO_DIR / f"interaction_{interaction_id}{ext}"
    fs_path.write_bytes(audio_bytes)
    return (str(fs_path), None)


def _fetch_scores(conn, interaction_id):
    cur = conn.execute(
        q("""SELECT * FROM interaction_rubric_scores
             WHERE interaction_id = ?
             ORDER BY interaction_rubric_score_id ASC"""),
        (interaction_id,),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def _fetch_clarifying_questions(conn, interaction_id):
    cur = conn.execute(
        q("""SELECT * FROM clarifying_questions
             WHERE interaction_id = ?
             ORDER BY cq_order ASC"""),
        (interaction_id,),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def _save_clarifying_questions(conn, interaction_id, questions):
    """Insert fresh CQ rows for an interaction. Wipes existing rows first.

    Called immediately after get_clarifying_questions() returns, before the
    reviewer has answered anything. cq_answer_value stays NULL until the
    reviewer submits answers via the regrade endpoint.
    """
    conn.execute(
        q("DELETE FROM clarifying_questions WHERE interaction_id = ?"),
        (interaction_id,),
    )
    for idx, cq in enumerate(questions or []):
        conn.execute(
            q("""INSERT INTO clarifying_questions (
                    interaction_id, cq_text, cq_ai_reason, cq_response_format,
                    cq_answer_value, cq_order
                 ) VALUES (?, ?, ?, ?, NULL, ?)"""),
            (
                interaction_id,
                cq.get("question") or "",
                cq.get("reason") or "",
                cq.get("format") or "yes_no",
                idx,
            ),
        )


def _apply_clarifying_answers(conn, interaction_id, answers):
    """Persist the reviewer's answers onto existing clarifying_questions rows.

    `answers` is a {question_text: answer_value} dict as submitted from the UI.
    We match each row by cq_text and update its cq_answer_value. Rows without
    a matching answer keep cq_answer_value = NULL.
    """
    if not answers:
        return
    for question_text, value in answers.items():
        if value is None:
            continue
        conn.execute(
            q("""UPDATE clarifying_questions
                    SET cq_answer_value = ?
                  WHERE interaction_id = ? AND cq_text = ?"""),
            (str(value), interaction_id, question_text),
        )


def _build_grade_response(interaction_id, grade_result, total_score, flags, transcript=None):
    """Shape the response body returned from POST /api/grade and the regrade routes."""
    return {
        "interaction_id":      interaction_id,
        "responder_name":      grade_result.get("responder_name"),
        "scores":              grade_result.get("scores") or {},
        "confidence":          grade_result.get("confidence") or {},
        "timestamps":          grade_result.get("timestamps") or {},
        "explanations":        grade_result.get("explanations") or {},
        "overall_assessment":  grade_result.get("overall_assessment") or "",
        "strengths":           grade_result.get("strengths") or "",
        "weaknesses":          grade_result.get("weaknesses") or "",
        "flags":               flags,
        "total_score":         total_score,
        "clarifying_questions": grade_result.get("clarifying_questions") or [],
        "transcript":          transcript,
    }


# ── Shared grade-and-persist helper ────────────────────────────
#
# Used by both /api/grade (auto-grade path when there are no clarifying
# questions) and /api/interactions/<id>/regrade (reviewer submits CQ
# answers, we grade with them as context). Assumes transcript + audio
# are already saved on the interaction row — only the score fields,
# rubric-score snapshots, respondent link, and status get written here.


class _GradingAPIError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _grade_and_persist(*, interaction_id, company_id, project_id,
                       respondent_user_id, transcript, context_answers,
                       criteria, script_text, context_text, grade_target,
                       is_initial_grade, location_id=None):
    """Run grade_with_claude and commit the results.

    If is_initial_grade is True we don't bump interaction_regrade_count —
    this is the first grade on a freshly-transcribed interaction. If False
    we're reusing the same endpoint to re-grade an already-graded call,
    and we bookkeep the regrade counter + original_score.

    location_id is the caller-supplied source-of-truth (from the UI on
    /api/grade, or read off interaction.interaction_location_id on regrade).
    When None we fall back to _project_location_id(project_id) for legacy
    rows or tenants where the project chain still resolves to a single
    location. This determines which (company_id, location_id, name) bucket
    the respondent gets upserted into.

    Returns a dict shaped like the grade response (interaction_id, scores,
    flags, total_score, respondent_*, transcript). Raises _GradingAPIError
    on failure so the caller can emit the right HTTP status.
    """
    ok, msg = check_rate_limit(company_id, "anthropic")
    if not ok:
        raise _GradingAPIError(msg, 429)

    try:
        grade_result = grader.grade_with_claude(
            transcript=transcript,
            context_answers=context_answers or {},
            rubric_criteria=criteria,
            rubric_script=script_text,
            rubric_context=context_text,
            grade_target=grade_target,
        )
    except Exception:
        logger.exception("Grading failed for interaction %s", interaction_id)
        conn = get_conn()
        try:
            _update_interaction_status(conn, interaction_id, STATUS_SUBMITTED)
        finally:
            conn.close()
        raise _GradingAPIError("Grading failed. Please try again.", 502)

    increment_usage(company_id, "anthropic")

    scores = grade_result.get("scores") or {}
    total_score = grader.calculate_total(scores, criteria)
    flags = grader.build_flags(scores, criteria)

    conn = get_conn()
    respondent_id = None
    respondent_name_final = grade_result.get("responder_name")
    try:
        # Transcript + audio already saved — pass audio_url=None to trigger
        # the "update score fields only" branch inside _persist_grade_result.
        _persist_grade_result(
            conn, interaction_id,
            grade_result=grade_result,
            criteria=criteria,
            final_status_id=STATUS_GRADED,
            audio_url=None,
            audio_bytes=None,
            total_score=total_score,
            flags=flags,
        )
        # For re-grades (not the first grade): bump counter + preserve original.
        if not is_initial_grade:
            cur = conn.execute(
                q("""SELECT interaction_overall_score, interaction_original_score
                       FROM interactions WHERE interaction_id = ?"""),
                (interaction_id,),
            )
            existing = _row_to_dict(cur.fetchone()) or {}
            original = (
                existing.get("interaction_original_score")
                if existing.get("interaction_original_score") is not None
                else existing.get("interaction_overall_score")
            )
            conn.execute(
                q("""UPDATE interactions SET
                        interaction_regrade_count  = interaction_regrade_count + 1,
                        interaction_original_score = COALESCE(?, interaction_original_score)
                     WHERE interaction_id = ?"""),
                (original, interaction_id),
            )

        # Source-of-truth: caller-supplied location_id (from the UI form on
        # /api/grade, or read off interaction.interaction_location_id on
        # regrade). Fall back to the project chain only for legacy rows.
        effective_location_id = location_id or _project_location_id(conn, project_id)
        respondent_id, canonical = _upsert_respondent(
            conn, company_id, effective_location_id, respondent_name_final,
        )
        if respondent_id is not None:
            _link_interaction_respondent(conn, interaction_id, respondent_id)
            respondent_name_final = canonical
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Persisting grade failed for interaction %s", interaction_id)
        raise _GradingAPIError("Saving grade failed. Please try again.", 500)
    finally:
        conn.close()

    write_audit_log(
        current_user.user_id,
        ACTION_GRADED if is_initial_grade else ACTION_REGRADED,
        ENTITY_INTERACTION, interaction_id,
        metadata={"project_id": project_id, "total_score": total_score,
                  "final_status_id": STATUS_GRADED,
                  "has_context_answers": bool(context_answers),
                  "location_id": effective_location_id},
    )
    update_performance_report_async(
        interaction_id, company_id,
        respondent_user_id=respondent_user_id or None,
        respondent_id=respondent_id,
    )
    # Refresh the location intel card in the background — non-blocking.
    location_id_for_intel = _project_location_id_via_either(project_id)
    if location_id_for_intel:
        compute_location_intel_async(location_id_for_intel, company_id)

    response = _build_grade_response(
        interaction_id, grade_result, total_score, flags, transcript=transcript
    )
    response["respondent_id"] = respondent_id
    response["respondent_name"] = respondent_name_final
    response["awaiting_clarification"] = False
    return response


# ═══════════════════════════════════════════════════════════════
# POST /api/grade   —  submit a call for AI grading
# ═══════════════════════════════════════════════════════════════


@interactions_bp.route("/grade", methods=["POST"])
@login_required
def submit_grade():
    company_id, err = _require_company()
    if err: return err

    # Multipart form inputs
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

    caller_user_id     = request.form.get("caller_user_id") or None
    respondent_user_id = request.form.get("respondent_user_id") or None
    interaction_date   = _parse_date(request.form.get("interaction_date"), date.today())

    # location_id is required. The UI marks the location select required,
    # so a missing or non-integer value means a bypassed UI or a stale
    # browser. Reject loudly rather than silently writing a NULL row.
    # (Cross-tenant ownership check happens below, inside the conn block,
    # next to _get_project_in_company.)
    try:
        location_id = int(request.form.get("location_id") or 0)
    except (TypeError, ValueError):
        return _err("Invalid location_id", 400)
    if not location_id:
        return _err("Missing location_id", 400)

    # Live-recording timestamps. All three are optional (uploads omit them).
    call_start_time       = (request.form.get("call_start_time") or "").strip() or None
    call_end_time         = (request.form.get("call_end_time")   or "").strip() or None
    call_duration_seconds = request.form.get("call_duration_seconds")
    try:
        call_duration_seconds = int(call_duration_seconds) if call_duration_seconds else None
    except (TypeError, ValueError):
        call_duration_seconds = None

    # Optional client-supplied rubric JSON
    rubric_raw = request.form.get("rubric")
    client_rubric = None
    if rubric_raw:
        try:
            client_rubric = json.loads(rubric_raw)
        except json.JSONDecodeError:
            return _err("Invalid rubric JSON", 400)

    # Rate-limit gates (both transcription + grading)
    ok, msg = check_rate_limit(company_id, "assemblyai")
    if not ok: return _err(msg, 429)
    ok, msg = check_rate_limit(company_id, "anthropic")
    if not ok: return _err(msg, 429)

    conn = get_conn()
    try:
        # Verify project ownership + load its rubric_group
        project = _get_project_in_company(conn, project_id, company_id)
        if not project:
            return _err("Project not found", 404)

        # Verify location ownership (same tenant-guard pattern). Cheap PK
        # lookup; deliberately separate from the project check so we can
        # return a precise error message.
        loc_row = conn.execute(
            q("""SELECT 1 FROM locations
                 WHERE location_id = ? AND company_id = ?
                   AND location_deleted_at IS NULL"""),
            (location_id, company_id),
        ).fetchone()
        if not loc_row:
            return _err("Invalid location_id for this company", 400)

        # Resolve criteria: client-supplied wins over project rubric_group
        script_text = None
        context_text = None
        grade_target = "respondent"

        if client_rubric is not None:
            criteria, script_text, context_text = _build_criteria_from_request_rubric(client_rubric)
            if not criteria:
                return _err("Rubric payload contained no criteria", 400)
        else:
            rubric_group = _load_rubric_group(conn, project["rubric_group_id"])
            if rubric_group is None:
                return _err("Project's rubric_group is missing or deleted", 500)
            grade_target = rubric_group["rg_grade_target"] or "respondent"
            items = _load_rubric_items(conn, project["rubric_group_id"])
            if not items:
                return _err("Project rubric has no items", 400)
            criteria = _items_to_criteria(items)

        # Create the interaction row up front so status transitions are visible.
        # Stamps interaction_uploaded_at = NOW() server-side regardless of
        # whether the call came from upload, live-recording, or VoIP queue.
        interaction_id = _insert_interaction_row(
            conn,
            project_id=project_id,
            caller_user_id=caller_user_id,
            respondent_user_id=respondent_user_id,
            location_id=location_id,
            interaction_date=interaction_date,
            status_id=STATUS_SUBMITTED,
            call_start_time=call_start_time,
            call_end_time=call_end_time,
            call_duration_seconds=call_duration_seconds,
            set_uploaded_at=True,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    # Release the conn while we do I/O-heavy work. Transcription + grading
    # can take >60s; holding a PG conn that long is wasteful.
    conn.close()

    # Save the uploaded audio to a temp file for AssemblyAI
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        audio_file.save(tmp.name)
        tmp.close()

        # ── Step 1: transcribe ──
        conn = get_conn()
        try:
            _update_interaction_status(conn, interaction_id, STATUS_TRANSCRIBING)
        finally:
            conn.close()

        hints = load_active_hints(company_id)
        try:
            transcript = grader.transcribe(tmp.name, keyterms_prompt=hints)
        except grader.EmptyTranscriptError:
            logger.warning("Empty transcript for interaction %s", interaction_id)
            conn = get_conn()
            try:
                _update_interaction_status(conn, interaction_id, STATUS_SUBMITTED)
            finally:
                conn.close()
            return _err("Transcription returned no audible content. "
                        "Please verify the audio file is not silent and try again.", 502)
        except Exception:
            logger.exception("Transcription failed for interaction %s", interaction_id)
            conn = get_conn()
            try:
                _update_interaction_status(conn, interaction_id, STATUS_SUBMITTED)
            finally:
                conn.close()
            return _err("Transcription failed. Please try again.", 502)

        increment_usage(company_id, "assemblyai")

        # Persist transcript + audio immediately so the regrade route can
        # load the transcript when the reviewer submits CQ answers.
        with open(tmp.name, "rb") as f:
            audio_bytes = f.read()
        audio_url, audio_data = _save_audio(interaction_id, audio_bytes, ext)
        conn = get_conn()
        try:
            _save_transcript_and_audio(
                conn, interaction_id,
                transcript=transcript,
                audio_url=audio_url,
                audio_bytes=audio_data,
                status_id=STATUS_GRADING,   # about to ask Claude for CQs
            )
            conn.commit()
        finally:
            conn.close()

        # ── Step 2: ask Claude for clarifying questions ──
        try:
            questions = grader.get_clarifying_questions(
                transcript=transcript,
                rubric_criteria=criteria,
                rubric_script=script_text,
                rubric_context=context_text,
                grade_target=grade_target,
            )
        except Exception:
            logger.exception("Clarifying-question step failed for interaction %s",
                             interaction_id)
            conn = get_conn()
            try:
                _update_interaction_status(conn, interaction_id, STATUS_SUBMITTED)
            finally:
                conn.close()
            return _err("Preparing questions failed. Please try again.", 502)

        increment_usage(company_id, "anthropic")

        # Save CQ rows now. They'll be fetched back on the regrade path with
        # cq_answer_value filled in from the reviewer's inputs.
        conn = get_conn()
        try:
            _save_clarifying_questions(conn, interaction_id, questions)
            conn.commit()
        finally:
            conn.close()

        # ── Step 3: if Claude asked no questions, auto-grade immediately ──
        if not questions:
            try:
                grade_outcome = _grade_and_persist(
                    interaction_id=interaction_id,
                    company_id=company_id,
                    project_id=project_id,
                    respondent_user_id=respondent_user_id,
                    location_id=location_id,
                    transcript=transcript,
                    context_answers={},
                    criteria=criteria,
                    script_text=script_text,
                    context_text=context_text,
                    grade_target=grade_target,
                    is_initial_grade=True,
                )
            except _GradingAPIError as exc:
                return _err(exc.message, exc.status_code)
            return jsonify(grade_outcome)

        # ── Path B: return questions; reviewer answers on regrade ──
        conn = get_conn()
        try:
            _update_interaction_status(conn, interaction_id, STATUS_AWAITING_CLARIFICATION)
            conn.commit()
        finally:
            conn.close()
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    return jsonify({
        "interaction_id":        interaction_id,
        "transcript":            transcript,
        "clarifying_questions":  questions,
        "awaiting_clarification": True,
    })


# ═══════════════════════════════════════════════════════════════
# POST /api/interactions/no-answer  —  log a call with no answer
# ═══════════════════════════════════════════════════════════════


@interactions_bp.route("/interactions/no-answer", methods=["POST"])
@login_required
def log_no_answer():
    company_id, err = _require_company()
    if err: return err

    body = _body()
    project_id = body.get("project_id")
    if not project_id:
        return _err("Missing project_id", 400)

    # location_id is required. The UI sends it from the location dropdown;
    # absence means a bypassed UI or a stale browser. Reject loudly so we
    # never write a no-answer row that's invisible to per-property reports.
    try:
        location_id = int(body.get("location_id") or 0)
    except (TypeError, ValueError):
        return _err("Invalid location_id", 400)
    if not location_id:
        return _err("Missing location_id", 400)

    caller_user_id = body.get("caller_user_id") or None
    interaction_date = _parse_date(body.get("interaction_date"), date.today())

    conn = get_conn()
    try:
        if not _get_project_in_company(conn, project_id, company_id):
            return _err("Project not found", 404)
        # Verify location ownership (same tenant-guard pattern as submit_grade).
        loc_row = conn.execute(
            q("""SELECT 1 FROM locations
                 WHERE location_id = ? AND company_id = ?
                   AND location_deleted_at IS NULL"""),
            (location_id, company_id),
        ).fetchone()
        if not loc_row:
            return _err("Invalid location_id for this company", 400)
        interaction_id = _insert_interaction_row(
            conn,
            project_id=project_id,
            caller_user_id=caller_user_id,
            respondent_user_id=None,
            location_id=location_id,
            interaction_date=interaction_date,
            status_id=STATUS_NO_ANSWER,
        )
        write_audit_log(
            current_user.user_id, ACTION_SUBMITTED, ENTITY_INTERACTION,
            interaction_id,
            metadata={"project_id": project_id, "no_answer": True,
                      "location_id": location_id},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True, "interaction_id": interaction_id})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# GET /api/interactions  —  list interactions for current company
# ═══════════════════════════════════════════════════════════════


@interactions_bp.route("/interactions", methods=["GET"])
@login_required
def list_interactions():
    company_id, err = _require_company()
    if err: return err

    args = request.args
    filters = ["p.company_id = ?", "i.interaction_deleted_at IS NULL"]
    params = [company_id]

    if args.get("project_id"):
        filters.append("i.project_id = ?")
        params.append(args["project_id"])
    if args.get("caller_user_id"):
        filters.append("i.caller_user_id = ?")
        params.append(args["caller_user_id"])
    if args.get("respondent_user_id"):
        filters.append("i.respondent_user_id = ?")
        params.append(args["respondent_user_id"])
    if args.get("from_date"):
        filters.append("i.interaction_date >= ?")
        params.append(args["from_date"])
    if args.get("to_date"):
        filters.append("i.interaction_date <= ?")
        params.append(args["to_date"])
    if args.get("status_id"):
        filters.append("i.status_id = ?")
        params.append(args["status_id"])
    if args.get("location_id"):
        # Match through the campaign join (cmp.location_id) — the same path
        # the SELECT uses for location_name. Single-location projects without
        # a campaign won't appear here; that's acceptable for filtering since
        # the pickable Location dropdown is sourced from /api/locations.
        filters.append("cmp.location_id = ?")
        params.append(args["location_id"])
    if args.get("q"):
        filters.append("LOWER(i.interaction_transcript) LIKE LOWER(?)")
        params.append(f"%{args['q']}%")
    if args.get("score_max"):
        try:
            score_max = float(args["score_max"])
        except (TypeError, ValueError):
            return _err("score_max must be numeric", 400)
        filters.append("i.interaction_overall_score IS NOT NULL")
        filters.append("i.interaction_overall_score <= ?")
        params.append(score_max)

    where_clause = " AND ".join(filters)
    # Respondent display name priority:
    #   1. respondents.respondent_name (detected external person)
    #   2. users.user_first_name + user_last_name (known-user path)
    #   3. interaction_responder_name (legacy free-text, still on some rows)
    sql = f"""
        SELECT
            i.interaction_id,
            i.project_id,
            i.caller_user_id,
            i.respondent_user_id,
            i.respondent_id,
            i.interaction_date,
            i.status_id,
            i.interaction_overall_score,
            i.interaction_original_score,
            i.interaction_regrade_count,
            i.interaction_flags,
            i.interaction_responder_name,
            i.interaction_call_start_time,
            i.interaction_call_duration_seconds,
            i.interaction_uploaded_at,
            i.interaction_created_at,
            i.interaction_updated_at,
            p.project_name,
            cmp.campaign_name,
            loc.location_id,
            loc.location_name,
            (caller.user_first_name || ' ' || caller.user_last_name) AS caller_name,
            COALESCE(
                r.respondent_name,
                NULLIF(TRIM(respondent.user_first_name || ' ' || respondent.user_last_name), ''),
                i.interaction_responder_name
            ) AS respondent_name
        FROM interactions i
        JOIN projects   p   ON p.project_id  = i.project_id
        LEFT JOIN campaigns cmp ON cmp.campaign_id = p.campaign_id
        LEFT JOIN locations loc ON loc.location_id = cmp.location_id
        LEFT JOIN users caller     ON caller.user_id     = i.caller_user_id
        LEFT JOIN users respondent ON respondent.user_id = i.respondent_user_id
        LEFT JOIN respondents r    ON r.respondent_id    = i.respondent_id
        WHERE {where_clause}
        ORDER BY i.interaction_date DESC, i.interaction_id DESC
    """

    # SQLite doesn't support the (a || ' ' || b) concat the same way, but the
    # || operator actually works fine in SQLite for strings. Leave as-is.
    conn = get_conn()
    try:
        cur = conn.execute(q(sql), params)
        return jsonify(_rows(cur))
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# GET /api/interactions/<id>  —  full interaction detail
# ═══════════════════════════════════════════════════════════════


@interactions_bp.route("/interactions/<int:interaction_id>", methods=["GET"])
@login_required
def get_interaction(interaction_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""
            SELECT
                i.*,
                p.project_name,
                cmp.campaign_name,
                loc.location_name,
                (caller.user_first_name || ' ' || caller.user_last_name) AS caller_name,
                COALESCE(
                    r.respondent_name,
                    NULLIF(TRIM(respondent.user_first_name || ' ' || respondent.user_last_name), ''),
                    i.interaction_responder_name
                ) AS respondent_name
            FROM interactions i
            JOIN projects   p   ON p.project_id  = i.project_id
            LEFT JOIN campaigns cmp ON cmp.campaign_id = p.campaign_id
            LEFT JOIN locations loc ON loc.location_id = cmp.location_id
            LEFT JOIN users caller     ON caller.user_id     = i.caller_user_id
            LEFT JOIN users respondent ON respondent.user_id = i.respondent_user_id
            LEFT JOIN respondents r    ON r.respondent_id    = i.respondent_id
            WHERE i.interaction_id = ? AND p.company_id = ?
              AND i.interaction_deleted_at IS NULL
            """),
            (interaction_id, company_id),
        )
        row = _row_to_dict(cur.fetchone())
        if not row:
            return _err("Interaction not found", 404)

        # Strip the audio blob from JSON output — large and binary.
        row.pop("interaction_audio_data", None)

        row["rubric_scores"] = _fetch_scores(conn, interaction_id)
        row["clarifying_questions"] = _fetch_clarifying_questions(conn, interaction_id)
        return jsonify(row)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# PUT /api/interactions/<id>/respondent  —  edit detected respondent
# ═══════════════════════════════════════════════════════════════
#
# Upserts a respondent row for this company/location/name (case-insensitive)
# and re-links the interaction. Lets the UI correct a name Claude missed or
# fill in one Claude couldn't detect. Scoped to the current company.


@interactions_bp.route("/interactions/<int:interaction_id>/respondent", methods=["PUT"])
@login_required
def update_interaction_respondent(interaction_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    name = (body.get("respondent_name") or "").strip()
    if not name:
        return _err("respondent_name is required", 400)
    if not _is_meaningful_respondent_name(name):
        return _err("Respondent name must be specific — avoid placeholders like 'Unknown'.", 400)

    conn = get_conn()
    try:
        interaction = _get_interaction_in_company(conn, interaction_id, company_id)
        if not interaction or interaction["interaction_deleted_at"] is not None:
            return _err("Interaction not found", 404)

        # Source-of-truth: the location stamped on the interaction row at
        # creation time. Fall back to the project chain only for legacy rows
        # that predate the interaction_location_id column.
        location_id = (interaction["interaction_location_id"]
                       or _project_location_id(conn, interaction["project_id"]))
        respondent_id, canonical = _upsert_respondent(
            conn, company_id, location_id, name,
        )
        if respondent_id is None:
            return _err("Could not save respondent name.", 400)
        _link_interaction_respondent(conn, interaction_id, respondent_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "interaction_id": interaction_id,
        "respondent_id":  respondent_id,
        "respondent_name": canonical,
    })


# ═══════════════════════════════════════════════════════════════
# POST /api/interactions/<id>/regrade  —  apply clarifying answers
# ═══════════════════════════════════════════════════════════════


@interactions_bp.route("/interactions/<int:interaction_id>/regrade", methods=["POST"])
@login_required
def regrade_with_answers(interaction_id):
    """Submit clarifying-question answers and grade the call.

    This is the second leg of the two-step grade flow (POST /api/grade
    returns the questions, this endpoint receives the reviewer's answers
    and produces the final scores). Also handles re-grading a call that
    has already been graded once — we detect that case by status_id.
    """
    company_id, err = _require_company()
    if err: return err

    body = _body()
    context_answers = body.get("context_answers") or {}
    if not isinstance(context_answers, dict):
        return _err("context_answers must be an object", 400)

    client_rubric = body.get("rubric")

    conn = get_conn()
    try:
        interaction = _get_interaction_in_company(conn, interaction_id, company_id)
        if not interaction or interaction["interaction_deleted_at"] is not None:
            return _err("Interaction not found", 404)
        if not interaction["interaction_transcript"]:
            return _err("Interaction has no transcript to grade", 400)

        project = _get_project_in_company(conn, interaction["project_id"], company_id)
        if not project:
            return _err("Interaction's project is missing", 404)

        grade_target = "respondent"
        script_text = context_text = None
        if client_rubric is not None:
            criteria, script_text, context_text = _build_criteria_from_request_rubric(client_rubric)
            if not criteria:
                return _err("Rubric payload contained no criteria", 400)
        else:
            rubric_group = _load_rubric_group(conn, project["rubric_group_id"])
            grade_target = (rubric_group or {}).get("rg_grade_target") or "respondent"
            items = _load_rubric_items(conn, project["rubric_group_id"])
            if not items:
                return _err("Project rubric has no items", 400)
            criteria = _items_to_criteria(items)

        transcript = interaction["interaction_transcript"]
        project_id = interaction["project_id"]
        respondent_user_id = interaction["respondent_user_id"]
        # Source-of-truth: the location stamped on the interaction row at
        # creation time. _grade_and_persist's "or _project_location_id"
        # fallback handles legacy rows that predate the column.
        location_id = interaction["interaction_location_id"]
        # "First grade" path = status is awaiting-clarification or earlier.
        # "Real regrade" path = status is already GRADED.
        is_initial_grade = interaction["status_id"] != STATUS_GRADED

        # Persist the reviewer's answers on the existing CQ rows so they're
        # visible in history and can be inspected later.
        _apply_clarifying_answers(conn, interaction_id, context_answers)
        conn.commit()
    finally:
        conn.close()

    try:
        response = _grade_and_persist(
            interaction_id=interaction_id,
            company_id=company_id,
            project_id=project_id,
            respondent_user_id=respondent_user_id,
            location_id=location_id,
            transcript=transcript,
            context_answers=context_answers,
            criteria=criteria,
            script_text=script_text,
            context_text=context_text,
            grade_target=grade_target,
            is_initial_grade=is_initial_grade,
        )
    except _GradingAPIError as exc:
        return _err(exc.message, exc.status_code)

    return jsonify(response)


# ═══════════════════════════════════════════════════════════════
# POST /api/interactions/<id>/regrade-with-context
# ═══════════════════════════════════════════════════════════════


@interactions_bp.route("/interactions/<int:interaction_id>/regrade-with-context", methods=["POST"])
@login_required
@role_required("manager", "admin", "super_admin")
def regrade_with_context(interaction_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    new_context = (body.get("context") or "").strip()
    if not new_context:
        return _err("Missing context", 400)

    client_rubric = body.get("rubric")

    ok, msg = check_rate_limit(company_id, "anthropic")
    if not ok: return _err(msg, 429)

    conn = get_conn()
    try:
        interaction = _get_interaction_in_company(conn, interaction_id, company_id)
        if not interaction or interaction["interaction_deleted_at"] is not None:
            return _err("Interaction not found", 404)
        if not interaction["interaction_transcript"]:
            return _err("Interaction has no transcript to regrade", 400)

        project = _get_project_in_company(conn, interaction["project_id"], company_id)
        if not project:
            return _err("Interaction's project is missing", 404)

        grade_target = "respondent"
        script_text = context_text_from_rubric = None
        if client_rubric is not None:
            criteria, script_text, context_text_from_rubric = _build_criteria_from_request_rubric(client_rubric)
            if not criteria:
                return _err("Rubric payload contained no criteria", 400)
        else:
            rubric_group = _load_rubric_group(conn, project["rubric_group_id"])
            grade_target = (rubric_group or {}).get("rg_grade_target") or "respondent"
            items = _load_rubric_items(conn, project["rubric_group_id"])
            if not items:
                return _err("Project rubric has no items", 400)
            criteria = _items_to_criteria(items)

        transcript = interaction["interaction_transcript"]
        existing_overall = interaction["interaction_overall_score"]
        existing_original = interaction["interaction_original_score"]
        existing_context_text = interaction["interaction_reviewer_context"] or ""
    finally:
        conn.close()

    combined_context = (existing_context_text + "\n\n" + new_context).strip() if existing_context_text else new_context
    grading_context = combined_context
    if context_text_from_rubric:
        grading_context = (context_text_from_rubric + "\n\n" + combined_context).strip()

    try:
        grade_result = grader.grade_with_claude(
            transcript=transcript,
            context_answers=None,
            rubric_criteria=criteria,
            rubric_script=script_text,
            rubric_context=grading_context,
            grade_target=grade_target,
        )
    except Exception:
        logger.exception("Context-regrade failed for interaction %s", interaction_id)
        return _err("Grading failed. Please try again.", 502)

    increment_usage(company_id, "anthropic")

    scores = grade_result.get("scores") or {}
    total_score = grader.calculate_total(scores, criteria)
    flags = grader.build_flags(scores, criteria)
    has_cqs = bool(grade_result.get("clarifying_questions"))
    final_status = STATUS_AWAITING_CLARIFICATION if has_cqs else STATUS_GRADED

    conn = get_conn()
    respondent_id = interaction.get("respondent_id")
    respondent_name_final = grade_result.get("responder_name")
    try:
        _persist_grade_result(
            conn, interaction_id,
            grade_result=grade_result,
            criteria=criteria,
            final_status_id=final_status,
            audio_url=None,
            audio_bytes=None,
            total_score=total_score,
            flags=flags,
            reviewer_context=combined_context,
        )
        # Source-of-truth: the location stamped on the interaction row at
        # creation time. Fall back to the project chain only for legacy rows.
        effective_location_id = (interaction["interaction_location_id"]
                                 or _project_location_id(conn, interaction["project_id"]))
        new_rid, canonical = _upsert_respondent(
            conn, company_id, effective_location_id, respondent_name_final,
        )
        _link_interaction_respondent(conn, interaction_id, new_rid)
        respondent_id = new_rid
        respondent_name_final = canonical
        original = existing_original if existing_original is not None else existing_overall
        conn.execute(
            q("""UPDATE interactions SET
                    interaction_regrade_count         = interaction_regrade_count + 1,
                    interaction_regraded_with_context = TRUE,
                    interaction_original_score        = COALESCE(?, interaction_original_score)
                 WHERE interaction_id = ?"""),
            (original, interaction_id),
        )
        write_audit_log(
            current_user.user_id, ACTION_REGRADED, ENTITY_INTERACTION,
            interaction_id,
            metadata={"mode": "reviewer_context", "total_score": total_score,
                      "final_status_id": final_status,
                      "location_id": effective_location_id},
            conn=conn,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Persisting context-regrade failed for interaction %s", interaction_id)
        return _err("Saving grade failed. Please try again.", 500)
    finally:
        conn.close()

    update_performance_report_async(
        interaction_id, company_id,
        respondent_user_id=interaction["respondent_user_id"],
        respondent_id=respondent_id,
    )
    location_id_for_intel = _project_location_id_via_either(interaction["project_id"])
    if location_id_for_intel:
        compute_location_intel_async(location_id_for_intel, company_id)

    response = _build_grade_response(
        interaction_id, grade_result, total_score, flags, transcript=transcript
    )
    response["respondent_id"] = respondent_id
    response["respondent_name"] = respondent_name_final
    return jsonify(response)


# ═══════════════════════════════════════════════════════════════
# GET /api/interactions/<id>/audio  —  stream audio for playback
# ═══════════════════════════════════════════════════════════════


_AUDIO_MIME = {
    ".mp3":  "audio/mpeg",
    ".mp4":  "audio/mp4",
    ".m4a":  "audio/mp4",
    ".wav":  "audio/wav",
    ".aac":  "audio/aac",
    ".ogg":  "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


@interactions_bp.route("/interactions/<int:interaction_id>/audio", methods=["GET"])
@login_required
def get_audio(interaction_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT i.interaction_audio_url, i.interaction_audio_data
                 FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 WHERE i.interaction_id = ? AND p.company_id = ?
                   AND i.interaction_deleted_at IS NULL"""),
            (interaction_id, company_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return _err("Interaction not found", 404)

    audio_url = row["interaction_audio_url"]
    if IS_POSTGRES:
        blob = row["interaction_audio_data"]
        if not blob:
            return _err("No audio stored for this interaction", 404)
        # psycopg2 returns memoryview for BYTEA → convert to bytes
        data = bytes(blob)
        return send_file(
            io.BytesIO(data),
            mimetype="audio/mpeg",
            download_name=f"interaction_{interaction_id}.mp3",
        )
    # SQLite: serve from filesystem path stored in interaction_audio_url
    if not audio_url:
        return _err("No audio stored for this interaction", 404)
    path = Path(audio_url)
    if not path.exists():
        return _err("Audio file missing on disk", 404)
    mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return send_file(str(path), mimetype=mime, download_name=path.name)


# ═══════════════════════════════════════════════════════════════
# DELETE /api/interactions/<id>  —  soft delete
# ═══════════════════════════════════════════════════════════════


@interactions_bp.route("/interactions/<int:interaction_id>", methods=["DELETE"])
@login_required
@role_required("admin", "super_admin")
def delete_interaction(interaction_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        interaction = _get_interaction_in_company(conn, interaction_id, company_id)
        if not interaction or interaction["interaction_deleted_at"] is not None:
            return _err("Interaction not found", 404)

        if IS_POSTGRES:
            conn.execute(
                "UPDATE interactions SET interaction_deleted_at = NOW() "
                "WHERE interaction_id = %s",
                (interaction_id,),
            )
        else:
            conn.execute(
                "UPDATE interactions SET interaction_deleted_at = CURRENT_TIMESTAMP "
                "WHERE interaction_id = ?",
                (interaction_id,),
            )
        write_audit_log(
            current_user.user_id, ACTION_DELETED, ENTITY_INTERACTION,
            interaction_id, conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
