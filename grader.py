"""
grader.py — Stateless AI grading primitives for Echo Audit V2.

Ported from V1 with DB / CSV / Excel / rubric-CRUD functions removed. This
module only contains the pure transcription + grading logic. All persistence
happens in the route layer.

Two-step Claude flow (V2):
    1. get_clarifying_questions(transcript, …)  → questions Claude needs
       answered before it can score the call accurately.
    2. grade_with_claude(transcript, context_answers, …)  → the actual
       scored grade, using the transcript plus the reviewer's answers.

When Claude returns an empty clarifying-questions list, the route layer
skips step 1's UI and goes straight to grade_with_claude with no answers.

Public API:
    transcribe(audio_path)                          -> str
    get_clarifying_questions(transcript, rubric_criteria=None,
                             rubric_script=None, rubric_context=None,
                             grade_target='respondent') -> list
    grade_with_claude(transcript, context_answers, rubric_criteria=None,
                      rubric_script=None, rubric_context=None,
                      grade_target='respondent') -> dict
    build_flags(scores, rubric_criteria=None)       -> str
    calculate_total(scores, rubric_criteria=None)   -> float
    validate_clarifying_questions(questions)        -> list
    AUDIO_EXTENSIONS                                -> set

Requires env vars ASSEMBLYAI_API_KEY and ANTHROPIC_API_KEY.
"""

import json
import logging
import os
import re
from pathlib import Path

import anthropic
import assemblyai as aai
from dotenv import load_dotenv

load_dotenv()

aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".webm"}

# Per-tenant transcription-hints limits. These are the single source of truth
# referenced by the API, the template, and validation.
KEYTERMS_PROMPT_MAX_TERMS = 200
KEYTERM_MIN_LENGTH = 5
KEYTERM_MAX_LENGTH = 50


class EmptyTranscriptError(RuntimeError):
    """Raised when transcription returns no usable text. Callers should surface
    a clear message rather than silently producing a graded interaction with
    empty content."""

# Default rubric used when caller does not provide one. Kept as legacy V1
# structure so a graded call still produces useful output even with no
# rubric_group attached.
_DEFAULT_CRITERIA = [
    {"name": "Speed of Answer",      "type": "numeric", "scale": 10},
    {"name": "Greeting & Opening",   "type": "numeric", "scale": 10},
    {"name": "Active Listening",     "type": "numeric", "scale": 10},
    {"name": "Product Knowledge",    "type": "numeric", "scale": 10},
    {"name": "Problem Resolution",   "type": "numeric", "scale": 10},
    {"name": "Empathy & Tone",       "type": "numeric", "scale": 10},
    {"name": "Closing & Next Steps", "type": "numeric", "scale": 10},
    {"name": "Overall Impression",   "type": "numeric", "scale": 10},
    {"name": "Follow-Up Promised",   "type": "yes_no"},
    {"name": "Issue Resolved",       "type": "yes_no"},
]

# Red-flag regex: questions that should never be yes_no. Ported verbatim from V1.
_YES_NO_RED_FLAGS = re.compile(
    r'\b(how well|how effectively|how clearly|how warm|how professional|how would you|'
    r'which best describes|what best describes|how did|how was|to what extent|'
    r'how much|how often|how frequently)\b', re.IGNORECASE,
)


# ── Prompt builder ──────────────────────────────────────────────


def build_rubric_prompt(criteria: list) -> str:
    """Build a rubric text block from a list of criteria dicts."""
    lines = ["SCORING RUBRIC", ""]
    for i, c in enumerate(criteria, 1):
        name = c["name"]
        ctype = c.get("type", "numeric")
        scale = c.get("scale", 10)
        guidance = (c.get("scoring_guidance") or "").strip()
        if ctype == "numeric":
            lines.append(f"{i}. {name} (1\u2013{scale})")
            lines.append(f"   Score on a 1\u2013{scale} scale where 1 is very poor and {scale} is excellent.")
            if guidance:
                lines.append(f"   Scoring guidance for {name}: {guidance}")
        elif ctype == "yes_no":
            lines.append(f"{i}. {name} (Yes / No)")
            lines.append("   Yes: This criterion was clearly met during the call.")
            lines.append("   No: This criterion was not met.")
            if guidance:
                lines.append(f"   Scoring guidance for {name}: {guidance}")
        else:
            lines.append(f"{i}. {name} (Yes / No / Pending)")
            lines.append("   Yes: Confirmed as completed.")
            lines.append("   No: Was not done despite being expected.")
            lines.append("   Pending: Cannot yet be confirmed \u2014 insufficient information.")
            if guidance:
                lines.append(f"   Scoring guidance for {name}: {guidance}")
        lines.append("")
    return "\n".join(lines)


