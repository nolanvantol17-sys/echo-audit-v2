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
    6. Transcribe → 42 (grading) → grade → persist rubric scores
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
from helpers import load_active_hints
from voip.audio_fetcher import fetch_and_store_audio_async
from voip.classifier import classify_call
from voip.credentials import decrypt_credentials
# Cross-module helpers — voip processor mirrors steps 2-4 of the
# interactions_routes.py grading flow so AI shop grades land in
# performance_reports the same way as web-form grades. Load order in
# app.py: interactions_routes (line 27) + performance_reports (line 28)
# both load before voip_routes (line 32) imports this module, so a
# top-level import is safe — no circular risk verified.
from interactions_routes import _link_interaction_respondent, _upsert_respondent
from performance_reports import update_performance_report_async

logger = logging.getLogger(__name__)


# Interaction status IDs — must match statuses seed in db.py.
STATUS_TRANSCRIBING = 40
STATUS_GRADING      = 42
STATUS_GRADED       = 43
STATUS_NO_ANSWER    = 44
STATUS_SUBMITTED    = 45


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


def _project_by_id(project_id, company_id):
    """Fetch a specific project + rg_grade_target, scoped to company.

    Used by the attribution path (ElevenLabs supplies an explicit project_id
    via dynamic_variables). Re-verifies tenancy at process time as cheap
    insurance against the project being deleted/archived between queue
    insert (when the route layer first verified) and process pickup.
    Returns None if the project no longer belongs to the given company,
    is deleted, or its rubric group is deleted.
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT p.*, rg.rg_grade_target
                   FROM projects p
                   JOIN rubric_groups rg ON rg.rubric_group_id = p.rubric_group_id
                  WHERE p.project_id = ?
                    AND p.company_id = ?
                    AND p.project_deleted_at IS NULL
                    AND rg.rg_deleted_at IS NULL"""),
            (project_id, company_id),
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
        # Mirrors interactions_routes.py:_score_to_numeric (Yes→9.9). Both
        # paths must agree because interaction_rubric_scores.chk_irs_score_value
        # caps at <= 9.9 (see schema.sql). Pre-2026-05-01 this was 10.0; the
        # constraint tightening in commit 68432b6 made that an instant fail.
        return 9.9
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
    _link_to_scheduled_call(queue_id, "failed")


