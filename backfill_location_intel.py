"""One-off backfill of location_intel rows after the pipeline fix.

Scans every (interaction_location_id, company_id) pair that has at least
one non-deleted interaction and runs compute_location_intel synchronously
for each. Existing rows are upserted in place; missing rows are created.

Usage:
    python3 backfill_location_intel.py --dry-run   # print plan only
    python3 backfill_location_intel.py             # execute

Safe to re-run; compute_location_intel is idempotent. Sequential by design
so we don't fan out 20+ Claude calls in parallel against the rate limiter.
"""
import argparse
import logging
import sys

from db import get_conn, q
from intel import compute_location_intel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_location_intel")


def _planned_pairs():
    conn = get_conn()
    try:
        cur = conn.execute(q("""
            SELECT i.interaction_location_id AS location_id,
                   p.company_id              AS company_id,
                   COUNT(*)                  AS call_count
              FROM interactions i
              JOIN projects p ON p.project_id = i.project_id
             WHERE i.interaction_deleted_at IS NULL
               AND i.interaction_location_id IS NOT NULL
             GROUP BY i.interaction_location_id, p.company_id
             ORDER BY p.company_id, i.interaction_location_id
        """))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _location_name(location_id):
    conn = get_conn()
    try:
        cur = conn.execute(
            q("SELECT location_name FROM locations WHERE location_id = ?"),
            (location_id,),
        )
        row = cur.fetchone()
        return dict(row)["location_name"] if row else None
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print planned pairs and exit without computing.")
    args = ap.parse_args()

    pairs = _planned_pairs()
    logger.info("Planned %d (location, company) pair(s)", len(pairs))
    for p in pairs:
        name = _location_name(p["location_id"]) or "(unknown)"
        logger.info("  company=%s location=%s [%s] — %d call(s)",
                    p["company_id"], p["location_id"], name, p["call_count"])

    if args.dry_run:
        logger.info("Dry run — no changes made.")
        return 0

    failures = 0
    for p in pairs:
        try:
            compute_location_intel(p["location_id"], p["company_id"])
            logger.info("OK   company=%s location=%s", p["company_id"], p["location_id"])
        except Exception:
            failures += 1
            logger.exception("FAIL company=%s location=%s",
                             p["company_id"], p["location_id"])

    logger.info("Done. %d/%d succeeded, %d failed",
                len(pairs) - failures, len(pairs), failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