# ── Transcription ──────────────────────────────────────────────


def transcribe(audio_path, keyterms_prompt: list | None = None) -> str:
    """Transcribe an audio file. Returns speaker-labeled text.

    keyterms_prompt: optional per-tenant custom vocabulary (list of strings,
    each 5-50 chars). Improves recognition of business-specific names/terms.
    Raises EmptyTranscriptError if the result is empty/whitespace-only so
    callers can surface a clear failure instead of producing an empty graded
    interaction.
    """
    config_kwargs = {
        "speaker_labels": True,
        "speech_models": ["universal-2"],
        "punctuate": True,
        "format_text": True,
        "disfluencies": False,
    }
    cleaned_terms = [t for t in (keyterms_prompt or []) if t and t.strip()]
    if cleaned_terms:
        # AssemblyAI universal-2 caps keyterms_prompt at 200 WORDS (not entries).
        # Multi-word phrases push the total over the limit even with a modest
        # entry count. Truncate in input order; followup needed for priority.
        capped, total_words = [], 0
        for t in cleaned_terms:
            wc = len(t.split())
            if total_words + wc > 200:
                break
            capped.append(t)
            total_words += wc
        if len(capped) < len(cleaned_terms):
            logger.warning(
                "transcribe: capped keyterms_prompt %d→%d entries (%d words, AAI 200-word limit)",
                len(cleaned_terms), len(capped), total_words,
            )
        config_kwargs["keyterms_prompt"] = capped
        logger.info(
            "transcribe: applying %d keyterms_prompt entries (%d words)",
            len(capped), total_words,
        )

    transcriber = aai.Transcriber()

    def _do_transcribe(kwargs):
        return transcriber.transcribe(
            str(audio_path), config=aai.TranscriptionConfig(**kwargs)
        )

    try:
        transcript = _do_transcribe(config_kwargs)
    except Exception as e:
        # Defensive fallback: if AAI rejects the request with a keyterms-related
        # error (e.g. limit changed upstream, or our cap missed an edge case),
        # retry once without keyterms so the user's grade still proceeds.
        msg = str(e)
        if "keyterms_prompt" in msg and "keyterms_prompt" in config_kwargs:
            logger.warning(
                "transcribe: AAI rejected keyterms_prompt (%s); retrying without hints",
                msg,
            )
            retry_kwargs = {k: v for k, v in config_kwargs.items() if k != "keyterms_prompt"}
            transcript = _do_transcribe(retry_kwargs)
        else:
            raise

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"{transcript.error}")

    if transcript.utterances:
        lines = []
        for u in transcript.utterances:
            mm = u.start // 60000
            ss = (u.start % 60000) // 1000
            lines.append(f"[{mm}:{ss:02d}] Speaker {u.speaker}: {u.text}")
        result = "\n".join(lines)
    else:
        result = transcript.text or ""

    if not result.strip():
        raise EmptyTranscriptError("Transcription returned no audible content.")

    return result


# ── Prompt helpers shared across both Claude calls ─────────────


def _normalize_grade_target(grade_target):
    if grade_target == "answerer":
        grade_target = "respondent"
    label = (
        "the person who answered the call" if grade_target == "respondent"
        else "the person who placed the call"
    )
    return grade_target, label


def _call_claude_json(prompt, *, max_tokens=4000, timeout=120.0):
    """Call Claude and parse its JSON reply. Raises ValueError on bad JSON."""
    response = _claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
        timeout=timeout,
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Claude response: %.500s", raw)
        raise ValueError(
            "AI service returned an invalid response. Please try again."
        ) from exc


