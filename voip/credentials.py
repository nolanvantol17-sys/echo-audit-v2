"""
voip/credentials.py — Fernet symmetric encryption for VoIP provider secrets.

All credentials are encrypted at rest in voip_configs.voip_config_credentials
as a single JSON-encoded+encrypted string inside a JSONB wrapper. The wrapper
shape is:

    { "enc": "<base64 fernet ciphertext>" }

The encryption key is sourced in this order:
    1. VOIP_ENCRYPTION_KEY env var  (must be a 32-byte url-safe base64 Fernet key)
    2. Derived from SECRET_KEY env var via PBKDF2-SHA256

NEVER log, return, or include decrypted credentials in API responses. Use
decrypt_credentials() only in server-internal paths (webhook signature
verification, recording downloads).
"""

import base64
import hashlib
import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Fixed salt is intentional. A per-row salt would force storing it next to
# the ciphertext and key rotation becomes impossible without decrypt-then-
# re-encrypt migrations. This mirrors how most SaaS secret stores work:
# one key, rotated via re-encryption jobs — not per-row salts.
_DERIVATION_SALT = b"echo-audit-voip-creds-v1"


def _get_fernet():
    """Build a Fernet instance from env. Raises RuntimeError on misconfiguration."""
    key = os.environ.get("VOIP_ENCRYPTION_KEY")
    if key:
        try:
            # Validate that the env key is a proper Fernet key (raises on bad len/charset)
            return Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as e:
            raise RuntimeError(
                "VOIP_ENCRYPTION_KEY is set but not a valid Fernet key. "
                "Generate one with: python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            ) from e

    secret = os.environ.get("SECRET_KEY")
    if not secret:
        raise RuntimeError(
            "Cannot build a VoIP encryption key: set VOIP_ENCRYPTION_KEY or SECRET_KEY."
        )

    # PBKDF2 derivation when only SECRET_KEY is available.
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_DERIVATION_SALT,
        iterations=260_000,
    )
    derived = kdf.derive(secret.encode("utf-8"))
    fernet_key = base64.urlsafe_b64encode(derived)
    return Fernet(fernet_key)


def encrypt_credentials(data: dict) -> str:
    """Serialize and encrypt a credentials dict. Returns a base64 string.

    The string is the raw Fernet token. Store it inside a JSONB wrapper like
    {"enc": <returned_string>} so the column type stays JSONB.
    """
    if not isinstance(data, dict):
        raise TypeError("credentials must be a dict")
    plaintext = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    f = _get_fernet()
    token = f.encrypt(plaintext)
    return token.decode("ascii")


def decrypt_credentials(encrypted) -> dict:
    """Decrypt and deserialize to a dict. Accepts either the raw token string
    or the {"enc": "..."} JSONB wrapper (as dict or JSON-encoded string).

    Raises ValueError on tampering, key mismatch, or malformed payload.
    """
    if encrypted is None or encrypted == "":
        return {}

    raw = encrypted
    if isinstance(raw, str):
        # Could be either a raw Fernet token or a JSON-encoded wrapper.
        stripped = raw.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                raise ValueError("credentials payload is not valid JSON")
            if isinstance(parsed, dict) and "enc" in parsed:
                raw = parsed["enc"]
    elif isinstance(raw, dict):
        raw = raw.get("enc") or ""

    if not raw:
        return {}

    f = _get_fernet()
    try:
        plaintext = f.decrypt(raw.encode("ascii") if isinstance(raw, str) else raw)
    except InvalidToken as e:
        raise ValueError("credentials ciphertext is invalid or key mismatch") from e

    try:
        return json.loads(plaintext.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError("decrypted payload is not valid JSON") from e


def credentials_fingerprint(data: dict) -> str:
    """Deterministic SHA-256 fingerprint of a credentials dict.

    Useful for audit logs ("credentials rotated from fp=abc to fp=def") without
    ever revealing the actual values. Returns the first 16 hex chars.
    """
    blob = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
