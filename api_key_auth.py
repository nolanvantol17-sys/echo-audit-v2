"""
api_key_auth.py — External API key authentication primitive.

External integrations (e.g. the outbound calling system) authenticate to
Echo Audit via long-lived API keys instead of user sessions. Keys are
issued via the create_api_key.py CLI and verified here.

Storage model (api_keys table):
    ak_prefix   — first 11 chars of plaintext ("ea_" + 8 entropy chars).
                  Indexed; narrows the candidate row on verify.
    ak_hash     — sha256 hex of full plaintext. Constant-time compared.
    ak_name     — operator-supplied label ("External Caller", "Mayfair Bot", …)
    status_id   — 1=active, 50=revoked. ak_revoked_at also set on revocation.

Plaintext key format:    ea_<32 urlsafe chars>     total 35 chars
Example:                 ea_kJxKZQzG8f7VbJqHRnW2yPmL3Tt6Bv7C

Plaintext is shown to the operator ONCE at creation time and never persisted.

Usage:
    from flask import g
    from api_key_auth import require_api_key

    @api_bp.route("/external/locations/lookup")
    @require_api_key
    def lookup_location():
        company_id = g.api_key_company_id
        ...
"""

import hashlib
import hmac
import logging
import secrets
from functools import wraps

from flask import g, jsonify, request

from db import IS_POSTGRES, get_conn, q

logger = logging.getLogger(__name__)

KEY_PLAINTEXT_PREFIX = "ea_"
PREFIX_STORED_LEN    = 11   # "ea_" + 8 entropy chars
ENTROPY_BYTES        = 24   # secrets.token_urlsafe(24) → 32 base64 chars (always)

# Fixed dummy hash used to keep the failure path constant-time. Real hashes
# are sha256 hex (64 chars); this matches that shape so compare_digest
# pays the same cost.
_DUMMY_HASH = "0" * 64


# ── Key generation + hashing ──────────────────────────────────


def generate_key():
    """Return a fresh plaintext API key. Show to the operator ONCE."""
    return KEY_PLAINTEXT_PREFIX + secrets.token_urlsafe(ENTROPY_BYTES)


def hash_key(plaintext):
    """sha256 hex digest of the plaintext key."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def prefix_of(plaintext):
    """First 11 chars of plaintext — what we store in ak_prefix for lookup."""
    return plaintext[:PREFIX_STORED_LEN]


# ── Verification ──────────────────────────────────────────────


def verify_key(plaintext):
    """Return company_id if plaintext matches an active key, else None.

    Bumps ak_last_used_at on success. Constant-time on the failure path —
    runs a dummy compare even when no prefix match exists, so attackers
    can't enumerate live prefixes via response-time differences.
    """
    if not plaintext or not plaintext.startswith(KEY_PLAINTEXT_PREFIX):
        hmac.compare_digest(_DUMMY_HASH, _DUMMY_HASH)
        return None

    prefix         = prefix_of(plaintext)
    candidate_hash = hash_key(plaintext)

    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT api_key_id, company_id, ak_hash
                   FROM api_keys
                  WHERE ak_prefix = ?
                    AND status_id = 1
                    AND ak_revoked_at IS NULL"""),
            (prefix,),
        )
        rows = [dict(r) for r in cur.fetchall()]

        matched_row = None
        for row in rows:
            # Don't short-circuit — keep loop work stable across rows.
            if hmac.compare_digest(candidate_hash, row["ak_hash"]):
                matched_row = row
        if matched_row is None:
            hmac.compare_digest(_DUMMY_HASH, _DUMMY_HASH)
            return None

        # Bump last-used. Best-effort — failure here doesn't fail auth.
        try:
            now_expr = "NOW()" if IS_POSTGRES else "CURRENT_TIMESTAMP"
            conn.execute(
                q(f"UPDATE api_keys SET ak_last_used_at = {now_expr} "
                  "WHERE api_key_id = ?"),
                (matched_row["api_key_id"],),
            )
            conn.commit()
        except Exception:
            logger.exception(
                "Failed to bump ak_last_used_at (api_key_id=%s)",
                matched_row["api_key_id"],
            )

        return matched_row["company_id"]
    finally:
        conn.close()


# ── Decorator ─────────────────────────────────────────────────


def _extract_key():
    """Read plaintext key from Authorization: Bearer or X-API-Key header."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return (request.headers.get("X-API-Key") or "").strip() or None


def require_api_key(fn):
    """Auth decorator for external API endpoints.

    On success: sets g.api_key_company_id and calls the wrapped view.
    On failure: returns 401 JSON {"error": "Unauthorized"} and logs a
    warning (mirrors the voip_routes.py signature-failure pattern).
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        plaintext  = _extract_key()
        company_id = verify_key(plaintext) if plaintext else None
        if company_id is None:
            # Pay the dummy-compare cost on the no-key path too.
            if not plaintext:
                hmac.compare_digest(_DUMMY_HASH, _DUMMY_HASH)
            logger.warning(
                "API key auth failed (path=%s remote=%s)",
                request.path, request.remote_addr,
            )
            return jsonify({"error": "Unauthorized"}), 401
        g.api_key_company_id = company_id
        return fn(*args, **kwargs)
    return wrapper
