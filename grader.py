"""
grader.py — Stateless AI grading primitives for Echo Audit V2.

Ported from V1 with DB / CSV / Excel / rubric-CRUD functions removed. This
module only contains the pure transcription + grading logic. All persistence
happens in the route layer.

Single-pass flow:
    transcribe(audio_path) → grade_with_claude(transcript, …)

Public API:
    transcribe(audio_path)                          -> str
    grade_with_claude(transcript, rubric_criteria=None,
                      rubric_script=None, rubric_context=None,
                      grade_target='respondent') -> dict
    build_flags(scores, rubric_criteria=None)       -> str
    calculate_total(scores, rubric_criteria=None)   -> float
    AUDIO_EXTENSIONS                                -> set

Requires env vars ASSEMBLYAI_API_KEY and ANTHROPIC_API_KEY.
"""

import json
import logging
import os
import time

import anthropic
import assemblyai as aai
from assemblyai import api as aai_api
from dotenv import load_dotenv

load_dotenv()

aai.settings.api_key = os.getenv("ASSEMBLYAI_API_KEY")
# Bound every individual AssemblyAI HTTP call (file upload + each status poll)
# so a single hung request can't wedge the grade worker. The overall per-attempt
# ceiling lives in transcribe() via _TRANSCRIBE_TIMEOUT_SECONDS.
aai.settings.http_timeout = 60.0
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".mp4", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".webm"}

# Per-tenant transcription-hints limits. These are the single source of truth
# referenced by the API, the template, and validation.
KEYTERMS_PROMPT_MAX_TERMS = 200
KEYTERM_MIN_LENGTH = 5
KEYTERM_MAX_LENGTH = 50

# Transcription resilience. AssemblyAI occasionally accepts an audio file and
# then never finishes it (the job wedges in 'processing' on their side). We
# drive the poll loop ourselves with a hard per-attempt deadline so a wedged
# job can't hang the grade worker forever, and resubmit a fresh transcript a
# couple of times — a re-submission almost always clears a transient stall.
_TRANSCRIBE_TIMEOUT_SECONDS = 180   # per-attempt wall-clock cap on AAI polling
_TRANSCRIBE_MAX_ATTEMPTS    = 2     # total submissions before giving up


class EmptyTranscriptError(RuntimeError):
    """Raised when transcription returns no usable text. Callers should surface
    a clear message rather than silently producing a graded interaction with
    empty content."""


class TranscriptionTimeout(RuntimeError):
    """Raised when AssemblyAI accepts the audio but never reaches a terminal
    state (completed/error) within the per-attempt deadline — i.e. their job
    wedged. Distinct from a hard error so transcribe() can resubmit, and so the
    worker surfaces a clear 'timed out' failure instead of hanging forever."""

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
            lines.append(f"{i}. {name} (0.0\u20139.9)")
            lines.append(f"   Score on a 0.0\u20139.9 scale to one decimal place where 0.0 is total failure and 9.9 is excellent.")
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


