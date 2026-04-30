"""
scheduled_calls_routes.py — Outbound AI shop scheduling endpoints.

Routes:
    POST /api/grade/ai-shop                — initiate an outbound AI call
    GET  /api/grade/ai-shop/<sc_id>/status — poll for terminal state (D3)

Tenancy is DERIVED via sc_location_id → locations.company_id. No
sc_company_id column. Verification at write time uses
verify_attribution_tenancy(); at read time the polling endpoint will
JOIN through locations as the canonical guard.
"""

import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

import auth
from ai_caller_client import AICallerError, initiate_call
from audit_log import (
    ACTION_SCHEDULED_AI_SHOP, ENTITY_SCHEDULED_CALL, write_audit_log,
)
from db import IS_POSTGRES, get_conn, q
from helpers import (
    get_effective_company_id, phone_digits, verify_attribution_tenancy,
)

logger = logging.getLogger(__name__)

scheduled_calls_bp = Blueprint("scheduled_calls", __name__)


def _as_int(v):
    """Coerce form value → int; return None for blank/missing/garbage."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@scheduled_calls_bp.route("/api/grade/ai-shop", methods=["POST"])
@login_required
@auth.role_required("admin", "manager", "super_admin")
def schedule_ai_shop():
    """Initiate an outbound AI shop. Persists scheduled_calls row in both
    success and failure paths so the operator always has an audit trail."""
    data = request.get_json(silent=True) or {}

    location_id    = _as_int(data.get("location_id"))
    project_id     = _as_int(data.get("project_id"))
    campaign_id    = _as_int(data.get("campaign_id"))     # optional
    caller_user_id = _as_int(data.get("caller_user_id"))

    if not (location_id and project_id and caller_user_id):
        return jsonify(error="location_id, project_id, and caller_user_id are required"), 400

    company_id = get_effective_company_id()
    if not company_id:
        return jsonify(error="No company context"), 403

    # ── Tenancy + phone resolution ────────────────────────────────
    conn = get_conn()
    try:
        err = verify_attribution_tenancy(
            conn, company_id, project_id, location_id, caller_user_id, campaign_id,
        )
        if err:
            return jsonify(error=err), 403

        # locations.location_phone is canonical for both outbound dial AND
        # inbound webhook matching (verified D2 recon).
        cur = conn.execute(
            q("""SELECT location_phone FROM locations
                  WHERE location_id = ? AND location_deleted_at IS NULL"""),
            (location_id,),
        )
        loc = cur.fetchone()
        digits = phone_digits((dict(loc) if loc else {}).get("location_phone"))
        if not digits:
            return jsonify(error="Location has no usable phone number on file"), 400
        # US-only assumption today (Mayfair is US). Followup memory
        # followup_to_e164_international_support tracks the helper-promotion
        # plan when the first non-US tenant onboards.
        phone_e164 = f"+1{digits}"

        # Insert scheduled_calls row BEFORE the outbound HTTP. Any failure
        # downstream gets a DB-backed audit trail (sc_status='failed').
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO scheduled_calls
                       (sc_location_id, sc_project_id, sc_campaign_id,
                        sc_caller_user_id, sc_requested_by_user_id,
                        sc_phone_number, sc_status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'initiated')
                   RETURNING sc_id""",
                (location_id, project_id, campaign_id,
                 caller_user_id, current_user.user_id, phone_e164),
            )
            sc_id = cur.fetchone()["sc_id"]
        else:
            conn.execute(
                """INSERT INTO scheduled_calls
                       (sc_location_id, sc_project_id, sc_campaign_id,
                        sc_caller_user_id, sc_requested_by_user_id,
                        sc_phone_number, sc_status)
                   VALUES (?, ?, ?, ?, ?, ?, 'initiated')""",
                (location_id, project_id, campaign_id,
                 caller_user_id, current_user.user_id, phone_e164),
            )
            sc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    # ── Outbound HTTP — outside any DB transaction ───────────────
    ai_response = None
    ai_error    = None
    try:
        ai_response = initiate_call(
            phone_e164,
            sc_id=sc_id, location_id=location_id, project_id=project_id,
            campaign_id=campaign_id, caller_user_id=caller_user_id,
        )
    except AICallerError as exc:
        ai_error = str(exc)
        logger.warning("[ai_shop] sc_id=%s ai_caller failed: %s", sc_id, exc)

    # ── Persist outcome on the scheduled_calls row ───────────────
    conn = get_conn()
    try:
        if ai_error is None:
            conv_id = (ai_response or {}).get("conversation_id")
            conn.execute(
                q("""UPDATE scheduled_calls
                        SET sc_conversation_id = ?,
                            sc_ai_caller_response = ?
                      WHERE sc_id = ?"""),
                (conv_id, json.dumps(ai_response), sc_id),
            )
        else:
            if IS_POSTGRES:
                conn.execute(
                    """UPDATE scheduled_calls
                          SET sc_status = 'failed',
                              sc_status_message = %s,
                              sc_completed_at = NOW()
                        WHERE sc_id = %s""",
                    (ai_error, sc_id),
                )
            else:
                conn.execute(
                    """UPDATE scheduled_calls
                          SET sc_status = 'failed',
                              sc_status_message = ?,
                              sc_completed_at = CURRENT_TIMESTAMP
                        WHERE sc_id = ?""",
                    (ai_error, sc_id),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # ── Audit log (best-effort — never 500 the user on log failure) ──
    try:
        write_audit_log(
            current_user.user_id,
            ACTION_SCHEDULED_AI_SHOP,
            ENTITY_SCHEDULED_CALL, sc_id,
            metadata={
                "location_id":      location_id,
                "project_id":       project_id,
                "campaign_id":      campaign_id,
                "caller_user_id":   caller_user_id,
                "phone_number":     phone_e164,
                "ai_caller_status": "failed" if ai_error else "initiated",
                "ai_caller_error":  ai_error,
            },
        )
    except Exception:
        logger.warning(
            "[ai_shop audit_log_failed] sc_id=%s status=%s",
            sc_id, "failed" if ai_error else "initiated",
            exc_info=True,
        )

    if ai_error:
        return jsonify(sc_id=sc_id, error=ai_error), 502

    return jsonify(
        sc_id=sc_id,
        conversation_id=(ai_response or {}).get("conversation_id"),
        status="initiated",
    ), 200


def _iso(dt):
    """ISO-format a datetime tolerantly. SQLite returns TEXT; PG returns
    datetime objects. Returns None for None inputs."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


@scheduled_calls_bp.route("/api/grade/ai-shop/<int:sc_id>/status", methods=["GET"])
@login_required
def schedule_ai_shop_status(sc_id):
    """Poll a scheduled AI shop's status.

    404 on missing row OR cross-tenant access (no info leak about the
    existence of other tenants' scheduled_calls). No role gate — read-only,
    anyone in the company who knows the sc_id can poll.

    Returns derived display_status:
      - 'graded' / 'no_answer' / 'failed' / 'timeout' (terminal — UI stops polling)
      - 'webhook_received' / 'processing' (intermediate, derived from join)
      - 'initiated' (default — waiting for the AI caller / ElevenLabs webhook)
    """
    company_id = get_effective_company_id()
    if not company_id:
        return jsonify(error="No company context"), 403

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""
                SELECT
                    sc.sc_id, sc.sc_status, sc.sc_conversation_id,
                    sc.sc_requested_at, sc.sc_completed_at, sc.sc_status_message,
                    sc.sc_phone_number,
                    l.location_name,
                    vcq.voip_queue_id, vcq.voip_queue_status,
                    vcq.voip_queue_interaction_id,
                    i.status_id AS interaction_status_id,
                    i.interaction_overall_score
                  FROM scheduled_calls sc
                  JOIN locations l ON l.location_id = sc.sc_location_id
                  LEFT JOIN voip_call_queue vcq
                         ON vcq.voip_queue_call_id = sc.sc_conversation_id
                  LEFT JOIN interactions i
                         ON i.interaction_id = vcq.voip_queue_interaction_id
                 WHERE sc.sc_id = ? AND l.company_id = ?
            """),
            (sc_id, company_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return jsonify(error="Not found"), 404

    r = dict(row)
    stored_status  = r["sc_status"]
    vcq_id         = r.get("voip_queue_id")
    vcq_status     = r.get("voip_queue_status")
    interaction_id = r.get("voip_queue_interaction_id")
    requested_at   = r.get("sc_requested_at")
    completed_at   = r.get("sc_completed_at")

    # elapsed_seconds for timeout derivation. Tolerate naive (SQLite TEXT
    # — cast fails gracefully) + aware (Postgres TIMESTAMPTZ) datetimes.
    elapsed_seconds = None
    if requested_at is not None:
        try:
            ra = requested_at
            if hasattr(ra, "tzinfo") and ra.tzinfo is None:
                ra = ra.replace(tzinfo=timezone.utc)
            elapsed_seconds = int((datetime.now(timezone.utc) - ra).total_seconds())
        except Exception:
            elapsed_seconds = None

    # Display status derivation — locked logic.
    if stored_status == "failed":
        display = "failed"
    elif stored_status == "graded":
        display = "graded"
    elif stored_status == "no_answer":
        display = "no_answer"
    elif stored_status == "initiated":
        if vcq_id is None and elapsed_seconds is not None and elapsed_seconds > 600:
            display = "timeout"
        elif vcq_status == "processing":
            display = "processing"
        elif vcq_id is not None:
            display = "webhook_received"
        else:
            display = "initiated"
    else:
        # Defensive — shouldn't hit given chk_sc_status, but don't crash.
        display = stored_status

    is_terminal = display in ("graded", "no_answer", "failed", "timeout")

    return jsonify(
        sc_id=r["sc_id"],
        display_status=display,
        stored_status=stored_status,
        conversation_id=r.get("sc_conversation_id"),
        location_name=r.get("location_name"),
        phone_number=r.get("sc_phone_number"),
        requested_at=_iso(requested_at),
        completed_at=_iso(completed_at),
        elapsed_seconds=elapsed_seconds,
        interaction_id=interaction_id,
        interaction_overall_score=(
            float(r["interaction_overall_score"])
            if r.get("interaction_overall_score") is not None else None
        ),
        status_message=r.get("sc_status_message"),
        is_terminal=is_terminal,
    )
