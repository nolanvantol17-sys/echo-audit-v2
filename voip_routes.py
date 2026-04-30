"""
voip_routes.py — Echo Audit V2 Phase 5 VoIP integration routes.

Blueprint mounts:
    /api/voip/webhook/<company_id>              POST (public, signature-verified)
    /api/voip/config                             GET / POST / DELETE (admin+)
    /api/voip/webhook-url                        GET  (admin+)
    /api/voip/providers                          GET  (authenticated)
    /api/voip/queue                              GET  (admin+)
    /api/voip/queue/<id>/grade                   POST (admin+)
    /api/voip/queue/<id>/skip                    POST (admin+)
    /api/voip/queue/<id>/audio                   GET  (admin+)

Webhook endpoint is deliberately public (no @login_required). It authenticates
via per-provider signature verification against the stored webhook secret.
All heavy work happens in daemon threads — the webhook always returns 200.
"""

import io
import json
import logging
import secrets
from datetime import datetime

import requests
from flask import Blueprint, Response, jsonify, request, send_file
from flask_login import current_user, login_required

from audit_log import (
    ACTION_CREATED, ACTION_DELETED, ACTION_UPDATED,
    ENTITY_COMPANY, write_audit_log,
)
from auth import role_required
from db import IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id
from voip.credentials import (
    credentials_fingerprint, decrypt_credentials, encrypt_credentials,
)
from voip.processor import process_voip_call_async
from voip.providers import (
    PROVIDER_INFO, PROVIDER_WEBHOOK_SECRET_FIELD, PROVIDERS,
)

logger = logging.getLogger(__name__)

voip_bp = Blueprint("voip", __name__, url_prefix="/api/voip")


# ── Shared helpers ─────────────────────────────────────────────


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


def _get_voip_config(conn, company_id, include_inactive=False):
    filters = ["company_id = ?"]
    if not include_inactive:
        filters.append("voip_config_is_active = TRUE")
    cur = conn.execute(
        q(f"SELECT * FROM voip_configs WHERE {' AND '.join(filters)}"),
        (company_id,),
    )
    return _row_to_dict(cur.fetchone())


def _get_queue_item(conn, voip_queue_id, company_id):
    cur = conn.execute(
        q("""SELECT * FROM voip_call_queue
             WHERE voip_queue_id = ? AND company_id = ?"""),
        (voip_queue_id, company_id),
    )
    return _row_to_dict(cur.fetchone())


def _webhook_url_for(company_id):
    """Build the webhook URL a client pastes into their VoIP platform."""
    host = request.host_url.rstrip("/")
    return f"{host}/api/voip/webhook/{company_id}"


def _audit(user_id, action_id, company_id, metadata=None, conn=None):
    """Thin wrapper — VoIP entities roll up under ENTITY_COMPANY for audit
    purposes since no dedicated entity_type_id exists for voip_config."""
    write_audit_log(
        user_id, action_id, ENTITY_COMPANY, company_id,
        metadata=metadata, conn=conn,
    )


# ═══════════════════════════════════════════════════════════════
# POST /api/voip/webhook/<company_id>   —  public
# ═══════════════════════════════════════════════════════════════


