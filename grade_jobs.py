"""
grade_jobs.py — Async grading queue for split-pane workflow.

The /api/grade-jobs endpoint enqueues a call (saving audio bytes to disk +
inserting an interaction row + a grade_jobs row) and fires a daemon thread
that does the actual transcribe-then-grade work in the background. Frontend
polls GET /api/grade-jobs to track phase transitions.

Public entry points:
    enqueue_grade_job(...)            — request-context safe; returns IDs
    process_grade_job_async(job_id)   — fires daemon thread, never raises

The daemon thread reuses interactions_routes._grade_and_persist for the
final write (passing actor_user_id explicitly since current_user is unavailable
outside the request context). Failure paths set gj_status='failed' with a
meaningful error and leave the interaction at status_id=45 (submitted) so
it's visible for manual retry.

Audio handling: the request handler writes audio bytes to a tempfile under
_AUDIO_DIR/queue/. The daemon transcribes from that path, then calls the
existing _save_audio + _save_transcript_and_audio machinery to persist
audio onto the interaction (BYTEA on PG, file path on SQLite). The queue
file is deleted at the end of processing.
"""

import logging
import os
import threading
from datetime import date
from pathlib import Path

import grader
from db import IS_POSTGRES, get_conn, q
from helpers import (
    check_rate_limit, increment_usage, load_active_hints,
)
from interactions_routes import (
    STATUS_GRADING, STATUS_SUBMITTED, STATUS_TRANSCRIBING,
    _AUDIO_DIR,
    _GradingAPIError,
    _grade_and_persist,
    _insert_interaction_row,
    _items_to_criteria,
    _load_rubric_group,
    _load_rubric_items,
    _save_audio,
    _save_transcript_and_audio,
    _update_interaction_status,
)

logger = logging.getLogger(__name__)


_QUEUE_AUDIO_DIR = _AUDIO_DIR / "queue"


# ── Helpers ─────────────────────────────────────────────────────


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _set_job(conn, job_id, *, status=None, error=None,
             phase_started=False, dismissed=False, interaction_id=None):
    """Update one or more grade_jobs columns. Caller commits."""
    parts, params = [], []
    if status is not None:
        parts.append("gj_status = ?")
        params.append(status)
    if error is not None:
        parts.append("gj_error = ?")
        params.append(error)
    if phase_started:
        if IS_POSTGRES:
            parts.append("gj_phase_started_at = NOW()")
        else:
            parts.append("gj_phase_started_at = CURRENT_TIMESTAMP")
    if dismissed:
        if IS_POSTGRES:
            parts.append("gj_dismissed_at = NOW()")
        else:
            parts.append("gj_dismissed_at = CURRENT_TIMESTAMP")
    if interaction_id is not None:
        parts.append("interaction_id = ?")
        params.append(interaction_id)
    if not parts:
        return
    params.append(job_id)
    conn.execute(
        q(f"UPDATE grade_jobs SET {', '.join(parts)} WHERE grade_job_id = ?"),
        params,
    )


def _set_job_committed(job_id, **kwargs):
    """One-shot version of _set_job that opens its own connection + commits."""
    conn = get_conn()
    try:
        _set_job(conn, job_id, **kwargs)
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    finally:
        conn.close()


def _mark_failed(job_id, message):
    """Best-effort failure write. Swallows secondary failures."""
    try:
        _set_job_committed(job_id, status="failed", error=message)
    except Exception:
        logger.exception("Could not mark grade_job %s as failed", job_id)


def _queue_audio_path(job_id, ext):
    _QUEUE_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return _QUEUE_AUDIO_DIR / f"queued_{job_id}{ext or '.bin'}"


def _delete_queue_audio(path):
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        logger.warning("Could not delete queued audio %s", path, exc_info=True)


# ── Enqueue (request-context safe) ─────────────────────────────