def _await_terminal(transcriber, transcript):
    """Poll AssemblyAI until `transcript` reaches a terminal state
    (completed/error) or our per-attempt deadline passes; return the terminal
    Transcript, or raise TranscriptionTimeout.

    CRITICAL: poll with the SDK's SINGLE-SHOT api.get_transcript (one bounded GET,
    capped by aai.settings.http_timeout) — NOT Transcript.get_by_id(), which
    internally runs the SDK's own UNBOUNDED wait_for_completion() loop and would
    block forever on a wedged job (the exact failure that stranded #306, where
    control never returns to re-check our deadline). Once AAI is terminal we hand
    back the rich Transcript via get_by_id, which returns immediately because the
    transcript is already done."""
    client = transcriber._client.http_client
    tid = transcript.id
    poll = max(2.0, float(aai.settings.polling_interval or 3))
    deadline = time.monotonic() + _TRANSCRIBE_TIMEOUT_SECONDS
    terminal = (aai.TranscriptStatus.completed, aai.TranscriptStatus.error)
    status = transcript.status
    while status not in terminal:
        if time.monotonic() >= deadline:
            raise TranscriptionTimeout(
                f"AssemblyAI did not finish within {_TRANSCRIBE_TIMEOUT_SECONDS}s "
                f"(transcript {tid}, status={status})"
            )
        time.sleep(poll)
        status = aai_api.get_transcript(client, tid).status
    return aai.Transcript.get_by_id(tid)


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
    cfg = aai.TranscriptionConfig(**config_kwargs)
    cfg_no_hints = (
        aai.TranscriptionConfig(
            **{k: v for k, v in config_kwargs.items() if k != "keyterms_prompt"})
        if "keyterms_prompt" in config_kwargs else None
    )

    def _submit(config):
        # submit() returns immediately (status 'queued'); we own the poll loop
        # in _await_terminal so a wedged job hits our deadline instead of
        # blocking forever like the SDK's all-in-one transcribe() would.
        return transcriber.submit(str(audio_path), config=config)

    transcript = None
    last_error = None
    for attempt in range(1, _TRANSCRIBE_MAX_ATTEMPTS + 1):
        try:
            try:
                t = _submit(cfg)
            except Exception as e:
                # Defensive fallback: if AAI rejects the keyterms_prompt (limit
                # changed upstream, or our cap missed an edge case), resubmit
                # once without hints so the user's grade still proceeds.
                msg = str(e)
                if cfg_no_hints is not None and "keyterms_prompt" in msg:
                    logger.warning(
                        "transcribe: AAI rejected keyterms_prompt (%s); retrying without hints",
                        msg,
                    )
                    t = _submit(cfg_no_hints)
                else:
                    raise
            t = _await_terminal(transcriber, t)
            if t.status == aai.TranscriptStatus.error:
                raise RuntimeError(t.error or "AssemblyAI returned an error status")
            transcript = t
            break  # completed
        except TranscriptionTimeout as e:
            last_error = e
            logger.warning("transcribe: attempt %d/%d wedged (%s) — resubmitting",
                           attempt, _TRANSCRIBE_MAX_ATTEMPTS, e)
        except Exception as e:
            last_error = e
            logger.warning("transcribe: attempt %d/%d failed (%s)",
                           attempt, _TRANSCRIBE_MAX_ATTEMPTS, e)

    if transcript is None:
        # All attempts exhausted — re-raise the last failure so the worker marks
        # the job failed with a meaningful message and resets the interaction
        # for retry (instead of hanging the worker indefinitely).
        raise last_error if last_error is not None else RuntimeError(
            "Transcription failed")

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


# ── Grading ────────────────────────────────────────────────────


def grade_with_claude(
    transcript: str,
    rubric_criteria: list = None,
    rubric_script: str = None,
    rubric_context: str = None,
    grade_target: str = "respondent",
) -> dict:
    """Grade a transcript using Claude. Returns scores, insights, and overall
    assessment.
    """
    criteria_list = rubric_criteria or _DEFAULT_CRITERIA
    rubric_text = build_rubric_prompt(criteria_list)

    _, grade_target_label = _normalize_grade_target(grade_target)

    script_block = ""
    if rubric_script and rubric_script.strip():
        # Label the script for whoever is being graded so the model checks the
        # right speaker's adherence (a caller script must not read as 'AGENT').
        script_block = (
            f"\n\nREFERENCE SCRIPT — Grade whether {grade_target_label} "
            f"followed this script:\n{rubric_script.strip()}\n"
        )

    call_context_block = ""
    if rubric_context and rubric_context.strip():
        call_context_block = f"\n\nCALL TYPE / CONTEXT:\n{rubric_context.strip()}\n"
    grade_target_block = f"\n\nGRADE TARGET: You are evaluating {grade_target_label}. Focus your scoring and feedback on this person's performance.\n"

    scores_parts, conf_parts, ts_parts, expl_parts = [], [], [], []
    for c in criteria_list:
        name = c["name"]
        ctype = c.get("type", "numeric")
        scale = c.get("scale", 10)
        if ctype == "numeric":
            scores_parts.append(f'    "{name}": <0.0-9.9, one decimal>')
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

