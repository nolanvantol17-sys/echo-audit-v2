"""bootstrap_elevenlabs_mayfair.py — One-shot voip_configs row for Mayfair.

Inserts (or updates) a voip_configs row for company_id=25 with
provider='elevenlabs', a placeholder webhook secret, and auto_grade=FALSE.

The placeholder secret intentionally fails HMAC verification — every webhook
attempt will 401 until the real secret is set via the UI (POST /api/voip/config)
after generating it in ElevenLabs' dashboard. This is the safe default for C1:
the discovery test call CANNOT happen until someone explicitly updates the
secret, so we won't accidentally accept signed payloads from a misconfigured
sender.

Run via Railway SSH:
    railway ssh "cd /app && python3 bootstrap_elevenlabs_mayfair.py"

Idempotent: re-running on an existing row updates the placeholder back to the
default. To replace the secret post-bootstrap, use the UI or POST /api/voip/config.
"""

import json
import logging
import sys

from db import IS_POSTGRES, get_conn, q
from voip.credentials import encrypt_credentials

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("bootstrap_elevenlabs_mayfair")

COMPANY_ID         = 25
PROVIDER_KEY       = "elevenlabs"
PLACEHOLDER_SECRET = "PLACEHOLDER_REPLACE_BEFORE_DISCOVERY_CALL"


def main():
    creds = {"webhook_secret": PLACEHOLDER_SECRET}
    try:
        enc = encrypt_credentials(creds)
    except Exception:
        logger.exception("encrypt_credentials failed — check VOIP_ENCRYPTION_KEY/SECRET_KEY env")
        sys.exit(2)
    credentials_json = json.dumps({"enc": enc})

    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT voip_config_id, voip_config_provider FROM voip_configs WHERE company_id = ?"),
            (COMPANY_ID,),
        )
        existing = cur.fetchone()

        if existing:
            existing = dict(existing)
            logger.info(
                "Existing voip_config found for company_id=%s (provider=%s) — updating to elevenlabs",
                COMPANY_ID, existing["voip_config_provider"],
            )
            if IS_POSTGRES:
                conn.execute(
                    """UPDATE voip_configs SET
                           voip_config_provider       = %s,
                           voip_config_credentials    = %s::jsonb,
                           voip_config_auto_grade     = %s,
                           voip_config_is_active      = TRUE
                       WHERE company_id = %s""",
                    (PROVIDER_KEY, credentials_json, False, COMPANY_ID),
                )
            else:
                conn.execute(
                    """UPDATE voip_configs SET
                           voip_config_provider       = ?,
                           voip_config_credentials    = ?,
                           voip_config_auto_grade     = ?,
                           voip_config_is_active      = 1
                       WHERE company_id = ?""",
                    (PROVIDER_KEY, credentials_json, 0, COMPANY_ID),
                )
            action = "updated"
        else:
            if IS_POSTGRES:
                conn.execute(
                    """INSERT INTO voip_configs (
                           company_id, voip_config_provider, voip_config_credentials,
                           voip_config_auto_grade, voip_config_is_active
                       ) VALUES (%s, %s, %s::jsonb, %s, TRUE)""",
                    (COMPANY_ID, PROVIDER_KEY, credentials_json, False),
                )
            else:
                conn.execute(
                    """INSERT INTO voip_configs (
                           company_id, voip_config_provider, voip_config_credentials,
                           voip_config_auto_grade, voip_config_is_active
                       ) VALUES (?, ?, ?, ?, 1)""",
                    (COMPANY_ID, PROVIDER_KEY, credentials_json, 0),
                )
            action = "inserted"

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    bar = "=" * 70
    print()
    print(bar)
    print(f"  voip_config {action} for company_id={COMPANY_ID}")
    print(f"    provider:        {PROVIDER_KEY}")
    print(f"    auto_grade:      FALSE  (C1 — discovery only, no processor work)")
    print(f"    webhook_secret:  PLACEHOLDER (every webhook will 401 until updated)")
    print()
    print("  Next steps:")
    print("    1. Generate the real webhook secret in ElevenLabs dashboard")
    print(f"    2. POST /api/voip/config (admin UI) to replace the placeholder")
    print(f"    3. Send the discovery test call to /api/voip/webhook/{COMPANY_ID}")
    print("    4. Inspect voip_call_queue.voip_queue_raw_payload to confirm shapes")
    print(bar)


if __name__ == "__main__":
    main()
