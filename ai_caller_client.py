"""
ai_caller_client.py — Outbound HTTP client for the AI caller service.

Used by /api/grade/ai-shop to schedule outbound AI shops. Single function
initiate_call() — wraps the POST + auth + JSON parse with a clear
exception surface so the route layer can persist sc_status='failed'
on any failure mode.

Env vars (loaded at module scope):
    AI_CALLER_BASE_URL    — e.g. "https://ai-caller-service-production.up.railway.app"
    AI_CALLER_AUTH_TOKEN  — bearer token sent on every request

Missing env vars at import time log a warning but DO NOT raise — many
deploys won't have AI caller wired. initiate_call() raises a clear error
if called when env is missing.
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_BASE_URL = (os.getenv("AI_CALLER_BASE_URL") or "").rstrip("/")
_AUTH_TOKEN = os.getenv("AI_CALLER_AUTH_TOKEN") or ""
_TIMEOUT_S = 15

if not _BASE_URL or not _AUTH_TOKEN:
    logger.warning(
        "ai_caller_client: AI_CALLER_BASE_URL and/or AI_CALLER_AUTH_TOKEN "
        "are not set — initiate_call() will raise until configured."
    )


class AICallerError(RuntimeError):
    """Any failure reaching, parsing, or being accepted by the AI caller.

    Caller is expected to catch this and persist sc_status='failed' with
    the message. Original exception (if any) is chained via __cause__.
    """


def initiate_call(
    phone_number: str,
    *,
    sc_id: int,
    location_id: int,
    project_id: int,
    campaign_id: int | None,
    caller_user_id: int,
) -> dict:
    """POST /calls/initiate to the AI caller. Returns parsed JSON on success.

    Raises AICallerError on missing config, network failure, non-2xx
    response, or unparseable JSON.

    Attribution overrides (echo_audit_*) are sent unconditionally. The AI
    caller may honor them today or ignore them; ignored fields are no-ops,
    so no Echo Audit code change is needed when the AI caller spec ships.
    """
    if not _BASE_URL or not _AUTH_TOKEN:
        raise AICallerError(
            "AI caller not configured (AI_CALLER_BASE_URL / "
            "AI_CALLER_AUTH_TOKEN env vars are unset)"
        )

    url = f"{_BASE_URL}/calls/initiate"
    headers = {
        "Authorization": f"Bearer {_AUTH_TOKEN}",
        "Content-Type":  "application/json",
    }
    payload = {
        "to_phone_number":           phone_number,
        "echo_audit_sc_id":          sc_id,
        "echo_audit_location_id":    location_id,
        "echo_audit_project_id":     project_id,
        "echo_audit_campaign_id":    campaign_id,
        "echo_audit_caller_user_id": caller_user_id,
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT_S)
    except requests.RequestException as exc:
        raise AICallerError(f"AI caller unreachable: {exc}") from exc

    if not (200 <= resp.status_code < 300):
        body_preview = (resp.text or "")[:300]
        raise AICallerError(
            f"AI caller returned HTTP {resp.status_code}: {body_preview}"
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise AICallerError(
            f"AI caller returned non-JSON body: {(resp.text or '')[:300]}"
        ) from exc
