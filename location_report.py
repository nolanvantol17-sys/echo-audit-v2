"""
location_report.py — AI narrative for the bulk-export Location Report Card.

Stateless helper. Builds a Claude prompt from a set of graded calls plus
aggregate stats, asks for structured JSON (overall_assessment + strengths +
improvements), parses, returns a dict or None.

Sync — runs in the request thread because the export response can't ship
without it. ~5-15s typical latency for a 25-call window.

Anti-fabrication clauses are baked into the prompt to keep Claude grounded
in the per-call assessments rather than speculating about causes outside
the data.
"""

import json
import logging
import os

import anthropic

from helpers import check_rate_limit, increment_usage

logger = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Cap calls fed to Claude — matches intel.py for consistency. Deterministic
# stats in the PDF cover ALL calls; this cap only bounds the narrative
# context window.
_PROMPT_CALL_CAP = 25


def generate_narrative(company_id, scored_calls, aggregate_stats):
    """Returns dict {overall_assessment, strengths, improvements} or None.

    None on rate-limit, malformed JSON, exceptions, or zero scored calls.
    Caller renders an 'unavailable' fallback in the PDF when None.
    """
    if not scored_calls:
        return None

    ok, _msg = check_rate_limit(company_id, "anthropic")
    if not ok:
        logger.warning(
            "Skipping location report narrative — anthropic rate limit hit "
            "(company=%s)", company_id,
        )
        return None

    # Sort newest-first, cap at the prompt budget. Most-recent context is
    # most actionable for the narrative. str() coerces date/datetime/None
    # uniformly so cross-type compares can't raise.
    recent = sorted(
        scored_calls,
        key=lambda c: (str(c.get("interaction_date") or ""),
                       c.get("interaction_id") or 0),
        reverse=True,
    )[:_PROMPT_CALL_CAP]

    history_block = "\n\n".join(
        f"Call on {c.get('interaction_date')} | "
        f"Score: {c.get('interaction_overall_score')}/10\n"
        f"Strengths: {c.get('interaction_strengths') or '—'}\n"
        f"Weaknesses: {c.get('interaction_weaknesses') or '—'}\n"
        f"Assessment: {c.get('interaction_overall_assessment') or '—'}"
        for c in recent
    )

    avg = aggregate_stats.get("avg_score")
    avg_str = f"{avg:.1f}" if avg is not None else "n/a"
    first = aggregate_stats.get("date_range_first") or "?"
    last  = aggregate_stats.get("date_range_last") or "?"
    total_graded = aggregate_stats.get("total_graded", len(recent))

    prompt = (
        "You are writing a performance summary for a single location based "
        "on graded calls. This is a customer-facing report. Write in a "
        "professional, factual tone — no marketing fluff.\n\n"
        "GROUNDING RULES (critical):\n"
        "- Describe only what the call data shows. Do NOT speculate about "
        "causes (e.g. 'staff turnover', 'busy season') unless the call "
        "assessments themselves state it.\n"
        "- Do NOT extrapolate beyond the included calls.\n"
        "- If the data is sparse or mixed, say so plainly rather than "
        "inventing narrative.\n\n"
        "REQUIREMENTS:\n"
        "- overall_assessment: 3-4 sentences capturing overall performance, "
        "what's working, what isn't, and any clear trends. Reference the "
        "average score and call volume in context.\n"
        "- strengths: 3-5 bullet points of consistent strengths across the "
        "graded calls. Each on its own line starting with '• '.\n"
        "- improvements: 3-5 bullet points of concrete areas for "
        "improvement. Each on its own line starting with '• '.\n\n"
        f"CONTEXT — {len(recent)} graded call(s) shown below "
        f"(of {total_graded} in scope), avg score {avg_str}/10, "
        f"date range {first} → {last}:\n\n"
        f"{history_block}\n\n"
        "Respond with valid JSON only — no extra prose:\n"
        "{\n"
        '  "overall_assessment": "...",\n'
        '  "strengths": "• bullet\\n• bullet",\n'
        '  "improvements": "• bullet\\n• bullet"\n'
        "}"
    )

    try:
        response = _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
    except Exception:
        logger.exception("Location report narrative Claude call failed")
        return None

    increment_usage(company_id, "anthropic")
    return {
        "overall_assessment": (data.get("overall_assessment") or "").strip() or None,
        "strengths":          (data.get("strengths")          or "").strip() or None,
        "improvements":       (data.get("improvements")       or "").strip() or None,
    }
