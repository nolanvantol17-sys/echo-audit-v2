"""
voip/classifier.py — One-shot Claude classifier for incoming VoIP calls.

classify_call(transcript, duration_seconds, termination_reason) returns one of:
    real_conversation, voicemail, no_answer, failed_call

Used by voip/processor.py to gate grading. Only real_conversation flows into
the existing grade pipeline; everything else lands as a no_answer interaction
with attribution preserved. Defensive: any failure → 'failed_call' + log.
"""

import logging
import os

import anthropic

logger = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_ALLOWED = {"real_conversation", "voicemail", "no_answer", "failed_call"}

# Cheap classifier model — narrow task, single-token output.
_MODEL = "claude-haiku-4-5-20251001"


def classify_call(
    transcript: str,
    duration_seconds: int | None,
    termination_reason: str | None,
    conversation_id: str | None = None,
) -> str:
    """Classify a call into one of four buckets. Never raises.

    Defensive: any exception, rate limit, or unparseable response →
    'failed_call' with logger.error/exception.

    Inputs:
        transcript          — speaker-labeled text. May be empty.
        duration_seconds    — call length in seconds; may be None.
        termination_reason  — provider-supplied hint (ElevenLabs only today,
                              e.g. 'Call ended by remote party'); may be None.
        conversation_id     — provider call id (e.g. ElevenLabs conv_*); logged
                              for post-hoc audit of misclassifications.
    """
    transcript_len = len(transcript or "")
    summary = (
        f"conv={conversation_id or '(none)'} len={transcript_len} "
        f"duration={duration_seconds} termination={termination_reason!r}"
    )

    prompt = (
        "You are a call classifier. Read the inputs and return exactly ONE "
        "lowercase label from this set, with NO extra text:\n"
        "  real_conversation  — two parties had a substantive back-and-forth "
        "exchange (about a property, leasing, a question — anything real). "
        "Requires a HUMAN response from the called party, not just an "
        "automated/recorded prompt.\n"
        "  voicemail          — the recording captured a voicemail prompt "
        "('leave a message after the tone', 'no one is available', "
        "'press the pound key', 'voicemail for ...').\n"
        "  no_answer          — call was not answered by a human, OR only "
        "automated content was captured. Includes: carrier/hold messages "
        "('we will be with you shortly', 'this call may be monitored or "
        "recorded'); IVR auto-attendant menus that list options ('press 1 "
        "for X, press 2 for Y') and never route to a human; empty/near-empty "
        "transcripts.\n"
        "  failed_call        — technical failure, garbage, or none of the above.\n"
        "\n"
        "GROUNDING RULES:\n"
        "- Use ONLY the inputs provided. Do not invent context.\n"
        "- Carrier hold messages alone are NOT real_conversation.\n"
        "- A voicemail prompt with no human reply is voicemail, not "
        "real_conversation.\n"
        "- An IVR menu/auto-attendant ('press 1 for leasing, press 2 for "
        "maintenance, ...') that never routes to a human is no_answer, even "
        "if the menu mentions topics like leasing or rentals.\n"
        "- If the called party (typically Speaker B) produces ONLY automated "
        "content (IVR menus, hold messages, voicemail prompts) and never a "
        "human voice, classify as no_answer regardless of what the caller "
        "(typically Speaker A — our outbound AI shopper) said. The caller "
        "speaking does NOT make it real_conversation.\n"
        "- Reply with the single label and nothing else.\n"
        "\n"
        f"DURATION_SECONDS: {duration_seconds}\n"
        f"TERMINATION_REASON: {termination_reason or '(none)'}\n"
        "\n"
        "TRANSCRIPT:\n"
        f"{transcript or '(empty)'}\n"
    )

    try:
        response = _claude.messages.create(
            model=_MODEL,
            max_tokens=20,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            timeout=30.0,
        )
        raw = (response.content[0].text or "").strip().lower()
    except Exception:
        logger.exception("[voip_classifier] Claude call failed (%s)", summary)
        return "failed_call"

    label = raw.split()[0] if raw else ""
    if label not in _ALLOWED:
        logger.error(
            "[voip_classifier] unparseable response %r (%s)", raw, summary,
        )
        return "failed_call"

    logger.info("[voip_classifier] result=%s %s", label, summary)
    return label
