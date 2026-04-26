"""One-shot backlog cleanup — purge orphan derived references left by the
2026-04-25 9-row interaction purge.

Background
----------
On 2026-04-25, migrate_purge_soft_deleted_interactions.py purged 9
soft-deleted interactions (iids 68, 73, 74, 75, 76, 77, 80, 81, 82) via
hard-delete. At the time, the hard-delete handler did NOT cascade to
derived data:
  - performance_reports.pr_processed_interaction_ids (JSONB array of
    contributing interaction_ids)
  - location_intel.li_* (recomputed-from-scratch stats + Claude brief)

Commit 855e5cd (2026-04-26) extended the cascade so future hard-deletes
self-clean. This script cleans up the backlog left by the 2026-04-25 run.

What this does
--------------
Part 1: DELETE performance_report row 28 (Brielle / Bella Terra).
  Pre-check: pr_processed_interaction_ids must equal [68] AND
  interaction_id=68 must no longer exist in interactions. If state has
  drifted (e.g. someone re-graded Brielle since the purge, adding new
  iids), HALT with diagnostics — do not auto-correct.

Part 2: fire compute_location_intel_async(82, 25) for Bella Terra.
  Idempotent (intel.py recomputes from fresh state); rate-limit gated
  inside intel.py for the optional Claude brief.

Modes
-----
  --dry-run (default)  : show pre-state + planned actions; change nothing
  --apply              : execute Part 1 (DELETE) + Part 2 (async fire);
                         verify post-state

Run via
-------
  railway run python3 migrate_purge_orphan_derived_refs.py --dry-run
  railway run python3 migrate_purge_orphan_derived_refs.py --apply
"""

import argparse
import json
import logging
import sys
import time

from db import IS_POSTGRES, get_conn, q
from intel import compute_location_intel_async


logger = logging.getLogger("migrate_purge_orphan_derived_refs")


# ── Constants — pinned to the specific backlog row ──────────────

PR_ID         = 28
EXPECTED_IID  = 68
LOC_ID        = 82
COMPANY_ID    = 25


# ── Helpers ─────────────────────────────────────────────────────


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _load_pr(conn, pr_id):
    cur = conn.execute(
        q("""SELECT pr.performance_report_id, pr.respondent_id, pr.subject_user_id,
                    r.respondent_name, r.location_id, l.location_name,
                    pr.pr_call_count, pr.pr_average_score,
                    pr.pr_processed_interaction_ids, pr.pr_updated_at
               FROM performance_reports pr
               LEFT JOIN respondents r ON r.respondent_id = pr.respondent_id
               LEFT JOIN locations   l ON l.location_id  = r.location_id
              WHERE pr.performance_report_id = ?"""),
        (pr_id,),
    )
    return _row_to_dict(cur.fetchone())


def _interaction_exists(conn, iid):
    cur = conn.execute(
        q("SELECT 1 FROM interactions WHERE interaction_id = ?"),
        (iid,),
    )
    return cur.fetchone() is not None


def _ids_list(raw):
    if isinstance(raw, str):
        try: return json.loads(raw)
        except Exception: return []
    if isinstance(raw, list):
        return list(raw)
    return []


