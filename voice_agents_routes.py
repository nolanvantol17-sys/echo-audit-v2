"""
voice_agents_routes.py — read-only endpoints for the voice-agent picker.

Powers the AI Shop scheduling UI (J-2 frontend, ships separately).

Routes:
    GET /api/voice-agents                — active agents, sorted by name
    GET /api/voice-agents/sticky-default — current user's last-used agent,
                                           or first active agent as fallback

Both endpoints are @login_required only (no role gate). The actual AI Shop
trigger that uses the chosen agent is gated separately by /api/grade/ai-shop.

Globally scoped today — when first BYO-ElevenLabs tenant onboards, filter by
voice_agent_company_id (see schema.sql comment + voice_agents migration).
"""

import logging

from flask import Blueprint, jsonify
from flask_login import current_user, login_required

from db import get_conn, q

logger = logging.getLogger(__name__)

voice_agents_bp = Blueprint("voice_agents", __name__, url_prefix="/api")


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _rows(cur):
    return [_row_to_dict(r) for r in cur.fetchall()]


@voice_agents_bp.route("/voice-agents", methods=["GET"])
@login_required
def list_voice_agents():
    """All active voice agents, sorted by name. JSON list of dicts."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT voice_agent_id, voice_agent_name, voice_agent_description,
                        voice_agent_elevenlabs_id, voice_agent_is_active
                   FROM voice_agents
                  WHERE voice_agent_is_active = TRUE
                  ORDER BY voice_agent_name""")
        )
        return jsonify(_rows(cur))
    finally:
        conn.close()


@voice_agents_bp.route("/voice-agents/sticky-default", methods=["GET"])
@login_required
def voice_agents_sticky_default():
    """Per-user sticky default. Returns {voice_agent_id: <int|null>}.

    Lookup order:
      1. Most recent scheduled_calls row by current user where
         sc_voice_agent_id IS NOT NULL — and the referenced agent is still
         active (deactivated agents fall through to step 2).
      2. First active voice_agent (lowest voice_agent_name alphabetically) —
         matches the dropdown's natural sort order so first-time users see
         the same default they'd see at the top of the list.
      3. null — when no active agents exist (would be a seed problem).
    """
    user_id = current_user.user_id
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT sc.sc_voice_agent_id AS vid
                   FROM scheduled_calls sc
                   JOIN voice_agents va ON va.voice_agent_id = sc.sc_voice_agent_id
                  WHERE sc.sc_requested_by_user_id = ?
                    AND sc.sc_voice_agent_id IS NOT NULL
                    AND va.voice_agent_is_active = TRUE
                  ORDER BY sc.sc_requested_at DESC
                  LIMIT 1"""),
            (user_id,),
        )
        row = _row_to_dict(cur.fetchone())
        if row and row.get("vid"):
            return jsonify({"voice_agent_id": int(row["vid"])})

        # Fallback to first active agent (alphabetical).
        cur = conn.execute(
            q("""SELECT voice_agent_id
                   FROM voice_agents
                  WHERE voice_agent_is_active = TRUE
                  ORDER BY voice_agent_name
                  LIMIT 1""")
        )
        row = _row_to_dict(cur.fetchone())
        return jsonify({
            "voice_agent_id": int(row["voice_agent_id"]) if row else None
        })
    finally:
        conn.close()