def enqueue_grade_job(
    *,
    company_id,
    submitted_by_user_id,
    project_id,
    location_id,
    audio_bytes,
    audio_ext,
    caller_user_id=None,
    respondent_user_id=None,
    interaction_date=None,
    campaign_id=None,
    call_start_time=None,
    call_end_time=None,
    call_duration_seconds=None,
):
    """Create the interaction row + grade_jobs row + persist audio bytes.

    Returns (grade_job_id, interaction_id). Raises on validation failure.
    Caller has already verified project + location ownership and rate limits.
    """
    conn = get_conn()
    try:
        interaction_id = _insert_interaction_row(
            conn,
            project_id=project_id,
            caller_user_id=caller_user_id,
            respondent_user_id=respondent_user_id,
            location_id=location_id,
            campaign_id=campaign_id,
            interaction_date=interaction_date or date.today(),
            status_id=STATUS_SUBMITTED,
            call_start_time=call_start_time,
            call_end_time=call_end_time,
            call_duration_seconds=call_duration_seconds,
            set_uploaded_at=True,
        )

        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO grade_jobs
                       (company_id, submitted_by_user_id, interaction_id, gj_status)
                   VALUES (%s, %s, %s, 'queued')
                   RETURNING grade_job_id""",
                (company_id, submitted_by_user_id, interaction_id),
            )
            job_id = cur.fetchone()["grade_job_id"]
        else:
            conn.execute(
                """INSERT INTO grade_jobs
                       (company_id, submitted_by_user_id, interaction_id, gj_status)
                   VALUES (?, ?, ?, 'queued')""",
                (company_id, submitted_by_user_id, interaction_id),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()

    # Save audio to queue directory; the daemon owns it from here.
    try:
        path = _queue_audio_path(job_id, audio_ext)
        path.write_bytes(audio_bytes)
    except Exception:
        # If we can't even land the bytes, mark the job failed and bail —
        # we still return the IDs so the UI can surface the error.
        logger.exception("Could not save queued audio for job %s", job_id)
        _mark_failed(job_id, "Could not save audio for processing.")

    return job_id, interaction_id


# ── Public entry points (daemon-side) ──────────────────────────


def process_grade_job_async(job_id, actor_user_id):
    """Fire-and-forget background processing. Never raises."""
    t = threading.Thread(
        target=_process_safely,
        args=(job_id, actor_user_id),
        daemon=True,
    )
    t.start()


def _process_safely(job_id, actor_user_id):
    try:
        _process(job_id, actor_user_id)
    except Exception as exc:
        logger.exception("grade_job %s processing crashed", job_id)
        _mark_failed(job_id, f"Unhandled error: {exc}")


# ── Implementation ─────────────────────────────────────────────


def _load_job_and_interaction(job_id):
    """Returns (job_dict, interaction_dict) or (None, None) if missing."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT j.grade_job_id, j.company_id, j.submitted_by_user_id,
                        j.interaction_id, j.gj_status,
                        i.project_id,
                        i.respondent_user_id,
                        i.interaction_location_id
                   FROM grade_jobs j
                   JOIN interactions i ON i.interaction_id = j.interaction_id
                  WHERE j.grade_job_id = ?"""),
            (job_id,),
        )
        row = _row_to_dict(cur.fetchone())
        return row
    finally:
        conn.close()


def _load_criteria_for_project(project_id):
    """Returns (criteria, script_text, context_text, grade_target).
    Mirrors the no-override branch in interactions_routes.submit_grade
    (script_text + context_text default to None on the standard path)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT rubric_group_id FROM projects WHERE project_id = ?"),
            (project_id,),
        )
        prow = _row_to_dict(cur.fetchone())
        if not prow or not prow.get("rubric_group_id"):
            return None, None, None, "respondent"
        rg_id = prow["rubric_group_id"]
        rubric_group = _load_rubric_group(conn, rg_id)
        items = _load_rubric_items(conn, rg_id)
        criteria = _items_to_criteria(items)
        grade_target = (rubric_group or {}).get("rg_grade_target") or "respondent"
        return criteria, None, None, grade_target
    finally:
        conn.close()