# ── Clarifying questions (Step 1 of the two-step flow) ─────────


def get_clarifying_questions(
    transcript: str,
    rubric_criteria: list = None,
    rubric_script: str = None,
    rubric_context: str = None,
    grade_target: str = "respondent",
) -> list:
    """Ask Claude what it would need from a reviewer to grade this call.

    Returns a list of clarifying question objects (possibly empty). Does NOT
    score the call. Each question has keys: question, reason, format,
    and optionally options (for multiple_choice).

    The prompt asks Claude to restrict itself to things that cannot be
    determined from the transcript alone — e.g. whether a promised follow-up
    actually happened, whether the caller had context the reviewer knows about.
    """
    criteria_list = rubric_criteria or _DEFAULT_CRITERIA
    rubric_text = build_rubric_prompt(criteria_list)

    script_block = ""
    if rubric_script and rubric_script.strip():
        script_block = f"\n\nAGENT SCRIPT — They are expected to follow this script:\n{rubric_script.strip()}\n"

    call_context_block = ""
    if rubric_context and rubric_context.strip():
        call_context_block = f"\n\nCALL TYPE / CONTEXT:\n{rubric_context.strip()}\n"

    _, grade_target_label = _normalize_grade_target(grade_target)
    grade_target_block = (
        f"\n\nGRADE TARGET: Focus on {grade_target_label}. Only ask about "
        f"their performance.\n"
    )

    prompt = f"""You are a professional customer service QA specialist preparing to grade a recorded call. Before you grade, identify anything you need the human reviewer to confirm — things that CANNOT be determined from the transcript alone.{call_context_block}{grade_target_block}

{rubric_text}{script_block}

TRANSCRIPT:
{transcript}

Examples of good clarifying questions:
- Did the promised follow-up actually happen after the call?
- Was this caller a first-time shopper or an existing customer?
- Was there a known outage or known issue at the property that day?
- Did the agent previously have a conversation with this caller you aren't seeing here?

Examples of BAD clarifying questions (do not ask these — you can determine them from the transcript):
- How warm was the agent's greeting?
- Did the agent mention the company name?
- How long was the hold time?

CLOSED-FORM ONLY — the reviewer answers via a wizard with buttons. Open-text questions are forbidden.

Constraints:
- Maximum 5 questions. Return an empty array if the transcript is sufficient to grade everything.
- Each question must include: question, reason (why you can't tell from the transcript), format, and options.
- format MUST be one of:
    - "yes_no"           — strictly binary ("Did X happen?"). options must be exactly [].
    - "yes_no_unclear"   — binary, but the reviewer might genuinely not know
                           ("Was this a first-time caller?"). options must be exactly [].
    - "multiple_choice"  — 3 to 5 mutually exclusive concrete outcomes. options
                           is an array of 3–5 short answer-button strings (NOT
                           full sentences). Use this when there are a small,
                           known set of plausible answers.
- DO NOT ask open-ended, scaled, or numeric questions. If you cannot phrase a
  question as one of the three formats above, omit it.

Respond with a valid JSON object in exactly this format — no extra text before or after:
{{
  "clarifying_questions": [
    {{
      "question": "question text for the reviewer",
      "reason": "I couldn't determine this from the transcript because...",
      "format": "yes_no",
      "options": []
    }}
  ]
}}"""

    result = _call_claude_json(prompt, max_tokens=1200, timeout=60.0)
    return validate_clarifying_questions(result.get("clarifying_questions"))


# ── Grading (Step 2 of the two-step flow) ──────────────────────