# ── Main ────────────────────────────────────────────────────────


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True,
                     help="(default) show plan; change nothing")
    grp.add_argument("--apply", action="store_true",
                     help="execute the cleanup")
    args = ap.parse_args()

    apply_mode = bool(args.apply)
    mode_label = "APPLY" if apply_mode else "DRY-RUN"

    print(f"=== migrate_purge_orphan_derived_refs [{mode_label}] ===\n")

    conn = get_conn()
    try:
        # ── Part 1: pre-check + report ──
        print(f"Part 1: performance_report cleanup")
        pr = _load_pr(conn, PR_ID)
        if pr is None:
            print(f"  pr_id={PR_ID} not found — already cleaned up or never existed.")
            print(f"  Skipping Part 1.\n")
            part1_ok = False
            part1_action = "skip-already-gone"
        else:
            ids = _ids_list(pr.get("pr_processed_interaction_ids"))
            iid_present = _interaction_exists(conn, EXPECTED_IID)
            print(f"  pr_id={PR_ID}  respondent={pr['respondent_name']}  location={pr['location_name']}")
            print(f"    pr_call_count = {pr['pr_call_count']}")
            print(f"    pr_avg_score  = {pr['pr_average_score']}")
            print(f"    pr_processed_interaction_ids = {ids}")
            print(f"    interaction_id={EXPECTED_IID} exists in interactions: {iid_present}")
            print(f"    pr_updated_at = {pr['pr_updated_at']}")

            # State validation
            ids_int = [int(x) for x in ids]
            if ids_int == [EXPECTED_IID] and not iid_present:
                print(f"  ✓ State matches expected: ids=[{EXPECTED_IID}] and that iid is purged.")
                print(f"  Plan: DELETE FROM performance_reports WHERE performance_report_id = {PR_ID}")
                part1_ok = True
                part1_action = "delete"
            else:
                print(f"  ✕ State has drifted from expected (ids=[{EXPECTED_IID}] AND iid {EXPECTED_IID} purged).")
                print(f"    Refusing to auto-correct. Investigate manually.")
                part1_ok = False
                part1_action = "halt-drifted"
        print()

        # ── Part 2: location_intel refresh plan ──
        print(f"Part 2: location_intel refresh")
        cur = conn.execute(
            q("""SELECT li.location_intel_id, l.location_name,
                        li.li_total_calls, li.li_avg_score, li.li_no_answer_count,
                        li.li_last_call_score, li.li_last_computed_at
                   FROM location_intel li
                   JOIN locations l ON l.location_id = li.location_id
                  WHERE li.location_id = ? AND li.company_id = ?"""),
            (LOC_ID, COMPANY_ID),
        )
        intel_row = _row_to_dict(cur.fetchone())
        if intel_row is None:
            print(f"  No location_intel row for loc_id={LOC_ID}, company_id={COMPANY_ID} — first refresh will create one.")
        else:
            print(f"  loc_id={LOC_ID}  loc={intel_row['location_name']}")
            print(f"    li_total_calls={intel_row['li_total_calls']}  li_avg_score={intel_row['li_avg_score']}")
            print(f"    li_no_answer_count={intel_row['li_no_answer_count']}  li_last_call_score={intel_row['li_last_call_score']}")
            print(f"    li_last_computed_at={intel_row['li_last_computed_at']}")
        print(f"  Plan: fire compute_location_intel_async({LOC_ID}, {COMPANY_ID})")
        print(f"        (idempotent; intel.py recomputes from fresh state; AI brief gated by anthropic rate limit)")
        print()

        if not apply_mode:
            print("DRY-RUN complete — no changes made.")
            print("Re-run with --apply to execute.\n")
            return 0

        # ── APPLY ──
        if part1_ok and part1_action == "delete":
            print("Part 1 — applying DELETE …")
            try:
                if IS_POSTGRES:
                    conn.execute(
                        "DELETE FROM performance_reports WHERE performance_report_id = %s",
                        (PR_ID,),
                    )
                else:
                    conn.execute(
                        "DELETE FROM performance_reports WHERE performance_report_id = ?",
                        (PR_ID,),
                    )
                conn.commit()
                print(f"  ✓ Deleted performance_report_id={PR_ID}")
            except Exception as e:
                conn.rollback()
                print(f"  ✕ DELETE failed: {e}")
                return 1
        elif part1_action == "halt-drifted":
            print("Part 1 — HALTED (drifted state). Not deleting pr_id={PR_ID}.")
            print("  Aborting — nothing in Part 2 should run if Part 1 failed validation.")
            return 1
        elif part1_action == "skip-already-gone":
            print("Part 1 — skipped (already gone).")

        # ── Part 2: fire async ──
        print("\nPart 2 — firing async location_intel refresh …")
        compute_location_intel_async(LOC_ID, COMPANY_ID)
        print(f"  ✓ Fired compute_location_intel_async({LOC_ID}, {COMPANY_ID})")
        # Give the daemon a moment to pick up before we verify (best-effort).
        # The thread runs in the background; verification just confirms the
        # deletion above stuck. Intel refresh is async + may not complete by
        # the time this script exits — that's fine.
        time.sleep(1)

        # ── Post-apply verification ──
        print("\nPost-apply verification:")
        pr_after = _load_pr(conn, PR_ID)
        if pr_after is None:
            print(f"  ✓ performance_report_id={PR_ID} no longer exists")
        else:
            print(f"  ✕ performance_report_id={PR_ID} STILL EXISTS — DELETE did not stick")
            return 1

        # Confirm no other PR rows still reference any of the 9 purged iids.
        purged_iids = {73, 74, 75, 76, 77, 80, 81, 82, 68}
        cur = conn.execute(q("SELECT performance_report_id, pr_processed_interaction_ids FROM performance_reports"))
        stragglers = []
        for r in cur.fetchall():
            d = _row_to_dict(r)
            ids = _ids_list(d.get("pr_processed_interaction_ids"))
            stale = [int(i) for i in ids if int(i) in purged_iids]
            if stale:
                stragglers.append((d["performance_report_id"], stale))
        if stragglers:
            print(f"  ✕ Other PR rows still reference purged iids: {stragglers}")
            return 1
        print(f"  ✓ Zero PR rows reference any of the 9 purged iids")

        print("\nMigration complete.")
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