SCORING INSTRUCTIONS — CRITICAL:
- Use a continuous 0.0–9.9 scale and ALWAYS report one decimal place (e.g. 8.4, 9.1, 6.7). The maximum possible score is 9.9 — never return 10.0 or higher.
- Score 9.0–9.9: Fully satisfied the criterion with no gaps.
- Score 7.0–8.9: Mostly satisfied but at least one identifiable gap.
- Score 5.0–6.9: Multiple noticeable gaps.
- Score 3.0–4.9: Largely failed.
- Score 0.0–2.9: Completely failed.
Use the decimal to express where within a band the performance lands — a strong-but-imperfect 8.4 is meaningfully different from a borderline 7.1.
CRITICAL RULE: A score of 7.0 or below REQUIRES justification — name the specific thing the agent did poorly.

GREETING STANDARD:
Agent's name + company/department name, warm and professional. Award full credit if both elements are present in any phrasing.

STRENGTHS & WEAKNESSES:
2–3 bullets each, grounded in specific call moments. Format each bullet on its own line starting with "• ".

CONFIDENCE: For each criterion give High / Medium / Low.
TIMESTAMPS: Use the nearest [MM:SS] marker, or "General".

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


# ── Export-time call summary (Haiku) ───────────────────────────


def summarize_call_for_export(transcript: str,
                              scores_per_criterion: dict,
                              location_name: str = None,
                              respondent_name: str = None) -> str:
    """Generate a one-sentence call summary for spreadsheet export.

    Run sequentially during export — uses Haiku for cost. Single sentence
    so the cell stays readable in a spreadsheet view. On failure returns
    "(summary unavailable)" so the export still succeeds.

    No-answer rows (status_id=44) should call summarize_no_answer_for_export
    instead — this helper assumes a real, graded conversation.
    """
    score_lines = []
    for name, value in (scores_per_criterion or {}).items():
        if value is None:
            continue
        score_lines.append(f"- {name}: {value}")
    score_block = "\n".join(score_lines) if score_lines else "(no rubric scores)"

    header_bits = []
    if location_name:   header_bits.append(f"Location: {location_name}")
    if respondent_name: header_bits.append(f"Respondent: {respondent_name}")
    header_block = "\n".join(header_bits)

    prompt = f"""You are summarizing a customer service call for a spreadsheet export.

Write ONE sentence covering the most important takeaway about the call —
either the strongest moment of the agent's performance or the most notable
shortcoming, whichever stands out most.

Constraints:
- Exactly ONE sentence. No preamble, no second sentence.
- No markdown, no bullets, no headings.
- Readable inside a single spreadsheet cell.

{header_block}

RUBRIC SCORES (already graded — use as context, do not restate verbatim):
{score_block}

TRANSCRIPT:
{transcript}

Respond with the single sentence only — no preamble, no quotes."""

    try:
        response = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        text = (response.content[0].text or "").strip()
        return text or "(summary unavailable)"
    except Exception:
        logger.exception("summarize_call_for_export failed")
        return "(summary unavailable)"


def summarize_no_answer_for_export(transcript: str) -> str:
    """Return the canonical Call-Summary label for a status=44 row.

    Always "No answer" — Carlos's G2.2 directive collapsed the prior
    Voicemail/No-answer split into a single label. Helper kept (vs inlining
    at the export site) for future flexibility if richer labels return.
    """
    return "No answer"


# ── Export-time location-rollup summary (Haiku) ───────────────


_LOC_PROMPT_CALL_CAP = 10


