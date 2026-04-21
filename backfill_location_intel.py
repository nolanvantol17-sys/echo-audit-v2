"""One-off backfill of location_intel rows after the pipeline fix.

Scans every (interaction_location_id, company_id) pair that has at least
one non-deleted interaction and runs compute_location_intel synchronously
for each. Existing rows are upserted in place; missing rows are created.

Usage:
    python3 backfill_location_intel.py --dry-run   # print plan only
    python3 backfill_location_intel.py             # execute (all pairs)

Targeted re-run for rate-limited briefs:
    python3 backfill_location_intel.py \
        --null-only --bypass-rate-limit --sleep-seconds 2

Safe to re-run; compute_location_intel is idempotent. Sequential by design
so we don't fan out 20+ Claude calls in parallel against the rate limiter.
"""
import argparse
import logging
import sys
import time

from db import IS_POSTGRES, get_conn, q
from intel import compute_location_intel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_location_intel")


def _planned_pairs(null_only=False):
    conn = get_conn()
    try:
        if null_only:
            sql = """
                SELECT i.interaction_location_id AS location_id,
                       p.company_id              AS company_id,
                       COUNT(*)                  AS call_count
                  FROM interactions i
                  JOIN projects p ON p.project_id = i.project_id
                  LEFT JOIN location_intel li
                    ON li.location_id = i.interaction_location_id
                   AND li.company_id = p.company_id
                 WHERE i.interaction_deleted_at IS NULL
                   AND i.interaction_location_id IS NOT NULL
                   AND (li.li_summary IS NULL OR li.li_summary = '')
                 GROUP BY i.interaction_location_id, p.company_id
                 ORDER BY p.company_id, i.interaction_location_id
            """
        else:
            sql = """
                SELECT i.interaction_location_id AS location_id,
                       p.company_id              AS company_id,
                       COUNT(*)                  AS call_count
                  FROM interactions i
                  JOIN projects p ON p.project_id = i.project_id
                 WHERE i.interaction_deleted_at IS NULL
                   AND i.interaction_location_id IS NOT NULL
                 GROUP BY i.interaction_location_id, p.company_id
                 ORDER BY p.company_id, i.interaction_location_id
            """
        cur = conn.execute(q(sql))
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


def _fetch_intel_row(location_id, company_id):
    """Read back the location_intel row to verify post-compute output."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT li_summary, li_strengths, li_weaknesses, li_last_computed_at
                   FROM location_intel
                  WHERE location_id = ? AND company_id = ?"""),
            (location_id, company_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _scored_call_count(location_id, company_id):
    """Count graded calls for (location, company) — mirrors compute_location_intel
    eligibility: not deleted, not no-answer (status 44), with a non-NULL score."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT COUNT(*) AS n
                   FROM interactions i
                   JOIN projects p ON p.project_id = i.project_id
                  WHERE i.interaction_location_id = ?
                    AND p.company_id = ?
                    AND i.interaction_deleted_at IS NULL
                    AND i.status_id != 44
                    AND i.interaction_overall_score IS NOT NULL"""),
            (location_id, company_id),
        )
        return dict(cur.fetchone())["n"]
    finally:
        conn.close()


def _reset_hourly_counter(company_ids):
    """Zero the current-hour anthropic counter for each affected company.

    Called after a --bypass-rate-limit run so backfill bursts don't eat
    into the tenant's user-facing hourly budget. Daily counter is left
    intact (real spend should be reflected in the daily total).
    """
    from helpers import _window_start
    ws = _window_start("hour")
    conn = get_conn()
    try:
        for cid in company_ids:
            if IS_POSTGRES:
                conn.execute(
                    """UPDATE api_usage SET au_request_count = 0
                        WHERE company_id = %s AND au_service = 'anthropic'
                          AND au_period_type = 'hour' AND au_period_start = %s""",
                    (cid, ws),
                )
            else:
                conn.execute(
                    """UPDATE api_usage SET au_request_count = 0
                        WHERE company_id = ? AND au_service = 'anthropic'
                          AND au_period_type = 'hour' AND au_period_start = ?""",
                    (cid, ws),
                )
            logger.warning(
                "RESET hourly anthropic counter to 0 for company=%s window=%s",
                cid, ws.isoformat(),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print planned pairs and exit without computing.")
    ap.add_argument("--null-only", action="store_true",
                    help="Only process pairs where location_intel.li_summary is NULL/empty.")
    ap.add_argument("--bypass-rate-limit", action="store_true",
                    help="Monkey-patch check_rate_limit to always pass. "
                         "Use for one-off backfills on a known set of pairs. "
                         "Resets the hourly counter to 0 after the run completes.")
    ap.add_argument("--sleep-seconds", type=float, default=0.0,
                    help="Sleep N seconds between compute calls to throttle "
                         "the upstream Anthropic API (default 0).")
    args = ap.parse_args()

    if args.bypass_rate_limit:
        import helpers
        import intel as _intel
        helpers.check_rate_limit = lambda *a, **kw: (True, "")
        _intel.check_rate_limit = lambda *a, **kw: (True, "")
        logger.warning("RATE LIMITER BYPASSED for this run.")

    pairs = _planned_pairs(null_only=args.null_only)
    logger.info("Planned %d (location, company) pair(s)%s",
                len(pairs), " [null-only]" if args.null_only else "")
    for p in pairs:
        name = _location_name(p["location_id"]) or "(unknown)"
        logger.info("  company=%s location=%s [%s] — %d call(s)",
                    p["company_id"], p["location_id"], name, p["call_count"])

    if args.dry_run:
        logger.info("Dry run — no changes made.")
        return 0

    generated  = 0
    skipped    = 0
    missing    = 0
    exceptions = 0
    for idx, p in enumerate(pairs):
        try:
            compute_location_intel(p["location_id"], p["company_id"])
        except Exception:
            exceptions += 1
            logger.exception("EXCEPTION company=%s location=%s",
                             p["company_id"], p["location_id"])
        else:
            row = _fetch_intel_row(p["location_id"], p["company_id"])
            summary = (row or {}).get("li_summary") if row else None
            if row and summary and summary.strip():
                generated += 1
                logger.info("OK   company=%s location=%s", p["company_id"], p["location_id"])
            else:
                scored = _scored_call_count(p["location_id"], p["company_id"])
                if scored == 0:
                    skipped += 1
                    logger.info(
                        "SKIP company=%s location=%s — no graded calls, brief correctly NULL",
                        p["company_id"], p["location_id"],
                    )
                else:
                    missing += 1
                    logger.error(
                        "MISSING company=%s location=%s — %d scored call(s) but li_summary "
                        "still NULL/empty after compute (check ANTHROPIC_API_KEY / Claude auth / rate limiter)",
                        p["company_id"], p["location_id"], scored,
                    )
        if args.sleep_seconds > 0 and idx < len(pairs) - 1:
            time.sleep(args.sleep_seconds)

    logger.info(
        "Done. Briefs generated: %d. Skipped (no graded calls): %d. "
        "Missing (should have briefed): %d. Exceptions: %d. (Total pairs: %d)",
        generated, skipped, missing, exceptions, len(pairs),
    )
    failures = missing + exceptions

    if args.bypass_rate_limit and pairs:
        affected = sorted({p["company_id"] for p in pairs})
        logger.warning("Resetting hourly counter for %d affected compan(ies): %s",
                       len(affected), affected)
        _reset_hourly_counter(affected)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
