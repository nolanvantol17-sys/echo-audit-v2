"""
voip/audio_fetcher.py — Async ElevenLabs audio post-fetcher.

Called as a daemon thread from voip/processor.py after grading completes,
to fetch the call's MP3 audio from ElevenLabs and write it onto the
interaction. Best-effort — never raises out, never blocks the processor.

Env vars:
    ELEVENLABS_API_KEY — global per-deploy key (ElevenLabs only exposes
                         one set of API keys per workspace today; multi-
                         tenant would require per-tenant key storage).

ElevenLabs endpoint:
    GET https://api.elevenlabs.io/v1/convai/conversations/{id}/audio
    Header: xi-api-key: <key>
    Returns raw audio bytes (MP3).

Audio readiness can lag the post_call_transcription webhook by a few
seconds. We do one 5-second-sleep retry on 404; otherwise log + give up.
"""

import logging
import os
import threading
import time

import requests
from dotenv import load_dotenv

from db import IS_POSTGRES, get_conn, q

load_dotenv()

logger = logging.getLogger(__name__)

_API_KEY  = os.getenv("ELEVENLABS_API_KEY") or ""
_BASE_URL = "https://api.elevenlabs.io/v1/convai/conversations"
_TIMEOUT_S = 30          # audio downloads can be larger/slower than the
                         # AI caller's POST — 30s headroom
_RETRY_SLEEP_S = 5       # one retry on 404 (audio not ready yet)

if not _API_KEY:
    logger.warning(
        "voip.audio_fetcher: ELEVENLABS_API_KEY is unset — "
        "fetch_and_store_audio() will return False until configured."
    )


def fetch_and_store_audio_async(interaction_id, conversation_id):
    """Fire-and-forget background fetch + store. Never blocks the caller.

    No-op if interaction_id or conversation_id is falsy.
    """
    if not interaction_id or not conversation_id:
        return
    t = threading.Thread(
        target=_fetch_safely,
        args=(interaction_id, conversation_id),
        daemon=True,
    )
    t.start()


def _fetch_safely(interaction_id, conversation_id):
    try:
        fetch_and_store_audio(interaction_id, conversation_id)
    except Exception:
        logger.exception(
            "[audio_fetcher] crashed (interaction_id=%s conv_id=%s)",
            interaction_id, conversation_id,
        )


def fetch_and_store_audio(interaction_id, conversation_id):
    """Fetch the call audio from ElevenLabs and persist onto the
    interaction row. Returns True on success, False on any failure.

    Best-effort: failures are logged at WARNING with [audio_fetcher] tag,
    never raised. The interaction is still graded; only audio is missing.
    """
    if not _API_KEY:
        logger.warning(
            "[audio_fetcher] skipped — ELEVENLABS_API_KEY unset "
            "(interaction_id=%s)", interaction_id,
        )
        return False

    url = f"{_BASE_URL}/{conversation_id}/audio"
    headers = {"xi-api-key": _API_KEY}

    audio_bytes = _fetch_with_404_retry(url, headers, conversation_id)
    if not audio_bytes:
        return False

    return _persist(interaction_id, audio_bytes)


def _fetch_with_404_retry(url, headers, conversation_id):
    """One attempt + one 5s-delayed retry on 404. Returns bytes or None."""
    for attempt in (1, 2):
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT_S)
        except requests.RequestException as exc:
            logger.warning(
                "[audio_fetcher] network failure on attempt %d "
                "(conv_id=%s): %s", attempt, conversation_id, exc,
            )
            return None

        if resp.status_code == 200:
            return resp.content

        if resp.status_code == 404 and attempt == 1:
            logger.info(
                "[audio_fetcher] 404 on first attempt — audio likely not "
                "ready, retrying in %ds (conv_id=%s)",
                _RETRY_SLEEP_S, conversation_id,
            )
            time.sleep(_RETRY_SLEEP_S)
            continue

        body_preview = (resp.text or "")[:300]
        logger.warning(
            "[audio_fetcher] HTTP %d on attempt %d (conv_id=%s): %s",
            resp.status_code, attempt, conversation_id, body_preview,
        )
        return None

    return None


def _persist(interaction_id, audio_bytes):
    """Write bytes + audio_url marker to the interaction. Returns bool.

    Mirrors interactions_routes._save_audio() semantics: PG stores bytes
    inline + db:// marker; SQLite writes to disk + filesystem path. The
    UPDATE WHERE clause is idempotent — won't clobber an existing audio
    blob if the daemon thread runs twice or someone re-triggers manually.
    """
    if IS_POSTGRES:
        audio_url = f"db://interactions/{interaction_id}"
        audio_data = audio_bytes
    else:
        from pathlib import Path
        audio_dir = Path(os.getenv("AUDIO_DIR") or "./audio_uploads")
        audio_dir.mkdir(parents=True, exist_ok=True)
        fs_path = audio_dir / f"interaction_{interaction_id}.mp3"
        fs_path.write_bytes(audio_bytes)
        audio_url = str(fs_path)
        audio_data = None

    conn = get_conn()
    try:
        conn.execute(
            q("""UPDATE interactions
                    SET interaction_audio_url  = ?,
                        interaction_audio_data = ?
                  WHERE interaction_id = ?
                    AND interaction_audio_data IS NULL"""),
            (audio_url, audio_data, interaction_id),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(
            "[audio_fetcher] DB write failed (interaction_id=%s)",
            interaction_id, exc_info=True,
        )
        return False
    finally:
        conn.close()

    logger.info(
        "[audio_fetcher] stored %d bytes (interaction_id=%s)",
        len(audio_bytes), interaction_id,
    )
    return True