def _link_to_scheduled_call(voip_queue_id, terminal_status):
    """Bump the matching scheduled_calls row to a terminal status, looking up
    the conversation_id from voip_call_queue in a single atomic UPDATE.

    Best-effort — never raises out. Silent no-op when:
      - voip_queue_id is None/0
      - queue row's voip_queue_call_id is NULL (legacy provider w/o
        conversation_id)
      - no scheduled_calls row matches (call wasn't scheduled by Echo
        Audit — e.g. inbound webhook from a cold call)
      - matching scheduled_calls row is already terminal (sc_status !=
        'initiated' — idempotent against double-fires or stale state)
    """
    if not voip_queue_id:
        return
    conn = get_conn()
    try:
        if IS_POSTGRES:
            conn.execute(
                """UPDATE scheduled_calls
                      SET sc_status = %s, sc_completed_at = NOW()
                    WHERE sc_status = 'initiated'
                      AND sc_conversation_id = (
                          SELECT voip_queue_call_id FROM voip_call_queue
                           WHERE voip_queue_id = %s
                      )""",
                (terminal_status, voip_queue_id),
            )
        else:
            conn.execute(
                """UPDATE scheduled_calls
                      SET sc_status = ?, sc_completed_at = CURRENT_TIMESTAMP
                    WHERE sc_status = 'initiated'
                      AND sc_conversation_id = (
                          SELECT voip_queue_call_id FROM voip_call_queue
                           WHERE voip_queue_id = ?
                      )""",
                (terminal_status, voip_queue_id),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(
            "[ai_shop] _link_to_scheduled_call failed (queue_id=%s status=%s)",
            voip_queue_id, terminal_status, exc_info=True,
        )
    finally:
        conn.close()


def _set_interaction_call_start_from_schedule(voip_queue_id):
    """Set interactions.interaction_call_start_time from the matching
    scheduled_calls.sc_requested_at (AI Shop calls only). Idempotent —
    only writes when interaction_call_start_time IS NULL. Best-effort,
    never raises out.

    Why: voip processor's _create_interaction() doesn't populate
    interaction_call_start_time, so AI shop graded rows fall through
    EA.formatCallTime()'s `startTime || uploadedAt` check and render
    as "—" on every display surface. Setting it here at the point we've
    just confirmed the scheduled_calls linkage gives every surface a
    real timestamp without touching any API/template/JS.
    """
    if not voip_queue_id:
        return
    conn = get_conn()
    try:
        if IS_POSTGRES:
            conn.execute(
                """UPDATE interactions
                      SET interaction_call_start_time = sc.sc_requested_at
                     FROM voip_call_queue vcq
                     JOIN scheduled_calls sc
                       ON sc.sc_conversation_id = vcq.voip_queue_call_id
                    WHERE vcq.voip_queue_id = %s
                      AND interactions.interaction_id = vcq.voip_queue_interaction_id
                      AND interactions.interaction_call_start_time IS NULL""",
                (voip_queue_id,),
            )
        else:
            conn.execute(
                """UPDATE interactions
                      SET interaction_call_start_time = (
                          SELECT sc.sc_requested_at
                            FROM voip_call_queue vcq
                            JOIN scheduled_calls sc
                              ON sc.sc_conversation_id = vcq.voip_queue_call_id
                           WHERE vcq.voip_queue_id = ?
                      )
                    WHERE interaction_id = (
                          SELECT voip_queue_interaction_id
                            FROM voip_call_queue WHERE voip_queue_id = ?
                      )
                      AND interaction_call_start_time IS NULL""",
                (voip_queue_id, voip_queue_id),
            )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(
            "[ai_shop] _set_interaction_call_start_from_schedule failed "
            "(queue_id=%s)", voip_queue_id, exc_info=True,
        )
    finally:
        conn.close()


def _company_id_for_interaction(interaction_id):
    """Look up company_id via the project — used by sites that need
    per-tenant context after the interaction is created."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT p.company_id
                   FROM interactions i
                   JOIN projects p ON p.project_id = i.project_id
                  WHERE i.interaction_id = ?"""),
            (interaction_id,),
        )
        row = cur.fetchone()
        return (dict(row).get("company_id") if row else None)
    finally:
        conn.close()


def _location_id_for_interaction(interaction_id):
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT interaction_location_id FROM interactions WHERE interaction_id = ?"),
            (interaction_id,),
        )
        row = cur.fetchone()
        return (dict(row).get("interaction_location_id") if row else None)
    finally:
        conn.close()


def _attach_respondent_and_fire_report(interaction_id, company_id,
                                       respondent_name, location_id):
    """Upsert + link a respondent for the freshly-graded interaction, then
    fire the (async) performance_report update. Mirrors steps 2-4 of the
    interactions_routes.py grading flow (line 748-774).

    Best-effort: the respondent upsert is wrapped so a failure here doesn't
    roll back the already-persisted grade. The async report fire is a no-op
    if respondent_id ends up None (matches update_performance_report_async's
    own guard at performance_reports.py line 299-302).
    """
    if not interaction_id or not company_id:
        return None
    respondent_id = None
    conn = get_conn()
    try:
        respondent_id, _canonical = _upsert_respondent(
            conn, company_id, location_id, respondent_name,
        )
        if respondent_id is not None:
            _link_interaction_respondent(conn, interaction_id, respondent_id)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(
            "[ai_shop] respondent upsert/link failed (interaction=%s)",
            interaction_id, exc_info=True,
        )
    finally:
        conn.close()
    update_performance_report_async(
        interaction_id, company_id,
        respondent_user_id=None,        # voip flow has no known-user subject
        respondent_id=respondent_id,
    )
    return respondent_id


