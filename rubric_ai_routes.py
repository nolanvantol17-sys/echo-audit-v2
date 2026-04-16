"""
rubric_ai_routes.py — Rubric generation via Claude, streamed over SSE.

Exposes:
    POST /api/rubric/generate-preview

Body:
    {
      "description":    "what we're grading (plain English)",
      "script":         "optional script/criteria paste",
      "feedback":       "optional — if refining, what to change",
      "current_rubric": [{...}, ...]   // optional — existing items when refining
    }

Response: text/event-stream. SSE events:
    event: name           (data: {"rubric_name": "..."})
    event: item           (data: {"name", "score_type", "weight", "scoring_guidance"})
    ...
    event: done           (data: {"count": N})
    event: error          (data: {"error": "..."})

Streaming is driven by Claude's `messages.stream` API. We ask Claude to emit
one JSON object per line in a predictable format, buffer the stream by
newline, parse each complete item, and forward it immediately as an SSE
event. Tenant-scoped via get_effective_company_id(); checks + increments
the anthropic rate limit once per preview generation.
"""

import json
import logging
import os
import re

import anthropic
from flask import Blueprint, Response, jsonify, request, stream_with_context
from flask_login import login_required

from helpers import check_rate_limit, get_effective_company_id, increment_usage

logger = logging.getLogger(__name__)

rubric_ai_bp = Blueprint("rubric_ai", __name__, url_prefix="/api")

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_VALID_SCORE_TYPES = {"out_of_10", "yes_no", "yes_no_pending"}


def _err(msg, code):
    return jsonify({"error": msg}), code


def _sse(event, data):
    """Format a single SSE frame. Values are JSON-encoded for predictability."""
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _build_prompt(description, script, feedback, current_rubric):
    """Build the Claude prompt. Asks for line-oriented output so we can stream
    items as they complete (standard JSON streaming is fragile because the
    document isn't valid until the final closing brace)."""
    description = (description or "").strip()
    script      = (script or "").strip()
    feedback    = (feedback or "").strip()
    is_refine   = bool(feedback and current_rubric)

    existing = ""
    if is_refine:
        existing = (
            "\n\nCURRENT RUBRIC (refine this — do not start from scratch):\n"
            + json.dumps(current_rubric, indent=2)
            + f"\n\nREVIEWER FEEDBACK: {feedback}\n"
        )

    script_block = ""
    if script:
        script_block = f"\n\nCALL SCRIPT / EXISTING CRITERIA:\n{script}\n"

    mode_line = (
        "Refine the rubric above based on the reviewer's feedback. Keep items that still apply, "
        "change items they flagged, and add or drop items as requested."
        if is_refine else
        "Build a QA grading rubric from the description."
    )

    return f"""You are a QA-rubric designer for a call-grading platform.

{mode_line}

GRADING CONTEXT:
{description or '(not provided)'}
{script_block}{existing}

OUTPUT FORMAT — line-oriented, streaming-friendly.
Emit EXACTLY these lines in order, one per line, nothing else:

RUBRIC_NAME: <short descriptive name, 2-6 words>
ITEM: {{"name":"...","score_type":"out_of_10","weight":1.0,"scoring_guidance":"..."}}
ITEM: {{"name":"...","score_type":"yes_no","weight":1.0,"scoring_guidance":"..."}}
...
DONE

Rules:
- Emit 5–12 items total. Aim for 7–9.
- score_type is one of: out_of_10 (graded 1-10), yes_no (binary), yes_no_pending (binary with a pending state for items that can't be confirmed yet).
- weight is a positive decimal. Default 1.0; use 1.5–2.0 for critical items and 0.5 for minor ones.
- scoring_guidance is 1–3 sentences. Describe what HIGH vs LOW looks like in concrete behaviours. No generic filler.
- Every ITEM must be a single line of valid JSON — no line breaks inside the object.
- Do NOT emit markdown, preamble, or code fences. Just the lines above.
- Terminate the stream with a literal line: DONE
"""