def grade_with_claude(
    transcript: str,
    context_answers: dict = None,
    rubric_criteria: list = None,
    rubric_script: str = None,
    rubric_context: str = None,
    grade_target: str = "respondent",
) -> dict:
    """Grade a transcript using Claude, informed by the reviewer's answers
    to the clarifying questions. Returns scores, insights, and overall
    assessment. Does NOT return clarifying questions — those belong to
    get_clarifying_questions() which runs before this call.
    """
    context_answers = context_answers or {}

    context_block = ""
    if context_answers:
        context_block = "\n\nAdditional context from the QA reviewer (answers to the clarifying questions you asked earlier):\n"
        for question, answer in context_answers.items():
            context_block += f"- {question}: {answer}\n"

    criteria_list = rubric_criteria or _DEFAULT_CRITERIA
    rubric_text = build_rubric_prompt(criteria_list)

    script_block = ""
    if rubric_script and rubric_script.strip():
        script_block = f"\n\nAGENT SCRIPT — Grade whether the agent followed this script:\n{rubric_script.strip()}\n"

    call_context_block = ""
    if rubric_context and rubric_context.strip():
        call_context_block = f"\n\nCALL TYPE / CONTEXT:\n{rubric_context.strip()}\n"

    _, grade_target_label = _normalize_grade_target(grade_target)
    grade_target_block = f"\n\nGRADE TARGET: You are evaluating {grade_target_label}. Focus your scoring and feedback on this person's performance.\n"

    scores_parts, conf_parts, ts_parts, expl_parts = [], [], [], []
    for c in criteria_list:
        name = c["name"]
        ctype = c.get("type", "numeric")
        scale = c.get("scale", 10)
        if ctype == "numeric":
            scores_parts.append(f'    "{name}": <1-{scale}>')
        elif ctype == "yes_no":
            scores_parts.append(f'    "{name}": "Yes or No"')
        else:
            scores_parts.append(f'    "{name}": "Yes, No, or Pending"')
        conf_parts.append(f'    "{name}": "High or Medium or Low"')
        ts_parts.append(f'    "{name}": "MM:SS or General"')
        expl_parts.append(f'    "{name}": "1-2 sentence explanation referencing specific call moments"')

    scores_format = "{\n" + ",\n".join(scores_parts) + "\n  }"
    conf_format = "{\n" + ",\n".join(conf_parts) + "\n  }"
    ts_format = "{\n" + ",\n".join(ts_parts) + "\n  }"
    expl_format = "{\n" + ",\n".join(expl_parts) + "\n  }"

    prompt = f"""You are a professional customer service quality assurance specialist evaluating recorded or transcribed customer interactions.{call_context_block}{grade_target_block}

{rubric_text}{script_block}

TRANSCRIPT:
{transcript}
{context_block}

SCORING INSTRUCTIONS — CRITICAL:
- Score 9-10: Fully satisfied the criterion with no gaps.
- Score 7-8: Mostly satisfied but at least one identifiable gap.
- Score 5-6: Multiple noticeable gaps.
- Score 3-4: Largely failed.
- Score 1-2: Completely failed.
CRITICAL RULE: A score of 7 or below REQUIRES justification — name the specific thing the agent did poorly.

GREETING STANDARD:
Agent's name + company/department name, warm and professional. Award full credit if both elements are present in any phrasing.

STRENGTHS & WEAKNESSES:
2–3 bullets each, grounded in specific call moments. Format each bullet on its own line starting with "• ".

CONFIDENCE: For each criterion give High / Medium / Low.
TIMESTAMPS: Use the nearest [MM:SS] marker, or "General".

Use the reviewer's answers above to resolve anything you couldn't tell from the transcript alone. Do not ask any more clarifying questions — this is the final grading pass.

Respond with a valid JSON object in exactly this format — no extra text before or after:
{{
  "responder_name": "the name of {grade_target_label} extracted from the call. If they did not state their name, return exactly 'Name not provided'",
  "scores": {scores_format},
  "confidence": {conf_format},
  "timestamps": {ts_format},
  "explanations": {expl_format},
  "overall_assessment": "2-3 sentence professional summary of the call overall",
  "strengths": "• Bullet 1\\n• Bullet 2\\n• Bullet 3",
  "weaknesses": "• Bullet 1\\n• Bullet 2\\n• Bullet 3"
}}"""

    return _call_claude_json(prompt, max_tokens=4000, timeout=120.0)


# ── Flags + totals ─────────────────────────────────────────────


