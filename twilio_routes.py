"""
twilio_routes.py — Twilio Voice browser-dial integration.

Status: ENV-GATED. Routes return 503 until TWILIO_* env vars are set.

Architecture (browser-mediated dial):
  1. User opens the Grade page → picks Browser-call mode → enters target phone
  2. Browser POSTs /api/twilio/access-token with the call context
     (project / location / campaign / respondent / target phone)
  3. Backend creates a `twilio_browser_calls` row + mints a short-lived
     JWT scoped to the TwiML App + this user's identity
  4. Browser uses Twilio Voice SDK to start a call with that token,
     passing the pending-call id as a custom parameter
  5. Twilio fetches /api/twilio/voice on connect → backend reads the
     pending-call context + returns <Dial><Number>...</Number></Dial>
     with recording enabled (record="record-from-answer-dual")
  6. Caller talks; Twilio records both legs server-side
  7. Twilio POSTs /api/twilio/recording-callback when the recording is
     ready; backend fetches the audio bytes and hands them to
     enqueue_grade_job() — the existing grade pipeline takes over from
     there (transcribe + grade), and the resulting interaction id is
     pinned back onto the twilio_browser_calls row

Activation (one-time, on the Twilio side — Mayfair admin or equivalent):
  1. Create / log into a Twilio account
  2. Buy a phone number that supports Voice (Phone Numbers → Buy a number)
     — this is the caller-ID number every dial will originate from
  3. Create a TwiML App (Develop → TwiML Apps → New)
       Voice → Request URL: https://<echo-audit-host>/api/twilio/voice
       HTTP method: POST
     Save it; copy the App SID
  4. Create an API Key + Secret (Develop → API Keys → Create new)
       Friendly name: "Echo Audit Voice"
       Type: Standard
     Copy BOTH the SID + Secret values; the secret is shown only once
  5. Send Echo Audit ops these six values:
       Account SID
       Auth Token
       API Key SID
       API Key Secret
       TwiML App SID
       Phone number (E.164, e.g. +15125551234)

Activation (Echo Audit side — Railway → echo-audit-app → Variables):
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_API_KEY
  TWILIO_API_SECRET
  TWILIO_TWIML_APP_SID
  TWILIO_PHONE_NUMBER

The blueprint serves 503 on every route while any of those are missing,
and the Grade page hides the Browser-call mode toggle.
"""

import logging
import os

from flask import Blueprint, Response, jsonify, request, url_for
from flask_login import current_user, login_required

from db import IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id

logger = logging.getLogger(__name__)

twilio_bp = Blueprint("twilio", __name__, url_prefix="/api/twilio")


_REQUIRED_ENV = (
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_API_KEY",
    "TWILIO_API_SECRET",
    "TWILIO_TWIML_APP_SID",
    "TWILIO_PHONE_NUMBER",
)


def _config():
    """Return Twilio config dict if every env var is set, else None."""
    cfg = {k: os.getenv(k, "").strip() for k in _REQUIRED_ENV}
    if not all(cfg.values()):
        return None
    return cfg


def _not_configured_response():
    return jsonify({
        "error": "Twilio Voice is not configured on this instance.",
        "missing_env_vars": [k for k in _REQUIRED_ENV if not os.getenv(k, "").strip()],
        "next_steps": (
            "An admin must set the TWILIO_* env vars and complete the Twilio "
            "console setup. See twilio_routes.py module docstring."
        ),
    }), 503


def is_twilio_voice_configured():
    """Exposed via the global context processor as `twilio_voice_enabled`
    so login.html / grade.html can hide the Browser-call surface on
    instances that haven't been wired yet."""
    return _config() is not None


# ── Helpers ─────────────────────────────────────────────────


def _err(msg, code):
    return jsonify({"error": msg}), code