@voip_bp.route("/webhook/<int:company_id>", methods=["POST"])
def voip_webhook(company_id):
    """Public webhook endpoint. Must always return 200 so providers don't retry.

    Processing errors are recorded on the queue row (voip_queue_error), not
    surfaced as HTTP errors.
    """
    # Capture the raw body BEFORE Flask's JSON parser touches it — signature
    # verification must run against the exact bytes the provider signed.
    raw_body = request.get_data(cache=True)
    headers = dict(request.headers)

    # ── Fast-path: Zoom / RingCentral URL-validation handshake ──
    # These come before we know which provider it is. Handle them optimistically
    # when we see the telltale headers. Regardless of provider, echoing the token
    # in a 200 response is the canonical behavior and never unsafe.
    validation_token = (
        headers.get("Validation-Token")
        or headers.get("validation-token")
    )
    if validation_token:
        # RingCentral-style handshake: echo the header back.
        resp = jsonify({"ok": True})
        resp.headers["Validation-Token"] = validation_token
        return resp, 200

    conn = get_conn()
    try:
        config_row = _get_voip_config(conn, company_id, include_inactive=False)
    finally:
        conn.close()

    if not config_row:
        # 404 for a missing company is fine — the VoIP provider isn't retrying
        # a URL that was never valid in the first place.
        return _err("VoIP config not found or inactive", 404)

    provider_key = config_row["voip_config_provider"]
    provider = PROVIDERS.get(provider_key)
    if not provider:
        return _err("Unknown provider configured", 500)

    # Zoom URL-validation event — body contains plainToken that must be HMAC'd back.
    if provider_key == "zoom_phone":
        try:
            parsed_early = json.loads(raw_body) if raw_body else {}
        except Exception:
            parsed_early = {}
        if parsed_early.get("event") == "endpoint.url_validation":
            plain = (parsed_early.get("payload") or {}).get("plainToken")
            if plain:
                try:
                    creds = decrypt_credentials(config_row["voip_config_credentials"])
                    secret = creds.get(PROVIDER_WEBHOOK_SECRET_FIELD[provider_key]) or ""
                except Exception:
                    secret = ""
                import hashlib
                import hmac as _hmac
                encrypted = _hmac.new(
                    secret.encode("utf-8"),
                    plain.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                return jsonify({"plainToken": plain, "encryptedToken": encrypted}), 200

    # ── Signature verification ──
    try:
        credentials = decrypt_credentials(config_row["voip_config_credentials"])
    except Exception:
        logger.exception("Failed to decrypt credentials for company %s", company_id)
        return _err("Signature verification failed", 401)

    # Secret resolution order: operator-supplied credentials FIRST, then the
    # legacy column. The voip_config_webhook_secret column was originally an
    # auto-generated fallback for generic_webhook (where Echo Audit issues the
    # secret), but the upsert flow populates it with secrets.token_urlsafe(32)
    # for ALL providers — which silently overrode the upstream-supplied secret
    # for Dialpad/Aircall/8x8/Zoom/ElevenLabs. Credentials are authoritative
    # for any provider whose webhook secret comes from the upstream platform.
    secret_field = PROVIDER_WEBHOOK_SECRET_FIELD.get(provider_key)
    secret = (
        (credentials.get(secret_field) if secret_field else None)
        or config_row.get("voip_config_webhook_secret")
        or ""
    )

    if not provider.verify_signature(raw_body, headers, secret):
        # Log full headers + body excerpt so we can debug provider integration
        # mismatches (wrong header name, wrong signing scheme, secret typo).
        # Grep tag: [voip_webhook signature_failed]
        logger.warning(
            "[voip_webhook signature_failed] company=%s provider=%s headers=%r body[:200]=%r",
            company_id, provider_key, headers, raw_body[:200],
        )
        return _err("Signature verification failed", 401)

    # Mirror visibility on the success path at DEBUG level — off by default in
    # prod, available when manually raised for inspection. Grep tag:
    # [voip_webhook signature_ok]
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[voip_webhook signature_ok] company=%s provider=%s headers=%r body[:200]=%r",
            company_id, provider_key, headers, raw_body[:200],
        )

    # ── Parse payload ──
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return _err("Invalid JSON payload", 400)

    try:
        event = provider.parse_webhook(payload, headers)
    except Exception:
        logger.exception("Provider %s parse_webhook raised", provider_key)
        event = None

    if event is None:
        # Not a call-completed event (e.g. heartbeat, unrelated notification).
        # Return 200 so the provider doesn't retry.
        return jsonify({"ok": True, "ignored": True}), 200

    # ── Insert queue row (or detect duplicate) ──
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT voip_queue_id FROM voip_call_queue
                 WHERE company_id = ? AND voip_queue_provider = ?
                   AND voip_queue_call_id = ?"""),
            (company_id, event.provider, event.call_id),
        )
        existing = cur.fetchone()
        if existing:
            # Already queued — silently accept so the provider stops retrying.
            return jsonify({"ok": True, "duplicate": True}), 200

        raw_payload_json = json.dumps(event.raw_payload)
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO voip_call_queue (
                       company_id, voip_queue_provider, voip_queue_call_id,
                       voip_queue_recording_url, voip_queue_caller_number,
                       voip_queue_called_number, voip_queue_call_date,
                       voip_queue_duration_seconds, voip_queue_raw_payload,
                       voip_queue_status
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'pending')
                   RETURNING voip_queue_id""",
                (company_id, event.provider, event.call_id,
                 event.recording_url, event.caller_number, event.called_number,
                 event.call_date, event.duration_seconds, raw_payload_json),
            )
            queue_id = cur.fetchone()["voip_queue_id"]
        else:
            conn.execute(
                """INSERT INTO voip_call_queue (
                       company_id, voip_queue_provider, voip_queue_call_id,
                       voip_queue_recording_url, voip_queue_caller_number,
                       voip_queue_called_number, voip_queue_call_date,
                       voip_queue_duration_seconds, voip_queue_raw_payload,
                       voip_queue_status
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (company_id, event.provider, event.call_id,
                 event.recording_url, event.caller_number, event.called_number,
                 event.call_date, event.duration_seconds, raw_payload_json),
            )
            queue_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Failed to enqueue VoIP call (company=%s)", company_id)
        # Still return 200 so the provider doesn't hammer us — we've logged it.
        return jsonify({"ok": True, "enqueued": False}), 200
    finally:
        conn.close()

    # ── Auto-grade kick-off ──
    if config_row.get("voip_config_auto_grade"):
        try:
            process_voip_call_async(queue_id)
        except Exception:
            logger.exception("Failed to kick off background processing for queue %s",
                             queue_id)
            # Queue row persists — a human can retry later via POST /grade.

    return jsonify({"ok": True, "voip_queue_id": queue_id}), 200


# ═══════════════════════════════════════════════════════════════
# GET/POST/DELETE /api/voip/config
# ═══════════════════════════════════════════════════════════════


def _public_config_view(config_row):
    """Render a config row WITHOUT credentials for client responses."""
    if config_row is None:
        return None
    creds = config_row.get("voip_config_credentials")
    configured = bool(creds) and creds not in ({}, "", "{}")
    return {
        "voip_config_id":         config_row["voip_config_id"],
        "company_id":             config_row["company_id"],
        "voip_config_provider":   config_row["voip_config_provider"],
        "voip_config_auto_grade": bool(config_row.get("voip_config_auto_grade")),
        "voip_config_is_active":  bool(config_row.get("voip_config_is_active")),
        "voip_config_created_at": config_row.get("voip_config_created_at"),
        "voip_config_updated_at": config_row.get("voip_config_updated_at"),
        "credentials_configured": configured,
        "webhook_url":            _webhook_url_for(config_row["company_id"]),
    }


@voip_bp.route("/config", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def get_voip_config():
    company_id, err = _require_company()
    if err: return err
    conn = get_conn()
    try:
        config_row = _get_voip_config(conn, company_id, include_inactive=True)
    finally:
        conn.close()
    if not config_row:
        return jsonify(None)
    return jsonify(_public_config_view(config_row))


@voip_bp.route("/config", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def upsert_voip_config():
    company_id, err = _require_company()
    if err: return err

    body = _body()
    provider_key = (body.get("voip_config_provider") or "").strip()
    credentials = body.get("credentials") or {}
    auto_grade = bool(body.get("voip_config_auto_grade"))

    if provider_key not in PROVIDERS:
        return _err("Unknown voip_config_provider", 400)
    if not isinstance(credentials, dict) or not credentials:
        return _err("credentials must be a non-empty object", 400)

    # Validate that all required fields for this provider are present.
    required_fields = next(
        (p["credentials_fields"] for p in PROVIDER_INFO if p["key"] == provider_key),
        [],
    )
    missing = [f for f in required_fields if not credentials.get(f)]
    if missing:
        return _err(f"Missing credential fields: {', '.join(missing)}", 400)

    # Encrypt, then wrap in the {"enc": "..."} JSONB shape.
    try:
        enc = encrypt_credentials(credentials)
    except Exception:
        logger.exception("encrypt_credentials failed")
        return _err("Credential encryption unavailable — check server config", 500)

    credentials_json = json.dumps({"enc": enc})
    webhook_secret = body.get("voip_config_webhook_secret") or secrets.token_urlsafe(32)
    fp = credentials_fingerprint(credentials)

    conn = get_conn()
    try:
        existing = _get_voip_config(conn, company_id, include_inactive=True)

        if existing:
            if IS_POSTGRES:
                conn.execute(
                    """UPDATE voip_configs SET
                           voip_config_provider       = %s,
                           voip_config_credentials    = %s::jsonb,
                           voip_config_auto_grade     = %s,
                           voip_config_webhook_secret = %s,
                           voip_config_is_active      = TRUE
                       WHERE company_id = %s""",
                    (provider_key, credentials_json, auto_grade,
                     webhook_secret, company_id),
                )
            else:
                conn.execute(
                    """UPDATE voip_configs SET
                           voip_config_provider       = ?,
                           voip_config_credentials    = ?,
                           voip_config_auto_grade     = ?,
                           voip_config_webhook_secret = ?,
                           voip_config_is_active      = 1
                       WHERE company_id = ?""",
                    (provider_key, credentials_json, auto_grade,
                     webhook_secret, company_id),
                )
            action = ACTION_UPDATED
        else:
            if IS_POSTGRES:
                conn.execute(
                    """INSERT INTO voip_configs (
                           company_id, voip_config_provider, voip_config_credentials,
                           voip_config_auto_grade, voip_config_webhook_secret,
                           voip_config_is_active
                       ) VALUES (%s, %s, %s::jsonb, %s, %s, TRUE)""",
                    (company_id, provider_key, credentials_json,
                     auto_grade, webhook_secret),
                )
            else:
                conn.execute(
                    """INSERT INTO voip_configs (
                           company_id, voip_config_provider, voip_config_credentials,
                           voip_config_auto_grade, voip_config_webhook_secret,
                           voip_config_is_active
                       ) VALUES (?, ?, ?, ?, ?, 1)""",
                    (company_id, provider_key, credentials_json,
                     auto_grade, webhook_secret),
                )
            action = ACTION_CREATED

        _audit(
            current_user.user_id, action, company_id,
            metadata={"voip_config_provider": provider_key,
                      "voip_config_auto_grade": auto_grade,
                      "credentials_fingerprint": fp},
            conn=conn,
        )
        conn.commit()

        refreshed = _get_voip_config(conn, company_id, include_inactive=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify(_public_config_view(refreshed)), 201 if action == ACTION_CREATED else 200


@voip_bp.route("/config", methods=["PATCH"])
@login_required
@role_required("admin", "super_admin")
def patch_voip_config():
    """Partial update for an existing voip_config row.

    Currently the only supported field is ``voip_config_auto_grade`` — flipping
    the toggle should not require re-entering the provider's credentials. Other
    fields (provider, credentials, webhook secret) are intentionally re-routes
    through POST so the same validation / encryption / fingerprinting path
    handles them.
    """
    company_id, err = _require_company()
    if err: return err

    body = _body()
    if "voip_config_auto_grade" not in body:
        return _err("Nothing to update", 400)
    auto_grade = bool(body.get("voip_config_auto_grade"))

    conn = get_conn()
    try:
        existing = _get_voip_config(conn, company_id, include_inactive=True)
        if not existing:
            return _err("VoIP config not found", 404)
        if IS_POSTGRES:
            conn.execute(
                "UPDATE voip_configs SET voip_config_auto_grade = %s "
                "WHERE company_id = %s",
                (auto_grade, company_id),
            )
        else:
            conn.execute(
                "UPDATE voip_configs SET voip_config_auto_grade = ? "
                "WHERE company_id = ?",
                (auto_grade, company_id),
            )
        _audit(
            current_user.user_id, ACTION_UPDATED, company_id,
            metadata={"action": "toggle_auto_grade",
                      "new_value": auto_grade},
            conn=conn,
        )
        conn.commit()
        refreshed = _get_voip_config(conn, company_id, include_inactive=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return jsonify(_public_config_view(refreshed))


@voip_bp.route("/config", methods=["DELETE"])
@login_required
@role_required("admin", "super_admin")
def deactivate_voip_config():
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        existing = _get_voip_config(conn, company_id, include_inactive=True)
        if not existing:
            return _err("VoIP config not found", 404)
        if IS_POSTGRES:
            conn.execute(
                "UPDATE voip_configs SET voip_config_is_active = FALSE "
                "WHERE company_id = %s",
                (company_id,),
            )
        else:
            conn.execute(
                "UPDATE voip_configs SET voip_config_is_active = 0 "
                "WHERE company_id = ?",
                (company_id,),
            )
        _audit(
            current_user.user_id, ACTION_DELETED, company_id,
            metadata={"action": "voip_config_deactivate"}, conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@voip_bp.route("/webhook-url", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def get_webhook_url():
    company_id, err = _require_company()
    if err: return err
    return jsonify({"webhook_url": _webhook_url_for(company_id)})


# ═══════════════════════════════════════════════════════════════
# GET /api/voip/providers   (public reference)
# ═══════════════════════════════════════════════════════════════


@voip_bp.route("/providers", methods=["GET"])
@login_required
def list_providers():
    return jsonify({"providers": PROVIDER_INFO})


# ═══════════════════════════════════════════════════════════════
# POST /api/voip/test-connection   (admin+, in-memory creds)
# ═══════════════════════════════════════════════════════════════
#
# Lightweight connectivity check used by the setup form. Accepts the same
# {voip_config_provider, credentials} body shape as POST /config but does
# NOT persist anything — credentials are validated against the provider's
# API and discarded. Returns:
#   { ok: bool, supported: bool, message: str, docs_url?: str }
#
# Only Dialpad and Aircall have live test paths today (they expose cheap
# authenticated GETs that confirm the secret is good). Other API-based
# providers report supported=False with a docs link so the user knows
# where to verify their credentials manually. Generic Webhook has no
# upstream API at all and the UI hides the button — but the endpoint
# answers safely if called directly.

_TEST_TIMEOUT_S = 8


def _test_dialpad(creds):
    api_key = (creds.get("api_key") or "").strip()
    if not api_key:
        return {"ok": False, "supported": True,
                "message": "API key is required to test the connection."}
    try:
        resp = requests.get(
            "https://dialpad.com/api/v2/users",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": 1},
            timeout=_TEST_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        return {"ok": False, "supported": True,
                "message": f"Could not reach Dialpad: {exc}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "supported": True,
                "message": "Dialpad rejected the API key — double-check it in your Dialpad admin console."}
    if resp.status_code >= 400:
        return {"ok": False, "supported": True,
                "message": f"Dialpad returned HTTP {resp.status_code}."}
    return {"ok": True, "supported": True,
            "message": "Dialpad credentials look good."}


def _test_aircall(creds):
    api_id = (creds.get("api_id") or "").strip()
    api_token = (creds.get("api_token") or "").strip()
    if not api_id or not api_token:
        return {"ok": False, "supported": True,
                "message": "API ID and API token are required to test the connection."}
    try:
        resp = requests.get(
            "https://api.aircall.io/v1/ping",
            auth=(api_id, api_token),
            timeout=_TEST_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        return {"ok": False, "supported": True,
                "message": f"Could not reach Aircall: {exc}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "supported": True,
                "message": "Aircall rejected the API ID/token — verify them in your Aircall dashboard."}
    if resp.status_code >= 400:
        return {"ok": False, "supported": True,
                "message": f"Aircall returned HTTP {resp.status_code}."}
    return {"ok": True, "supported": True,
            "message": "Aircall credentials look good."}


_PROVIDER_TESTERS = {
    "dialpad": _test_dialpad,
    "aircall": _test_aircall,
}


def _docs_url_for(provider_key):
    for p in PROVIDER_INFO:
        if p["key"] == provider_key:
            return p.get("docs_url")
    return None


@voip_bp.route("/test-connection", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def test_voip_connection():
    body = _body()
    provider_key = (body.get("voip_config_provider") or "").strip()
    credentials = body.get("credentials") or {}

    if provider_key not in PROVIDERS:
        return _err("Unknown voip_config_provider", 400)
    if not isinstance(credentials, dict):
        return _err("credentials must be an object", 400)

    docs_url = _docs_url_for(provider_key)
    tester = _PROVIDER_TESTERS.get(provider_key)
    if tester is None:
        # Provider doesn't have a live test path yet. Tell the user instead
        # of pretending success — and surface the docs URL so they can verify
        # credentials by hand.
        return jsonify({
            "ok": False,
            "supported": False,
            "message": ("Live connection testing isn't available for this "
                        "provider yet. Save your credentials and we'll surface "
                        "any auth errors when the first webhook arrives."),
            "docs_url": docs_url,
        })
    try:
        result = tester(credentials)
    except Exception:
        logger.exception("VoIP test-connection failed for %s", provider_key)
        return _err("Connection test crashed — check server logs.", 500)
    if docs_url and not result.get("ok"):
        result["docs_url"] = docs_url
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════
# Call queue
# ═══════════════════════════════════════════════════════════════


_QUEUE_COLUMNS = (
    "voip_queue_id", "voip_queue_call_id", "voip_queue_provider",
    "voip_queue_caller_number", "voip_queue_called_number",
    "voip_queue_call_date", "voip_queue_duration_seconds",
    "voip_queue_status", "voip_queue_error", "voip_queue_interaction_id",
    "voip_queue_created_at", "voip_queue_updated_at",
)


@voip_bp.route("/queue", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def list_queue():
    company_id, err = _require_company()
    if err: return err

    filters = ["company_id = ?"]
    params = [company_id]
    if request.args.get("status"):
        filters.append("voip_queue_status = ?")
        params.append(request.args["status"])
    if request.args.get("from_date"):
        filters.append("voip_queue_call_date >= ?")
        params.append(request.args["from_date"])
    if request.args.get("to_date"):
        filters.append("voip_queue_call_date <= ?")
        params.append(request.args["to_date"])

    cols = ", ".join(_QUEUE_COLUMNS)
    sql = (
        f"SELECT {cols} FROM voip_call_queue "
        f"WHERE {' AND '.join(filters)} "
        "ORDER BY voip_queue_created_at DESC, voip_queue_id DESC"
    )
    conn = get_conn()
    try:
        cur = conn.execute(q(sql), params)
        return jsonify(_rows(cur))
    finally:
        conn.close()


@voip_bp.route("/queue/<int:voip_queue_id>/grade", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def trigger_queue_grade(voip_queue_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        item = _get_queue_item(conn, voip_queue_id, company_id)
    finally:
        conn.close()

    if not item:
        return _err("Queue item not found", 404)
    if item["voip_queue_status"] not in ("pending", "failed"):
        return _err(
            f"Cannot grade item in status '{item['voip_queue_status']}'",
            409,
        )

    try:
        process_voip_call_async(voip_queue_id)
    except Exception:
        logger.exception("Failed to kick off manual grade for queue %s",
                         voip_queue_id)
        return _err("Could not start grading", 500)

    return jsonify({"ok": True, "voip_queue_id": voip_queue_id, "status": "processing"})


@voip_bp.route("/queue/<int:voip_queue_id>/skip", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def skip_queue_item(voip_queue_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        item = _get_queue_item(conn, voip_queue_id, company_id)
        if not item:
            return _err("Queue item not found", 404)
        if item["voip_queue_status"] in ("processing", "graded"):
            return _err(
                f"Cannot skip item in status '{item['voip_queue_status']}'",
                409,
            )
        conn.execute(
            q("UPDATE voip_call_queue SET voip_queue_status = 'skipped' "
              "WHERE voip_queue_id = ?"),
            (voip_queue_id,),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@voip_bp.route("/queue/<int:voip_queue_id>/audio", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def get_queue_audio(voip_queue_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT voip_queue_recording_data
                 FROM voip_call_queue
                 WHERE voip_queue_id = ? AND company_id = ?"""),
            (voip_queue_id, company_id),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return _err("Queue item not found", 404)
    blob = row["voip_queue_recording_data"] if IS_POSTGRES else row[0]
    if not blob:
        return _err("No audio stored yet for this queue item", 404)
    if isinstance(blob, memoryview):
        blob = bytes(blob)
    return send_file(
        io.BytesIO(blob),
        mimetype="audio/mpeg",
        download_name=f"voip_queue_{voip_queue_id}.mp3",
    )
