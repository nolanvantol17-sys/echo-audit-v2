"""
voip/processor.py — Background grading pipeline for queued VoIP calls.

Public entry points:
    process_voip_call(voip_queue_id)         — synchronous (used by tests / CLI)
    process_voip_call_async(voip_queue_id)   — fires a daemon thread

Flow for a single queue item:

    1. Load queue row + company's voip_config
    2. Mark queue row 'processing'
    3. Pick the company's most-recently-created active project + rubric group
    4. Download the recording (if URL set) using provider-specific auth,
       store bytes in voip_queue_recording_data
    5. Create an interactions row with status_id = 40 (transcribing)
    6. Transcribe → 42 (grading) → grade → persist rubric scores + clarifying Qs
    7. Interaction status_id = 43 (graded), queue row status = 'graded',
       voip_queue_interaction_id set
    8. Any exception: queue row = 'failed' + error message; interaction (if any)
       rolled back to status 45 (pending) so it's visible for manual retry.

The processor never raises — all errors end up in voip_queue_error so the
webhook that triggered us can continue to 200 OK and never see a retry.
"""

import io
import json
import logging
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import Optional

import grader
from db import IS_POSTGRES, get_conn, q
from voip.credentials import decrypt_credentials

logger = logging.getLogger(__name__)


# Interaction status IDs — must match statuses seed in db.py.
STATUS_TRANSCRIBING           = 40
STATUS_AWAITING_CLARIFICATION = 41
STATUS_GRADING                = 42
STATUS_GRADED                 = 43
STATUS_SUBMITTED              = 45


# ── Public entry points ────────────────────────────────────────


def process_voip_call_async(voip_queue_id: int) -> None:
    """Fire-and-forget background processing. Never raises."""
    t = threading.Thread(
        target=_process_safely,
        args=(voip_queue_id,),
        daemon=True,
    )
    t.start()


def process_voip_call(voip_queue_id: int) -> None:
    """Synchronous version. Never raises — all errors are logged + recorded on
    the queue row."""
    _process_safely(voip_queue_id)


def _process_safely(voip_queue_id: int) -> None:
    try:
        _process(voip_queue_id)
    except Exception as exc:
        logger.exception("VoIP queue %s processing crashed", voip_queue_id)
        _mark_failed(voip_queue_id, f"Unhandled error: {exc}")


# ── Implementation ─────────────────────────────────────────────


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _load_queue_and_config(queue_id):
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT * FROM voip_call_queue WHERE voip_queue_id = ?"),
            (queue_id,),
        )
        queue_row = _row_to_dict(cur.fetchone())
        if not queue_row:
            return None, None

        cur = conn.execute(
            q("""SELECT * FROM voip_configs
                 WHERE company_id = ? AND voip_config_is_active = TRUE"""),
            (queue_row["company_id"],),
        )
        config_row = _row_to_dict(cur.fetchone())
        return queue_row, config_row
    finally:
        conn.close()