def summarize_location_for_export(
    location_name: str,
    graded_calls: list,
    no_answer_count: int,
    aggregate_avg: float | None,
) -> str:
    """1-2 sentence Haiku summary of a location's aggregate performance.

    graded_calls: list of dicts each carrying {transcript, scores_per_criterion,
                  total_score}. Already filtered to graded conversations
                  (status_id != 44). May be empty.
    no_answer_count: how many calls in scope ended as no_answer (status_id=44).
    aggregate_avg: weighted average across graded_calls (None if no graded).

    Deterministic short-circuit when graded_calls is empty: skip the Haiku
    call and return a deterministic string describing the no-answer cohort.
    Mirrors summarize_call_for_export's failure semantics: returns the literal
    "(summary unavailable)" on any Haiku exception so the caller can render
    the row without losing the export.
    """
    if not graded_calls:
        # Pure no-answer cohort — no real conversations to summarize. Deterministic
        # string keeps token spend off pure no_answer locations and gives the
        # spreadsheet reader a clear, non-confusing cell.
        n = no_answer_count or 0
        plural = "s" if n != 1 else ""
        return f"{n} attempted call{plural}; no answer reached on any."

    # Cap context — most-recent calls are most actionable for a summary. Caller
    # is expected to pre-sort newest-first; we re-sort defensively if a date
    # field is present, otherwise trust caller order.
    recent = graded_calls[:_LOC_PROMPT_CALL_CAP]

    avg_str = f"{aggregate_avg:.1f}" if aggregate_avg is not None else "n/a"
    total_graded = len(graded_calls)

    history_lines = []
    for c in recent:
        score = c.get("total_score")
        score_str = f"{float(score):.1f}" if score is not None else "n/a"
        scores = c.get("scores_per_criterion") or {}
        score_bits = [f"{name}={value}" for name, value in scores.items()
                      if value is not None]
        scores_block = "; ".join(score_bits) if score_bits else "(no rubric scores)"
        # Cap individual transcripts so a single long call doesn't crowd the prompt.
        tx = (c.get("transcript") or "")[:1500]
        history_lines.append(
            f"Call score {score_str}/10 | {scores_block}\nTranscript excerpt:\n{tx}"
        )
    history_block = "\n\n".join(history_lines)

    prompt = (
        "You are summarizing a single location's customer-service performance "
        "for a spreadsheet export. The summary will appear in a single cell "
        "next to the location name + aggregate score.\n\n"
        "GROUNDING RULES (critical):\n"
        "- Describe only what the call data shows. Do NOT speculate about "
        "causes (e.g. 'staff turnover', 'busy season') unless the calls "
        "themselves state it.\n"
        "- Do NOT extrapolate beyond the included calls.\n"
        "- If the data is sparse or mixed, say so plainly rather than "
        "inventing narrative.\n\n"
        "REQUIREMENTS:\n"
        "- Exactly ONE OR TWO sentences. No preamble, no bullets, no markdown.\n"
        "- Reference the aggregate (avg score / call volume) in context.\n"
        "- Highlight the single most notable strength OR shortcoming across "
        "the calls — whichever stands out most.\n"
        "- Readable inside a single spreadsheet cell.\n\n"
        f"LOCATION: {location_name}\n"
        f"AGGREGATE: {total_graded} graded call(s), avg score {avg_str}/10, "
        f"{no_answer_count} no-answer attempt(s).\n\n"
        f"GRADED CALLS (most recent {len(recent)} of {total_graded}):\n\n"
        f"{history_block}\n\n"
        "Respond with the 1-2 sentence summary only — no preamble, no quotes."
    )

    try:
        response = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        text = (response.content[0].text or "").strip()
        return text or "(summary unavailable)"
    except Exception:
        logger.exception("summarize_location_for_export failed (location=%s)",
                         location_name)
        return "(summary unavailable)"


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