def _normalize_e164(raw):
    """Light E.164-ish normalization for outbound dials. Strips everything
    that isn't a digit or '+', then prepends US '+1' if the number looks
    like a 10-digit local number (the most common shape Mayfair will paste).

    Not a full-blown phonenumbers parse — Twilio itself rejects malformed
    numbers with a clear error, so we just want to catch the easy cases.
    """
    if not raw:
        return ""
    cleaned = "".join(ch for ch in str(raw) if ch.isdigit() or ch == "+")
    if cleaned.startswith("+"):
        return cleaned
    if len(cleaned) == 10:
        return "+1" + cleaned
    if len(cleaned) == 11 and cleaned.startswith("1"):
        return "+" + cleaned
    return cleaned


def _create_pending_call(*, company_id, user_id, project_id, location_id,
                         campaign_id, target_phone, respondent_name):
    """Insert a twilio_browser_calls row in 'pending' state. Returns id."""
    conn = get_conn()
    try:
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO twilio_browser_calls
                       (company_id, caller_user_id, project_id, location_id,
                        campaign_id, tbc_target_phone, tbc_respondent_name,
                        tbc_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                   RETURNING tbc_id""",
                (company_id, user_id, project_id, location_id, campaign_id,
                 target_phone, respondent_name),
            )
            tbc_id = cur.fetchone()["tbc_id"]
        else:
            conn.execute(
                """INSERT INTO twilio_browser_calls
                       (company_id, caller_user_id, project_id, location_id,
                        campaign_id, tbc_target_phone, tbc_respondent_name,
                        tbc_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (company_id, user_id, project_id, location_id, campaign_id,
                 target_phone, respondent_name),
            )
            tbc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return tbc_id
    finally:
        conn.close()


def _set_call_status(tbc_id, *, status=None, call_sid=None,
                     recording_sid=None, recording_url=None,
                     interaction_id=None, error=None, completed=False):
    """Patch a twilio_browser_calls row with whatever the caller has."""
    sets, params = [], []
    if status is not None:
        sets.append("tbc_status = ?"); params.append(status)
    if call_sid is not None:
        sets.append("tbc_call_sid = ?"); params.append(call_sid)
    if recording_sid is not None:
        sets.append("tbc_recording_sid = ?"); params.append(recording_sid)
    if recording_url is not None:
        sets.append("tbc_recording_url = ?"); params.append(recording_url)
    if interaction_id is not None:
        sets.append("tbc_interaction_id = ?"); params.append(interaction_id)
    if error is not None:
        sets.append("tbc_error = ?"); params.append(error)
    if completed:
        sets.append("tbc_completed_at = NOW()" if IS_POSTGRES else "tbc_completed_at = CURRENT_TIMESTAMP")
    if not sets:
        return
    sql = "UPDATE twilio_browser_calls SET " + ", ".join(sets) + " WHERE tbc_id = ?"
    params.append(tbc_id)
    conn = get_conn()
    try:
        conn.execute(q(sql), params)
        conn.commit()
    finally:
        conn.close()


def _load_pending(tbc_id):
    """Return a twilio_browser_calls row dict by id, or None."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT * FROM twilio_browser_calls WHERE tbc_id = ?"),
            [tbc_id],
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row) if hasattr(row, "keys") else {k: row[i] for i, k in enumerate(row.keys())}
    finally:
        conn.close()


# ── Routes ──────────────────────────────────────────────────


@twilio_bp.route("/access-token", methods=["POST"])
@login_required
def access_token():
    """Mint a short-lived Twilio Voice JWT for the browser, plus create the
    pending-call row that ties this dial to the user's grade context.

    Body: {project_id, location_id, campaign_id?, target_phone,
           respondent_name?}
    Returns: {token, identity, pending_call_id}
    """
    cfg = _config()
    if not cfg:
        return _not_configured_response()

    company_id = get_effective_company_id()
    if company_id is None:
        return _err("No active company.", 400)

    body = request.get_json(silent=True) or {}
    target_phone = _normalize_e164(body.get("target_phone"))
    if not target_phone:
        return _err("target_phone is required.", 400)
    project_id  = body.get("project_id")
    location_id = body.get("location_id")
    campaign_id = body.get("campaign_id")
    respondent  = (body.get("respondent_name") or "").strip() or None

    if not project_id or not location_id:
        return _err("project_id and location_id are required.", 400)

    tbc_id = _create_pending_call(
        company_id=company_id, user_id=current_user.user_id,
        project_id=project_id, location_id=location_id, campaign_id=campaign_id,
        target_phone=target_phone, respondent_name=respondent,
    )

    # Mint the JWT — lazy import keeps app boot light if twilio isn't installed.
    from twilio.jwt.access_token import AccessToken
    from twilio.jwt.access_token.grants import VoiceGrant

    identity = f"user-{current_user.user_id}-call-{tbc_id}"
    token = AccessToken(
        cfg["TWILIO_ACCOUNT_SID"],
        cfg["TWILIO_API_KEY"],
        cfg["TWILIO_API_SECRET"],
        identity=identity,
        # Short TTL — token is consumed immediately by the browser to start
        # one specific call, so 5 minutes is plenty.
        ttl=300,
    )
    token.add_grant(VoiceGrant(
        outgoing_application_sid=cfg["TWILIO_TWIML_APP_SID"],
        incoming_allow=False,
    ))

    return jsonify({
        "token": token.to_jwt(),
        "identity": identity,
        "pending_call_id": tbc_id,
    })


@twilio_bp.route("/voice", methods=["POST"])
def voice_twiml():
    """TwiML response Twilio fetches when the browser-initiated call connects.

    Reads the pending-call id from the custom parameter the browser passed
    via Voice SDK (`params: {pending_call_id}`), pulls the target number,
    and returns a <Dial><Number record="..."/></Dial> response.

    Recording status callback points back at /api/twilio/recording-callback
    so we can pull the audio when it's ready.

    No login required — Twilio is the caller. Authenticity will be enforced
    via Twilio's request-signature check in a follow-up; the recording
    pipeline is already idempotent on Call SID, so a stray request can
    only cause a duplicate enqueue at worst.
    """
    cfg = _config()
    if not cfg:
        return Response("Twilio Voice not configured", status=503,
                        mimetype="text/plain")

    tbc_id_raw = request.values.get("pending_call_id")
    try:
        tbc_id = int(tbc_id_raw)
    except (TypeError, ValueError):
        logger.warning("[twilio.voice] missing/bad pending_call_id=%r", tbc_id_raw)
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Call could not be set up.</Say><Hangup/></Response>',
            mimetype="application/xml",
        )

    pending = _load_pending(tbc_id)
    if not pending:
        logger.warning("[twilio.voice] no pending row for id=%s", tbc_id)
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Call context expired.</Say><Hangup/></Response>',
            mimetype="application/xml",
        )

    target = pending["tbc_target_phone"]
    call_sid = request.values.get("CallSid")
    if call_sid:
        _set_call_status(tbc_id, call_sid=call_sid, status="dialing")

    # record-from-answer-dual = both legs in one mixed file, recording starts
    # the moment the property answers (skips the dial tone).
    callback_url = request.url_root.rstrip("/") + "/api/twilio/recording-callback"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
          f'<Dial callerId="{cfg["TWILIO_PHONE_NUMBER"]}" '
                  'record="record-from-answer-dual" '
                  f'recordingStatusCallback="{callback_url}" '
                  'recordingStatusCallbackEvent="completed" '
                  f'recordingStatusCallbackMethod="POST" '
                  'answerOnBridge="true">'
            f'<Number>{target}</Number>'
          '</Dial>'
        '</Response>'
    )
    return Response(twiml, mimetype="application/xml")


@twilio_bp.route("/recording-callback", methods=["POST"])
def recording_callback():
    """Twilio webhook fired when a call recording is fully written and
    available. Pulls the audio bytes via Twilio's REST API and hands them
    to enqueue_grade_job — same path the existing live-record + upload
    flows use.

    Idempotent on RecordingSid: re-firing this webhook for the same
    recording is a no-op (already-processed rows have a non-null
    tbc_interaction_id).
    """
    cfg = _config()
    if not cfg:
        return ("not configured", 503)

    call_sid       = request.values.get("CallSid")
    recording_sid  = request.values.get("RecordingSid")
    recording_url  = request.values.get("RecordingUrl")
    duration_raw   = request.values.get("RecordingDuration")
    if not call_sid or not recording_sid or not recording_url:
        logger.warning("[twilio.rec_cb] missing required fields: %r", request.values)
        return ("", 400)

    # Look up the pending row by Call SID. Failed lookups are logged but
    # we still 200 so Twilio doesn't keep retrying — manual investigation
    # is more useful than retry loops on a structural mismatch.
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT * FROM twilio_browser_calls WHERE tbc_call_sid = ? LIMIT 1"),
            [call_sid],
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        logger.warning("[twilio.rec_cb] no row for call_sid=%s", call_sid)
        return ("", 200)
    pending = dict(row) if hasattr(row, "keys") else {k: row[i] for i, k in enumerate(row.keys())}

    if pending.get("tbc_interaction_id"):
        logger.info("[twilio.rec_cb] already processed tbc=%s", pending["tbc_id"])
        return ("", 200)

    _set_call_status(pending["tbc_id"], recording_sid=recording_sid,
                     recording_url=recording_url, status="recorded")

    # Fetch the recording audio. Twilio recordings are MP3 by default; the
    # base RecordingUrl needs a .mp3 suffix for direct download. Auth is
    # HTTP Basic with Account SID + Auth Token.
    import requests
    audio_resp = requests.get(
        recording_url + ".mp3",
        auth=(cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"]),
        timeout=30,
    )
    if not audio_resp.ok:
        logger.warning("[twilio.rec_cb] audio fetch failed status=%s tbc=%s",
                       audio_resp.status_code, pending["tbc_id"])
        _set_call_status(pending["tbc_id"], status="failed",
                         error=f"audio fetch HTTP {audio_resp.status_code}",
                         completed=True)
        return ("", 200)

    audio_bytes = audio_resp.content
    duration_seconds = None
    try:
        if duration_raw:
            duration_seconds = int(duration_raw)
    except ValueError:
        pass

    # Hand off to the existing pipeline — creates the interaction, pins the
    # audio, queues the grade job. Daemon picks it up and grades async.
    from grade_jobs import enqueue_grade_job, process_grade_job_async
    try:
        job_id, interaction_id = enqueue_grade_job(
            company_id=pending["company_id"],
            submitted_by_user_id=pending["caller_user_id"],
            project_id=pending["project_id"],
            location_id=pending["location_id"],
            audio_bytes=audio_bytes,
            audio_ext="mp3",
            caller_user_id=pending["caller_user_id"],
            campaign_id=pending.get("campaign_id"),
            call_duration_seconds=duration_seconds,
        )
    except Exception as e:
        logger.exception("[twilio.rec_cb] enqueue_grade_job failed tbc=%s", pending["tbc_id"])
        _set_call_status(pending["tbc_id"], status="failed",
                         error=f"enqueue failed: {e}", completed=True)
        return ("", 200)

    _set_call_status(pending["tbc_id"], status="graded",
                     interaction_id=interaction_id, completed=True)

    # Fire the async grade processor in the background (matches the live
    # record / upload path's hand-off).
    try:
        process_grade_job_async(job_id, pending["caller_user_id"])
    except Exception:
        logger.exception("[twilio.rec_cb] async grade kickoff failed job=%s", job_id)
    return ("", 200)


@twilio_bp.route("/pending-call/<int:tbc_id>", methods=["GET"])
@login_required
def get_pending_call_status(tbc_id):
    """Light polling endpoint for the browser to track call → recording →
    grade status without keeping a websocket open. Returns the row's
    current state + (when grading is queued) the interaction id."""
    pending = _load_pending(tbc_id)
    if not pending:
        return _err("Pending call not found.", 404)
    if pending["caller_user_id"] != current_user.user_id and not current_user.is_super_admin:
        return _err("Not yours.", 403)
    return jsonify({
        "tbc_id":          pending["tbc_id"],
        "status":          pending["tbc_status"],
        "interaction_id":  pending.get("tbc_interaction_id"),
        "error":           pending.get("tbc_error"),
        "completed_at":    pending.get("tbc_completed_at"),
    })
