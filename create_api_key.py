"""create_api_key.py — Mint a new external API key for a company.

Run via Railway SSH:
    railway ssh "cd /app && python3 create_api_key.py --company-id 25 --name 'External Caller'"

The plaintext key is printed ONCE to stdout. Store it immediately — it is
hashed at rest and cannot be recovered. Never log the plaintext to a file.

Idempotency: refuses to create a duplicate active key with the same
(company_id, ak_name). Revoke the existing one first if you need to re-issue.
"""

import argparse
import logging
import sys

from api_key_auth import generate_key, hash_key, prefix_of
from db import IS_POSTGRES, get_conn, q

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("create_api_key")


def _company_name(conn, company_id):
    cur = conn.execute(
        q("SELECT company_name FROM companies WHERE company_id = ?"),
        (company_id,),
    )
    row = cur.fetchone()
    return dict(row)["company_name"] if row else None


def _name_already_used(conn, company_id, name):
    cur = conn.execute(
        q("""SELECT api_key_id FROM api_keys
              WHERE company_id = ? AND ak_name = ?
                AND status_id = 1 AND ak_revoked_at IS NULL"""),
        (company_id, name),
    )
    return cur.fetchone() is not None


def main():
    parser = argparse.ArgumentParser(
        description="Create a new external API key for a company."
    )
    parser.add_argument("--company-id", type=int, required=True,
                        help="Company that owns the key.")
    parser.add_argument("--name", type=str, required=True,
                        help="Operator-facing label (e.g. 'External Caller').")
    args = parser.parse_args()

    name = args.name.strip()
    if not name:
        logger.error("--name must be non-empty.")
        sys.exit(2)

    conn = get_conn()
    try:
        company_name = _company_name(conn, args.company_id)
        if not company_name:
            logger.error("No company with company_id=%s", args.company_id)
            sys.exit(2)

        if _name_already_used(conn, args.company_id, name):
            logger.error(
                "An active API key named %r already exists for company_id=%s. "
                "Revoke it first if you need to re-issue.",
                name, args.company_id,
            )
            sys.exit(2)

        plaintext = generate_key()
        prefix    = prefix_of(plaintext)
        ak_hash   = hash_key(plaintext)

        if IS_POSTGRES:
            conn.execute(
                """INSERT INTO api_keys
                       (company_id, ak_prefix, ak_hash, ak_name, status_id)
                   VALUES (%s, %s, %s, %s, 1)""",
                (args.company_id, prefix, ak_hash, name),
            )
        else:
            conn.execute(
                """INSERT INTO api_keys
                       (company_id, ak_prefix, ak_hash, ak_name, status_id)
                   VALUES (?, ?, ?, ?, 1)""",
                (args.company_id, prefix, ak_hash, name),
            )
        conn.commit()
    finally:
        conn.close()

    bar = "=" * 70
    print()
    print(bar)
    print(f"  API KEY CREATED for company {args.company_id} ({company_name!r})")
    print(f"  Name:   {name}")
    print(f"  Prefix: {prefix}")
    print()
    print("  PLAINTEXT KEY — STORE THIS NOW. IT WILL NOT BE SHOWN AGAIN:")
    print()
    print(f"    {plaintext}")
    print()
    print(bar)


if __name__ == "__main__":
    main()
