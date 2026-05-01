"""
backfill_rescore_all.py — Re-grade all graded interactions under the new
0.0–9.9 scoring ceiling.

Run via Railway:
    railway run python backfill_rescore_all.py --company-id 25 --actor-user-id 48
    railway run python backfill_rescore_all.py --company-id 25 --actor-user-id 48 --limit 1
    railway run python backfill_rescore_all.py --company-id 25 --actor-user-id 48 --dry-run

Reuses _grade_and_persist with is_initial_grade=False so each call:
  • bumps interaction_regrade_count
  • preserves interaction_original_score (existing wins; otherwise the current
    overall_score becomes the original_score)
  • writes an ACTION_REGRADED audit log
  • triggers async performance_report + location_intel refresh

Sequential execution to stay polite to the Anthropic rate limit. Expect
~30–45s per call → 45–60 min for ~80 calls.

Idempotent in the loose sense: re-running will re-grade everything again,
but interaction_original_score is preserved across runs (existing wins),
so the "true original" stays anchored to the pre-9.9-ceiling number on
its first re-grade and is not overwritten by subsequent backfills.
"""

import argparse
import logging
import sys
import time

from app import app
from db import get_conn, q
from interactions_routes import (
    STATUS_GRADED,
    _grade_and_persist,
    _items_to_criteria,
    _load_rubric_group,
    _load_rubric_items,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_rescore_all")


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def fetch_target_interactions(company_id, limit=None):
    """All graded, non-deleted, transcript-bearing interactions for this tenant."""
    sql = """
        SELECT i.interaction_id,
               i.project_id,
               i.interaction_overall_score,
               i.interaction_original_score,
               i.interaction_location_id,
               i.respondent_user_id,
               i.interaction_transcript,
               p.rubric_group_id
          FROM interactions i
          JOIN projects p ON p.project_id = i.project_id
         WHERE p.company_id = ?
           AND i.status_id = ?
           AND i.interaction_deleted_at IS NULL
           AND i.interaction_transcript IS NOT NULL
         ORDER BY i.interaction_id ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = get_conn()
    try:
        cur = conn.execute(q(sql), (company_id, STATUS_GRADED))
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company-id",    type=int, required=True)
    p.add_argument("--actor-user-id", type=int, required=True,
                   help="user_id to attribute the audit log entries to.")
    p.add_argument("--limit",   type=int, default=None,
                   help="Process at most N interactions (test runs).")
    p.add_argument("--dry-run", action="store_true",
                   help="List which interactions would be re-graded; no API calls.")
    p.add_argument("--sleep",   type=float, default=1.0,
                   help="Seconds to sleep between calls (default 1.0).")
    args = p.parse_args()

    with app.app_context():
        rows = fetch_target_interactions(args.company_id, limit=args.limit)
        logger.info("Found %d graded interactions to re-score (company_id=%d)",
                    len(rows), args.company_id)

        if args.dry_run:
            for r in rows:
                logger.info("[dry-run] interaction_id=%d  score=%s  project_id=%d  rubric_group_id=%s",
                            r["interaction_id"], r["interaction_overall_score"],
                            r["project_id"], r["rubric_group_id"])
            return 0

        ok_count, fail_count = 0, 0
        results = []

        for i, r in enumerate(rows, 1):
            iid             = r["interaction_id"]
            project_id      = r["project_id"]
            old_score       = r["interaction_overall_score"]
            rubric_group_id = r["rubric_group_id"]

            conn = get_conn()
            try:
                rg = _load_rubric_group(conn, rubric_group_id)
                grade_target = (rg or {}).get("rg_grade_target") or "respondent"
                items = _load_rubric_items(conn, rubric_group_id)
            finally:
                conn.close()

            if not items:
                logger.warning("[%d/%d] interaction_id=%d project_id=%d has no rubric items — skipping",
                               i, len(rows), iid, project_id)
                fail_count += 1
                continue

            criteria = _items_to_criteria(items)

            try:
                logger.info("[%d/%d] re-grading interaction_id=%d (old=%s) …",
                            i, len(rows), iid, old_score)
                resp = _grade_and_persist(
                    interaction_id=iid,
                    company_id=args.company_id,
                    project_id=project_id,
                    respondent_user_id=r.get("respondent_user_id"),
                    transcript=r["interaction_transcript"],
                    criteria=criteria,
                    script_text=None,
                    context_text=None,
                    grade_target=grade_target,
                    is_initial_grade=False,
                    location_id=r.get("interaction_location_id"),
                    actor_user_id=args.actor_user_id,
                )
                new_score = resp.get("total_score")
                logger.info("[%d/%d] interaction_id=%d  %s → %s",
                            i, len(rows), iid, old_score, new_score)
                results.append({"iid": iid, "old": old_score, "new": new_score})
                ok_count += 1
            except Exception as exc:
                logger.exception("[%d/%d] interaction_id=%d FAILED: %s",
                                 i, len(rows), iid, exc)
                fail_count += 1

            time.sleep(args.sleep)

        logger.info("DONE — ok=%d fail=%d total=%d", ok_count, fail_count, len(rows))
        scores = sorted([r["new"] for r in results if r["new"] is not None])
        if scores:
            mid = scores[len(scores) // 2]
            logger.info("New score distribution: min=%.1f median=%.1f max=%.1f n=%d",
                        scores[0], mid, scores[-1], len(scores))
        return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