def _process(job_id, actor_user_id):
    job = _load_job_and_interaction(job_id)
    if not job:
        logger.warning("grade_job %s: row not found", job_id)
        return

    if job["gj_status"] not in ("queued", "failed"):
        logger.info("grade_job %s: skipping (status=%s)", job_id, job["gj_status"])
        return

    interaction_id = job["interaction_id"]
    company_id = job["company_id"]
    project_id = job["project_id"]
    location_id = job["interaction_location_id"]
    respondent_user_id = job["respondent_user_id"]

    # Load rubric criteria from the project. Rubric override on /api/grade-jobs
    # is intentionally not supported in v1 — followup if needed.
    try:
        criteria, script_text, context_text, grade_target = _load_criteria_for_project(project_id)
        if not criteria:
            _mark_failed(job_id, "Project rubric has no items.")
            _set_job_committed_interaction_status(interaction_id, STATUS_SUBMITTED)
            return
    except Exception as e:
        logger.exception("Could not load criteria for grade_job %s", job_id)
        _mark_failed(job_id, f"Loading rubric failed: {e}")
        return

    # Find the queued audio file by scanning for any extension.
    queue_path = _find_queue_audio(job_id)
    if not queue_path or not queue_path.exists():
        _mark_failed(job_id, "Queued audio file not found on disk.")
        _set_job_committed_interaction_status(interaction_id, STATUS_SUBMITTED)
        return

    try:
        # ── Phase: transcribing ──
        _set_job_committed(job_id, status="transcribing", phase_started=True)
        _set_job_committed_interaction_status(interaction_id, STATUS_TRANSCRIBING)

        hints = load_active_hints(company_id)
        try:
            transcript = grader.transcribe(str(queue_path), keyterms_prompt=hints)
        except grader.EmptyTranscriptError:
            _mark_failed(job_id, "Transcription returned no audible content.")
            _set_job_committed_interaction_status(interaction_id, STATUS_SUBMITTED)
            return
        except Exception as e:
            logger.exception("Transcription failed for grade_job %s", job_id)
            _mark_failed(job_id, f"Transcription failed: {e}")
            _set_job_committed_interaction_status(interaction_id, STATUS_SUBMITTED)
            return

        increment_usage(company_id, "assemblyai")

        # Persist transcript + audio onto the interaction row.
        try:
            audio_bytes = queue_path.read_bytes()
        except Exception as e:
            _mark_failed(job_id, f"Could not read audio for storage: {e}")
            _set_job_committed_interaction_status(interaction_id, STATUS_SUBMITTED)
            return

        audio_url, audio_data = _save_audio(interaction_id, audio_bytes, queue_path.suffix)
        conn = get_conn()
        try:
            _save_transcript_and_audio(
                conn, interaction_id,
                transcript=transcript,
                audio_url=audio_url,
                audio_bytes=audio_data,
                status_id=STATUS_GRADING,
            )
            conn.commit()
        finally:
            conn.close()

        # ── Phase: grading ──
        _set_job_committed(job_id, status="grading", phase_started=True)

        try:
            _grade_and_persist(
                interaction_id=interaction_id,
                company_id=company_id,
                project_id=project_id,
                respondent_user_id=respondent_user_id,
                location_id=location_id,
                transcript=transcript,
                criteria=criteria,
                script_text=script_text,
                context_text=context_text,
                grade_target=grade_target,
                is_initial_grade=True,
                actor_user_id=actor_user_id,
            )
        except _GradingAPIError as e:
            # _grade_and_persist already rolled the interaction back to SUBMITTED.
            _mark_failed(job_id, e.message)
            return
        except Exception as e:
            logger.exception("Grade-and-persist crashed for grade_job %s", job_id)
            _set_job_committed_interaction_status(interaction_id, STATUS_SUBMITTED)
            _mark_failed(job_id, f"Grading crashed: {e}")
            return

        # ── Phase: graded ──
        _set_job_committed(job_id, status="graded", error="")

    finally:
        _delete_queue_audio(queue_path)


def _find_queue_audio(job_id):
    """Locate the queued audio file by job_id (any extension)."""
    if not _QUEUE_AUDIO_DIR.exists():
        return None
    for p in _QUEUE_AUDIO_DIR.glob(f"queued_{job_id}.*"):
        return p
    return None


def _set_job_committed_interaction_status(interaction_id, status_id):
    """Update interactions.status_id in its own transaction. Best-effort."""
    try:
        conn = get_conn()
        try:
            _update_interaction_status(conn, interaction_id, status_id)
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not set interaction %s status to %s",
                         interaction_id, status_id)
