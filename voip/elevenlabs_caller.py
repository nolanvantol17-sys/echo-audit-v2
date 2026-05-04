"""
voip/elevenlabs_caller.py — In-process ElevenLabs outbound caller.

Replaces the standalone AI caller HTTP service (Migration M1 → M2 → M3)
with a direct SDK call inside Echo Audit. Used by /api/grade/ai-shop to
place outbound calls when a manager schedules an AI shop.

Env vars (loaded at module scope):
    ELEVENLABS_API_KEY                — workspace API key
    ELEVENLABS_AGENT_ID               — Conversational AI agent ID
    ELEVENLABS_AGENT_PHONE_NUMBER_ID  — phone number ID registered to the
                                        agent inside ElevenLabs (ElevenLabs
                                        handles Twilio integration; no
                                        Twilio creds needed here)

Missing env vars at import log a warning but do NOT raise — many deploys
won't have outbound calling wired. initiate_call() raises a clear
ElevenLabsCallError when called with missing config.

The 4-key echo_audit_* dynamic_variables payload is what the post-call
webhook handler (voip_routes.py) reads to attribute the inbound webhook
back to the right Echo Audit project / location / caller / campaign.
"""

import logging
import os

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs

load_dotenv()

logger = logging.getLogger(__name__)

_API_KEY               = os.getenv("ELEVENLABS_API_KEY") or ""
_AGENT_ID              = os.getenv("ELEVENLABS_AGENT_ID") or ""
_AGENT_PHONE_NUMBER_ID = os.getenv("ELEVENLABS_AGENT_PHONE_NUMBER_ID") or ""
_TIMEOUT_S             = 30.0  # ElevenLabs SDK default is 240s — way too long

if not (_API_KEY and _AGENT_ID and _AGENT_PHONE_NUMBER_ID):
    logger.warning(
        "voip.elevenlabs_caller: one or more required env vars unset "
        "(ELEVENLABS_API_KEY, ELEVENLABS_AGENT_ID, "
        "ELEVENLABS_AGENT_PHONE_NUMBER_ID) — initiate_call() will raise "
        "until configured."
    )


class ElevenLabsCallError(RuntimeError):
    """Any failure placing the outbound ElevenLabs call.

    Caller is expected to catch this and persist sc_status='failed' with
    the message. Original exception (if any) is chained via __cause__.
    """


def initiate_call(
    to_phone_number: str,
    *,
    location_id: int,
    project_id: int,
    campaign_id: int | None,
    caller_user_id: int,
    agent_id: str | None = None,
) -> dict:
    """Place an outbound AI call via ElevenLabs. Returns dict on success.

    agent_id (J-1) — per-call ElevenLabs agent override. When None, falls
    back to the ELEVENLABS_AGENT_ID env var so callers that haven't yet
    been wired to the voice_agents picker keep working.

    Returns:
        {
            "status": "initiated",
            "conversation_id": "<elevenlabs conv id>",
            "echo_audit_location_id":    int,
            "echo_audit_project_id":     int,
            "echo_audit_campaign_id":    int | None,
            "echo_audit_caller_user_id": int,
        }

    The echo_audit_* fields are echoed back from the dynamic_variables
    payload so the route layer can persist a self-contained debug snapshot
    (sc_ai_caller_response JSONB) without re-deriving them.

    Raises:
        ElevenLabsCallError on missing env config, SDK error, or response
        without a conversation_id.
    """
    effective_agent_id = agent_id or _AGENT_ID
    if not (_API_KEY and effective_agent_id and _AGENT_PHONE_NUMBER_ID):
        raise ElevenLabsCallError(
            "ElevenLabs not configured (ELEVENLABS_API_KEY / "
            "ELEVENLABS_AGENT_ID / ELEVENLABS_AGENT_PHONE_NUMBER_ID env vars)"
        )

    dynamic_variables = {
        "echo_audit_location_id":    int(location_id),
        "echo_audit_project_id":     int(project_id),
        "echo_audit_campaign_id":    int(campaign_id) if campaign_id is not None else None,
        "echo_audit_caller_user_id": int(caller_user_id),
    }

    client = ElevenLabs(api_key=_API_KEY, timeout=_TIMEOUT_S)
    try:
        response = client.conversational_ai.twilio.outbound_call(
            agent_id=effective_agent_id,
            agent_phone_number_id=_AGENT_PHONE_NUMBER_ID,
            to_number=to_phone_number,
            conversation_initiation_client_data={
                "dynamic_variables": dynamic_variables,
            },
        )
    except Exception as exc:
        raise ElevenLabsCallError(
            f"ElevenLabs outbound_call failed: {exc}"
        ) from exc

    # Defensive response unpack — mirrors the AI caller's pattern. Typed
    # return is TwilioOutboundCallResponse with .conversation_id attr;
    # dict fallback handles any future SDK shape change cheaply.
    conversation_id = getattr(response, "conversation_id", None)
    if not conversation_id and isinstance(response, dict):
        conversation_id = response.get("conversation_id")
    if not conversation_id:
        raise ElevenLabsCallError(
            f"ElevenLabs response missing conversation_id: {response!r}"
        )

    return {
        "status": "initiated",
        "conversation_id": conversation_id,
        **dynamic_variables,
    }