def build_flags(scores: dict, rubric_criteria: list = None) -> str:
    """Generate auto-flag notes based on Yes/No scores."""
    flags = []
    if rubric_criteria is None:
        if scores.get("Follow-Up Promised") == "No":
            flags.append("🚩 MISSING FOLLOW-UP")
        if scores.get("Issue Resolved") == "No":
            flags.append("🚩 ISSUE UNRESOLVED")
    else:
        for c in rubric_criteria:
            if c.get("required") and c.get("type") in ("yes_no", "yes_no_pending"):
                if scores.get(c["name"]) == "No":
                    flags.append(f"🚩 {c['name'].upper()} — NOT DONE")
    return "\n".join(flags)


def calculate_total(scores: dict, rubric_criteria: list = None) -> float:
    """Weighted average of numeric category scores. Default weight=1."""
    if rubric_criteria:
        numeric = [(c["name"], float(c.get("weight", 1)))
                   for c in rubric_criteria
                   if c.get("type", "numeric") == "numeric"]
    else:
        numeric = [(c["name"], 1.0) for c in _DEFAULT_CRITERIA if c.get("type") == "numeric"]
    weighted_sum, total_weight = 0.0, 0.0
    for name, w in numeric:
        val = scores.get(name)
        if isinstance(val, (int, float)):
            weighted_sum += val * w
            total_weight += w
    return round(weighted_sum / total_weight, 1) if total_weight else 0.0


# ── Clarifying-question validation ─────────────────────────────

# Closed-form formats produced by the new prompt. scale_1_10 is intentionally
# NOT in this set — if Claude returns it (or any other legacy/invalid shape)
# the reviewer-facing UI has no widget for it, so we coerce to skip_only.
_CLOSED_FORM_FORMATS = ("yes_no", "yes_no_unclear", "multiple_choice")
_MC_MIN_OPTIONS = 3
_MC_MAX_OPTIONS = 5


def validate_clarifying_questions(questions):
    """Closed-form-only validation for the wizard UI.

    Each question is coerced into one of:
        yes_no | yes_no_unclear | multiple_choice | skip_only

    Anything Claude returns that does not cleanly fit (open-text, scale_1_10,
    multiple_choice with too few/too many/garbage options, missing format,
    red-flag yes_no, etc.) is downgraded to skip_only — the reviewer sees
    the question + reason as read-only context and clicks Skip to advance.
    The original question text and reason are always preserved so the
    reviewer can still factor them in mentally.
    """
    if not questions or not isinstance(questions, list):
        return []
    valid = []
    for qn in questions:
        if not isinstance(qn, dict) or not qn.get("question"):
            continue

        fmt = (qn.get("format") or "").lower().strip()
        # Map legacy variants Claude has returned in the past.
        if fmt == "scale_1_5":
            fmt = "scale_1_10"

        # Normalize options to a list-of-clean-strings, regardless of format.
        opts_raw = qn.get("options") or []
        if not isinstance(opts_raw, list):
            opts_raw = []
        opts_clean = [str(o).strip() for o in opts_raw if str(o).strip()]

        # Apply rules in order. First match wins; fallthrough → skip_only.
        coerced_fmt = None
        coerced_opts = []

        if fmt == "yes_no":
            # Quality/degree questions phrased as yes/no are useless — skip them.
            if _YES_NO_RED_FLAGS.search(qn["question"]):
                coerced_fmt = "skip_only"
            else:
                coerced_fmt = "yes_no"

        elif fmt == "yes_no_unclear":
            coerced_fmt = "yes_no_unclear"

        elif fmt == "multiple_choice":
            if _MC_MIN_OPTIONS <= len(opts_clean) <= _MC_MAX_OPTIONS:
                coerced_fmt = "multiple_choice"
                coerced_opts = opts_clean
            else:
                coerced_fmt = "skip_only"

        else:
            # Unknown / open-text / scale_1_10 / missing → skip_only.
            coerced_fmt = "skip_only"

        qn["format"] = coerced_fmt
        qn["options"] = coerced_opts
        valid.append(qn)
    return valid