_ITEM_LINE_RE   = re.compile(r"^\s*ITEM:\s*(\{.*\})\s*$")
_NAME_LINE_RE   = re.compile(r"^\s*RUBRIC_NAME:\s*(.+?)\s*$")
_DONE_LINE_RE   = re.compile(r"^\s*DONE\s*$")


def _normalize_item(item):
    """Coerce an item dict into the shape the frontend + create route accept."""
    if not isinstance(item, dict):
        return None
    name = (item.get("name") or "").strip()
    if not name:
        return None
    score_type = (item.get("score_type") or "out_of_10").strip()
    if score_type not in _VALID_SCORE_TYPES:
        score_type = "out_of_10"
    try:
        weight = float(item.get("weight") or 1.0)
    except (TypeError, ValueError):
        weight = 1.0
    if weight <= 0:
        weight = 1.0
    guidance = (item.get("scoring_guidance") or "").strip() or None
    return {
        "name":             name,
        "score_type":       score_type,
        "weight":           weight,
        "scoring_guidance": guidance,
    }


@rubric_ai_bp.route("/rubric/generate-preview", methods=["POST"])
@login_required
def generate_rubric_preview():
    company_id = get_effective_company_id()
    if company_id is None:
        return _err("No company context", 400)

    body = request.get_json(silent=True) or {}
    description = body.get("description") or ""
    script      = body.get("script") or ""
    feedback    = body.get("feedback") or ""
    current     = body.get("current_rubric") or []

    if not (description.strip() or (feedback and current)):
        return _err("description is required (or feedback + current_rubric for refinement)", 400)

    ok, msg = check_rate_limit(company_id, "anthropic")
    if not ok:
        return _err(msg, 429)

    prompt = _build_prompt(description, script, feedback, current)

    @stream_with_context
    def generate():
        """Yield SSE frames as Claude streams lines back."""
        item_count = 0
        rubric_name_sent = False
        try:
            with _claude.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=3000,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
                timeout=90.0,
            ) as stream:
                buf = ""
                for text in stream.text_stream:
                    if not text:
                        continue
                    buf += text
                    # Process full lines; keep the last partial line in the buffer.
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        frame = _process_line(line)
                        if frame is None:
                            continue
                        event, payload = frame
                        if event == "name":
                            rubric_name_sent = True
                        elif event == "item":
                            item_count += 1
                        yield _sse(event, payload)
                        if event == "done":
                            # Short-circuit — Claude may still be generating
                            # post-DONE text that we want to ignore.
                            return
                # Flush any trailing line.
                if buf.strip():
                    frame = _process_line(buf)
                    if frame:
                        event, payload = frame
                        if event == "name":
                            rubric_name_sent = True
                        elif event == "item":
                            item_count += 1
                        yield _sse(event, payload)
                        if event == "done":
                            return
            # Stream ended without an explicit DONE — emit our own.
            if not rubric_name_sent:
                yield _sse("name", {"rubric_name": "Generated Rubric"})
            yield _sse("done", {"count": item_count})
        except Exception as exc:
            logger.exception("Rubric preview stream failed")
            yield _sse("error", {"error": str(exc) or "AI stream failed"})
            return

        # Only count usage on a successful completion — keeps rate-limit cost
        # honest if Claude cuts out early.
        try:
            increment_usage(company_id, "anthropic")
        except Exception:
            logger.exception("increment_usage failed after rubric preview")

    # SSE needs chunked transfer, no buffering, no caching.
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",   # disable nginx proxy buffering
        },
    )


def _process_line(line):
    """Parse one streamed line into an (event, data) tuple, or None to skip."""
    if not line or not line.strip():
        return None
    m = _NAME_LINE_RE.match(line)
    if m:
        return ("name", {"rubric_name": m.group(1)})
    m = _ITEM_LINE_RE.match(line)
    if m:
        try:
            raw = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
        normalized = _normalize_item(raw)
        if not normalized:
            return None
        return ("item", normalized)
    if _DONE_LINE_RE.match(line):
        return ("done", {"ok": True})
    return None