def _create_interaction(project_id, call_date, *,
                        location_id=None, campaign_id=None,
                        caller_user_id=None, call_duration_seconds=None):
    """Create a new interaction row with status=transcribing.

    Optional kw-only attribution comes from the queue row when the upstream
    supplied it (ElevenLabs dynamic_variables). Defaults of None preserve
    backward compatibility with legacy VoIP providers that go through
    _active_project and don't carry attribution.
    """
    conn = get_conn()
    try:
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO interactions
                       (project_id, caller_user_id, respondent_user_id,
                        interaction_location_id, campaign_id,
                        interaction_call_duration_seconds,
                        interaction_date, interaction_submitted_at, status_id)
                   VALUES (%s, %s, NULL, %s, %s, %s, %s, NOW(), %s)
                   RETURNING interaction_id""",
                (project_id, caller_user_id,
                 location_id, campaign_id, call_duration_seconds,
                 call_date or date.today(), STATUS_TRANSCRIBING),
            )
            interaction_id = cur.fetchone()["interaction_id"]
        else:
            conn.execute(
                """INSERT INTO interactions
                       (project_id, caller_user_id, respondent_user_id,
                        interaction_location_id, campaign_id,
                        interaction_call_duration_seconds,
                        interaction_date, interaction_submitted_at, status_id)
                   VALUES (?, ?, NULL, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                (project_id, caller_user_id,
                 location_id, campaign_id, call_duration_seconds,
                 call_date or date.today(), STATUS_TRANSCRIBING),
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
    """Write transcript + scores in one transaction."""
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

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _extract_termination_reason(queue_row):
    """Pull data.metadata.termination_reason from voip_queue_raw_payload.

    Defensive: returns None on missing field, malformed JSON, or wrong type.
    ElevenLabs supplies this; legacy AAI providers always get None, which
    the classifier handles fine.
    """
    raw = queue_row.get("voip_queue_raw_payload")
    if not raw:
        return None
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        return ((raw.get("data") or {}).get("metadata") or {}).get("termination_reason")
    except Exception:
        return None


def _persist_classified_no_answer(interaction_id, transcript,
                                  audio_url=None, audio_bytes=None):
    """Mark an interaction no_answer (status 44) with transcript stored.

    Used when the classifier decides the call wasn't a real conversation
    (voicemail, hold-message-only, failed_call). Preserves transcript and
    optional audio so an operator can audit why it was filtered out. No
    rubric scores written.
    """
    conn = get_conn()
    try:
        conn.execute(
            q("""UPDATE interactions SET
                    interaction_transcript = ?,
                    interaction_audio_url  = ?,
                    interaction_audio_data = ?,
                    status_id              = ?
                  WHERE interaction_id = ?"""),
            (transcript, audio_url, audio_bytes, STATUS_NO_ANSWER, interaction_id),
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


def _grade_and_finalize(voip_queue_id, interaction_id, *,
                        transcript, criteria, project,
                        audio_url=None, audio_bytes=None,
                        provider=None, conversation_id=None):
    """Run Claude grading + persist results + mark queue graded.

    Shared by both transcript paths (legacy AAI and provided-transcript).
    On any failure rolls the interaction back to status 45 (pending) and
    marks the queue row failed. Never raises.
    """
    _set_interaction_status(interaction_id, STATUS_GRADING)
    try:
        grade_result = grader.grade_with_claude(
            transcript=transcript,
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

    try:
        _persist_grade(
            interaction_id,
            grade_result=grade_result,
            criteria=criteria,
            total_score=total_score,
            flags=flags,
            transcript=transcript,
            audio_url=audio_url,
            audio_bytes=audio_bytes,
            final_status_id=STATUS_GRADED,
        )
    except Exception as e:
        _set_interaction_status(interaction_id, STATUS_SUBMITTED)
        _mark_failed(voip_queue_id, f"Persisting grade failed: {e}")
        return

    _set_queue_status(
        voip_queue_id, "graded",
        error="", interaction_id=interaction_id,
    )
    _link_to_scheduled_call(voip_queue_id, "graded")
    _set_interaction_call_start_from_schedule(voip_queue_id)
    _attach_respondent_and_fire_report(
        interaction_id,
        company_id      = _company_id_for_interaction(interaction_id),
        respondent_name = grade_result.get("responder_name"),
        location_id     = _location_id_for_interaction(interaction_id),
    )
    # Fire-and-forget ElevenLabs audio post-fetch. Provider-gated: AAI flow
    # already has its own audio path, and other VoIP providers don't expose
    # a conversation→audio endpoint compatible with this fetcher.
    if provider == "elevenlabs" and conversation_id:
        fetch_and_store_audio_async(interaction_id, conversation_id)


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

    # ── Detect attribution / provided-transcript path ──────────────
    # When the upstream supplied a transcript inline (ElevenLabs today via
    # webhook dynamic_variables), skip audio download + AAI transcription
    # and use the provided text + attribution columns directly.
    has_provided_transcript = bool(queue_row.get("voip_queue_provided_transcript"))
    explicit_project_id     = queue_row.get("voip_queue_project_id")

    # ── Project resolution ────────────────────────────────────────
    if explicit_project_id:
        # Tenancy double-check at process time (cheap insurance against
        # the project being deleted/archived between queue and processor).
        project = _project_by_id(explicit_project_id, company_id)
        if not project:
            _mark_failed(
                voip_queue_id,
                f"Attribution project_id {explicit_project_id} no longer "
                "valid (deleted/archived/wrong tenant)",
            )
            return
    else:
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

    # ── Audio + transcript acquisition ────────────────────────────
    audio_bytes = None
    transcript  = None

    if has_provided_transcript:
        # Use upstream-supplied text verbatim. No credentials, no download,
        # no tempfile, no AAI. ElevenLabs flow (option A: never store audio).
        transcript = queue_row["voip_queue_provided_transcript"]
    else:
        # Legacy AAI flow: decrypt credentials → download → tempfile → transcribe.
        try:
            credentials = decrypt_credentials(config_row["voip_config_credentials"])
        except Exception as e:
            _mark_failed(voip_queue_id, f"Credentials unavailable: {e}")
            return

        audio_bytes   = queue_row.get("voip_queue_recording_data")
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

        # Persist downloaded bytes onto the queue row so reruns don't re-download.
        try:
            _set_queue_status(voip_queue_id, "processing", recording_data=audio_bytes)
        except Exception:
            logger.exception("Could not persist downloaded bytes; continuing anyway")

    # ── Create interaction row (with attribution + duration if present) ──
    interaction_id = _create_interaction(
        project["project_id"], queue_row["voip_queue_call_date"],
        location_id           = queue_row.get("voip_queue_location_id"),
        campaign_id           = queue_row.get("voip_queue_campaign_id"),
        caller_user_id        = queue_row.get("voip_queue_caller_user_id"),
        call_duration_seconds = queue_row.get("voip_queue_duration_seconds"),
    )

    # ── Provided-transcript path: empty guard, classify, then grade ──
    if has_provided_transcript:
        if not transcript.strip():
            # Empty fast-path: skip the classifier Claude call entirely.
            _set_interaction_status(interaction_id, STATUS_NO_ANSWER)
            _set_queue_status(
                voip_queue_id, "graded",
                error="", interaction_id=interaction_id,
            )
            _link_to_scheduled_call(voip_queue_id, "no_answer")
            _set_interaction_call_start_from_schedule(voip_queue_id)
            return

        # Classify before grading. Voicemails / hold-only / failed → no_answer
        # with transcript stored for audit; only real_conversation grades.
        termination_reason = _extract_termination_reason(queue_row)
        classification = classify_call(
            transcript,
            queue_row.get("voip_queue_duration_seconds"),
            termination_reason,
        )
        if classification != "real_conversation":
            _persist_classified_no_answer(
                interaction_id, transcript,
                audio_url=None, audio_bytes=None,
            )
            _set_queue_status(
                voip_queue_id, "graded",
                error="", interaction_id=interaction_id,
            )
            _link_to_scheduled_call(voip_queue_id, "no_answer")
            _set_interaction_call_start_from_schedule(voip_queue_id)
            # Voicemail/hold audio is genuinely useful for the manager —
            # fire the same async fetch as the success-grade path. Provider-
            # gated; AAI/no-conv-id flows skip naturally.
            if (queue_row.get("voip_queue_provider") == "elevenlabs"
                    and queue_row.get("voip_queue_call_id")):
                fetch_and_store_audio_async(
                    interaction_id, queue_row["voip_queue_call_id"]
                )
            return

        # Audio is fetched async post-grade by audio_fetcher (Arc D —
        # reverses C2b.2's never-store decision). audio_url/bytes stay None
        # at grade time; the daemon thread fills them in from ElevenLabs'
        # /audio API once the queue row is graded.
        _grade_and_finalize(
            voip_queue_id, interaction_id,
            transcript=transcript, criteria=criteria, project=project,
            audio_url=None, audio_bytes=None,
            provider=queue_row.get("voip_queue_provider"),
            conversation_id=queue_row.get("voip_queue_call_id"),
        )
        return

    # ── Legacy AAI path: tempfile → transcribe → grade → persist ──
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    try:
        tmp.write(audio_bytes)
        tmp.close()

        # Per-tenant custom vocabulary — only loaded for the AAI path
        hints = load_active_hints(company_id)
        try:
            transcript = grader.transcribe(tmp.name, keyterms_prompt=hints)
        except grader.EmptyTranscriptError:
            _set_interaction_status(interaction_id, STATUS_SUBMITTED)
            _mark_failed(voip_queue_id, "Transcription returned no audible content.")
            return
        except Exception as e:
            _set_interaction_status(interaction_id, STATUS_SUBMITTED)
            _mark_failed(voip_queue_id, f"Transcription failed: {e}")
            return

        audio_url = _save_audio_url(interaction_id)
        # SQLite doesn't persist bytes on the interaction; keep on queue row.
        audio_bytes_for_interaction = audio_bytes if IS_POSTGRES else None

        # Classifier runs on all providers, not just ElevenLabs — provider-
        # agnostic gate. Any future provider that lands a transcript via this
        # path inherits the same voicemail/hold/failed filtering for free.
        # Empty transcript already short-circuited above via EmptyTranscriptError.
        termination_reason = _extract_termination_reason(queue_row)
        classification = classify_call(
            transcript,
            queue_row.get("voip_queue_duration_seconds"),
            termination_reason,
        )
        if classification != "real_conversation":
            _persist_classified_no_answer(
                interaction_id, transcript,
                audio_url=audio_url,
                audio_bytes=audio_bytes_for_interaction,
            )
            _set_queue_status(
                voip_queue_id, "graded",
                error="", interaction_id=interaction_id,
            )
            _link_to_scheduled_call(voip_queue_id, "no_answer")
            return

        _grade_and_finalize(
            voip_queue_id, interaction_id,
            transcript=transcript, criteria=criteria, project=project,
            audio_url=audio_url, audio_bytes=audio_bytes_for_interaction,
            provider=queue_row.get("voip_queue_provider"),
            conversation_id=queue_row.get("voip_queue_call_id"),
        )
    finally:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except Exception:
            pass