def _active_project(company_id):
    """Return the company's most recently created active project + its rubric group."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT p.*, rg.rg_grade_target
                 FROM projects p
                 JOIN rubric_groups rg ON rg.rubric_group_id = p.rubric_group_id
                 WHERE p.company_id = ?
                   AND p.status_id = 1
                   AND p.project_deleted_at IS NULL
                   AND rg.rg_deleted_at IS NULL
                 ORDER BY p.project_created_at DESC, p.project_id DESC
                 LIMIT 1"""),
            (company_id,),
        )
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def _load_rubric_items(rubric_group_id):
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT rubric_item_id, ri_name, ri_score_type, ri_weight,
                        ri_scoring_guidance, ri_order
                 FROM rubric_items
                 WHERE rubric_group_id = ? AND ri_deleted_at IS NULL
                 ORDER BY ri_order ASC, rubric_item_id ASC"""),
            (rubric_group_id,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# Maps match interactions_routes._SCORE_TYPE_V2_TO_V1 — kept in sync deliberately.
_V2_TO_V1 = {
    "out_of_10":      "numeric",
    "yes_no":         "yes_no",
    "yes_no_pending": "yes_no_pending",
}
_V1_TO_V2 = {v: k for k, v in _V2_TO_V1.items()}


def _items_to_criteria(items):
    criteria = []
    for it in items:
        criteria.append({
            "name":             it["ri_name"],
            "type":             _V2_TO_V1.get(it["ri_score_type"], "numeric"),
            "scale":            10,
            "weight":           float(it["ri_weight"]) if it["ri_weight"] is not None else 1.0,
            "scoring_guidance": it.get("ri_scoring_guidance") or "",
            "_rubric_item_id":  it["rubric_item_id"],
        })
    return criteria


def _score_to_numeric(value, score_type) -> float:
    if value is None:
        return 0.0
    if score_type == "numeric":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    s = str(value).strip().lower()
    if s == "yes":
        return 10.0
    if s == "no":
        return 0.0
    return 5.0


def _download_recording(url: str, provider: str, credentials: dict) -> Optional[bytes]:
    """Fetch the recording bytes. Auth rules per provider (best-effort):

    - aircall: Basic auth (api_id / api_token)
    - zoom_phone: OAuth token acquired via account/client creds — when we have
      a `recording_access_token` field we use it; otherwise we try unauth first.
    - ringcentral: OAuth bearer with the server — we try unauth first; if the
      client supplied an `access_token` field we send it.
    - dialpad, 8x8, generic: unauthenticated or pre-signed URL.

    Production deployments should replace this best-effort block with a proper
    OAuth client per provider. Documented as a follow-up.
    """
    if not url:
        return None

    import requests

    headers = {}
    auth = None
    prov = (provider or "").lower()

    if prov == "aircall":
        api_id = credentials.get("api_id")
        api_token = credentials.get("api_token")
        if api_id and api_token:
            auth = (api_id, api_token)
    elif prov == "zoom_phone":
        token = credentials.get("recording_access_token") or credentials.get("access_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif prov == "ringcentral":
        token = credentials.get("access_token")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    # dialpad / eight_by_eight / generic_webhook: URL usually pre-signed.

    try:
        resp = requests.get(url, headers=headers, auth=auth, timeout=60, stream=True)
        resp.raise_for_status()
        # Cap at 100 MB to guard against runaway responses.
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > 100 * 1024 * 1024:
                raise RuntimeError("recording exceeds 100 MB limit")
            chunks.append(chunk)
        return b"".join(chunks)
    except Exception as e:
        logger.exception("Failed to download recording for provider %s", prov)
        raise RuntimeError(f"Recording download failed: {e}") from e


def _set_queue_status(queue_id, status, *, error=None, interaction_id=None,
                      recording_data=None):
    conn = get_conn()
    try:
        parts = ["voip_queue_status = ?"]
        params = [status]
        if error is not None:
            parts.append("voip_queue_error = ?")
            params.append(error)
        if interaction_id is not None:
            parts.append("voip_queue_interaction_id = ?")
            params.append(interaction_id)
        if recording_data is not None:
            parts.append("voip_queue_recording_data = ?")
            params.append(recording_data)
        params.append(queue_id)
        conn.execute(
            q(f"UPDATE voip_call_queue SET {', '.join(parts)} "
              "WHERE voip_queue_id = ?"),
            params,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _mark_failed(queue_id, error_message):
    """Best-effort failure write. Swallows secondary failures so the thread
    never crashes on a logging write."""
    try:
        _set_queue_status(queue_id, "failed", error=error_message)
    except Exception:
        logger.exception("Could not mark queue %s as failed", queue_id)


def _create_interaction(project_id, call_date):
    conn = get_conn()
    try:
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO interactions
                       (project_id, caller_user_id, respondent_user_id,
                        interaction_date, interaction_submitted_at, status_id)
                   VALUES (%s, NULL, NULL, %s, NOW(), %s)
                   RETURNING interaction_id""",
                (project_id, call_date or date.today(), STATUS_TRANSCRIBING),
            )
            interaction_id = cur.fetchone()["interaction_id"]
        else:
            conn.execute(
                """INSERT INTO interactions
                       (project_id, caller_user_id, respondent_user_id,
                        interaction_date, interaction_submitted_at, status_id)
                   VALUES (?, NULL, NULL, ?, CURRENT_TIMESTAMP, ?)""",
                (project_id, call_date or date.today(), STATUS_TRANSCRIBING),
            )
            interaction_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
        conn.commit()
        return interaction_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_interaction_status(interaction_id, status_id):
    conn = get_conn()
    try:
        conn.execute(
            q("UPDATE interactions SET status_id = ? WHERE interaction_id = ?"),
            (status_id, interaction_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _persist_grade(interaction_id, *, grade_result, criteria, total_score,
                   flags, transcript, audio_url, audio_bytes, final_status_id):
    """Write transcript + scores + clarifying questions in one transaction."""
    conn = get_conn()
    try:
        scores = grade_result.get("scores") or {}
        explanations = grade_result.get("explanations") or {}

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
                    status_id                      = ?
                 WHERE interaction_id = ?"""),
            (
                transcript,
                audio_url,
                audio_bytes,
                total_score,
                flags,
                grade_result.get("strengths") or "",
                grade_result.get("weaknesses") or "",
                grade_result.get("overall_assessment") or "",
                grade_result.get("responder_name"),
                final_status_id,
                interaction_id,
            ),
        )

        # snapshot rubric scores
        conn.execute(
            q("DELETE FROM interaction_rubric_scores WHERE interaction_id = ?"),
            (interaction_id,),
        )
        for c in criteria:
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
                    c.get("_rubric_item_id"),
                    c["name"],
                    _V1_TO_V2.get(c.get("type", "numeric"), "out_of_10"),
                    float(c.get("weight", 1.0)),
                    c.get("scoring_guidance") or None,
                    score_value,
                    explanation,
                ),
            )

        # clarifying questions
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

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _save_audio_url(interaction_id):
    """Pick the audio_url marker consistent with Phase 3's storage scheme."""
    if IS_POSTGRES:
        return f"db://interactions/{interaction_id}"
    return f"voip://interaction_{interaction_id}"


def _process(voip_queue_id: int) -> None:
    queue_row, config_row = _load_queue_and_config(voip_queue_id)
    if not queue_row:
        logger.warning("voip queue %s: row not found", voip_queue_id)
        return

    if queue_row["voip_queue_status"] not in ("pending", "failed"):
        # Already processed or mid-flight — don't clobber.
        logger.info("voip queue %s: skipping (status=%s)",
                    voip_queue_id, queue_row["voip_queue_status"])
        return

    # Mark processing (reset any prior error)
    _set_queue_status(voip_queue_id, "processing", error="")

    if not config_row:
        _mark_failed(voip_queue_id, "VoIP config missing or inactive for this company.")
        return

    company_id = queue_row["company_id"]

    # Resolve active project + rubric
    project = _active_project(company_id)
    if not project:
        _mark_failed(voip_queue_id,
                     "No active project with a rubric group found for this company.")
        return

    items = _load_rubric_items(project["rubric_group_id"])
    if not items:
        _mark_failed(voip_queue_id,
                     "Active project's rubric group has no items.")
        return
    criteria = _items_to_criteria(items)

    # Credentials — decrypt only if we need them.
    try:
        credentials = decrypt_credentials(config_row["voip_config_credentials"])
    except Exception as e:
        _mark_failed(voip_queue_id, f"Credentials unavailable: {e}")
        return

    # Download recording if a URL is set AND we don't already have bytes.
    audio_bytes = queue_row.get("voip_queue_recording_data")
    recording_url = queue_row.get("voip_queue_recording_url")
    try:
        if not audio_bytes and recording_url:
            audio_bytes = _download_recording(
                recording_url, queue_row["voip_queue_provider"], credentials,
            )
    except Exception as e:
        _mark_failed(voip_queue_id, str(e))
        return

    if not audio_bytes:
        _mark_failed(voip_queue_id, "No audio available to transcribe.")
        return

    # Normalize memoryview → bytes (psycopg2 returns memoryview for BYTEA)
    if isinstance(audio_bytes, memoryview):
        audio_bytes = bytes(audio_bytes)

    # Persist the downloaded bytes back onto the queue row so reruns don't re-download.
    try:
        _set_queue_status(voip_queue_id, "processing", recording_data=audio_bytes)
    except Exception:
        logger.exception("Could not persist downloaded bytes; continuing anyway")

    # Write to a temp file for AssemblyAI
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    interaction_id = None
    try:
        tmp.write(audio_bytes)
        tmp.close()

        # Create interaction row up front so status transitions are observable.
        interaction_id = _create_interaction(
            project["project_id"], queue_row["voip_queue_call_date"],
        )

        # Transcribe
        try:
            transcript = grader.transcribe(tmp.name)
        except Exception as e:
            _set_interaction_status(interaction_id, STATUS_SUBMITTED)
            _mark_failed(voip_queue_id, f"Transcription failed: {e}")
            return

        # Grade
        _set_interaction_status(interaction_id, STATUS_GRADING)
        try:
            grade_result = grader.grade_with_claude(
                transcript=transcript,
                context_answers=None,
                rubric_criteria=criteria,
                rubric_script=None,
                rubric_context=None,
                grade_target=project.get("rg_grade_target") or "respondent",
            )
        except Exception as e:
            _set_interaction_status(interaction_id, STATUS_SUBMITTED)
            _mark_failed(voip_queue_id, f"Grading failed: {e}")
            return

        scores = grade_result.get("scores") or {}
        total_score = grader.calculate_total(scores, criteria)
        flags = grader.build_flags(scores, criteria)
        has_cqs = bool(grade_result.get("clarifying_questions"))
        final_status = STATUS_AWAITING_CLARIFICATION if has_cqs else STATUS_GRADED

        audio_url = _save_audio_url(interaction_id)
        # On SQLite we don't persist the bytes on the interaction; keep them
        # on the queue row (already done above).
        audio_bytes_for_interaction = audio_bytes if IS_POSTGRES else None

        try:
            _persist_grade(
                interaction_id,
                grade_result=grade_result,
                criteria=criteria,
                total_score=total_score,
                flags=flags,
                transcript=transcript,
                audio_url=audio_url,
                audio_bytes=audio_bytes_for_interaction,
                final_status_id=final_status,
            )
        except Exception as e:
            _set_interaction_status(interaction_id, STATUS_SUBMITTED)
            _mark_failed(voip_queue_id, f"Persisting grade failed: {e}")
            return

        # Success path
        _set_queue_status(
            voip_queue_id, "graded",
            error="", interaction_id=interaction_id,
        )

    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except Exception:
            pass
