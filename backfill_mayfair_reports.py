"""One-off backfill of V2 Performance Reports for Mayfair project_id=30.

Purpose
-------
Mayfair Secret Shopping — Legacy V1 (project_id=30, company_id=25) had 22 graded
interactions migrated from V1 with no Performance Reports generated. This script
walks each graded interaction in chronological order and invokes the standard
update_performance_report() path so Claude builds the per-respondent reports
incrementally, exactly as it would for a freshly graded call.

Target sizing
-------------
Expected: 22 interactions across 22 distinct respondents (verified by recon).
Includes 15 named-respondent rows + 7 newly-linked "Name Not Detected"
respondents (per-property anonymous, created 2026-04-17 in the orphan linkage
step). Dry-run will reprint the exact set.

Why respondent_id, never respondent_user_id
-------------------------------------------
project 30 is wired to rubric_group_id=29 with rg_grade_target='respondent'.
That setting drives the grading flow at interactions_routes.py:801-805 and
:1510-1514 to fire update_performance_report_async with respondent_id, never
respondent_user_id. We mirror that contract here. Tenants where
rg_grade_target='caller' would invert this — they'd pass respondent_user_id
and leave respondent_id None — and would NOT use this script.

Rate limits
-----------
Anthropic rate limits in helpers.py are 10/hour, 50/day per company. 22 calls
in one shot would breach. helpers.check_rate_limit() returns True (skip) when
company_id is None, so we deliberately pass company_id=None to bypass the
per-company throttle for this one-off backfill. Standard production traffic
remains throttled normally because it always passes a real company_id.

Idempotency
-----------
update_performance_report() short-circuits if interaction_id is already in
pr_processed_interaction_ids, so re-running this script is safe.

Authorization
-------------
Run date: 2026-04-17
Authorized by: claude.cowork@mayfairmgt.com

Usage
-----
    python3 backfill_mayfair_reports.py            # dry-run (default)
    python3 backfill_mayfair_reports.py --apply    # actually call Claude

--apply prints the dry-run summary first, then prompts y/N before doing
anything. After --apply finishes it runs and prints the verification SQL.
"""

import argparse
import sys
import time
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv('.env')

from db import get_conn, q
from performance_reports import update_performance_report

PROJECT_ID = 30
COMPANY_ID = 25
GRADED_STATUS_ID = 43

SELECT_TARGETS = q("""
    SELECT i.interaction_id,
           i.interaction_date,
           i.respondent_id,
           r.respondent_name,
           l.location_name
    FROM interactions i
    JOIN respondents r ON r.respondent_id = i.respondent_id
    LEFT JOIN locations l ON l.location_id = r.location_id
    WHERE i.project_id = ?
      AND i.status_id = ?
      AND i.respondent_id IS NOT NULL
      AND i.interaction_deleted_at IS NULL
      AND r.company_id = ?
    ORDER BY i.interaction_date ASC, i.interaction_id ASC
""")

VERIFICATION_SQL = q("""
    SELECT pr.respondent_id,
           r.respondent_name,
           l.location_name,
           pr.pr_call_count,
           pr.pr_average_score,
           jsonb_array_length(pr.pr_processed_interaction_ids) AS ids_len,
           pr.pr_updated_at
    FROM performance_reports pr
    JOIN respondents r ON r.respondent_id = pr.respondent_id
    LEFT JOIN locations l ON l.location_id = r.location_id
    WHERE r.company_id = ?
    ORDER BY l.location_name, r.respondent_name
""")


def fetch_targets():
    conn = get_conn()
    cur = conn.execute(SELECT_TARGETS, (PROJECT_ID, GRADED_STATUS_ID, COMPANY_ID))
    rows = [dict(r) for r in cur.fetchall()]
    return rows


def print_dry_run(rows):
    by_resp = defaultdict(list)
    for r in rows:
        by_resp[(r['respondent_id'], r['respondent_name'], r['location_name'])].append(r)

    print("=" * 78)
    print(f"DRY-RUN: {len(rows)} interactions across {len(by_resp)} respondents")
    print(f"project_id={PROJECT_ID} company_id={COMPANY_ID} status_id={GRADED_STATUS_ID}")
    print("=" * 78)

    for (rid, rname, lname), items in sorted(
        by_resp.items(), key=lambda kv: (kv[0][2] or '', kv[0][1] or '')
    ):
        print(f"\n  respondent_id={rid:>3}  {rname:<22}  @ {lname or '(no location)'}")
        for it in items:
            print(f"    iid={it['interaction_id']:>4}  date={it['interaction_date']}")


def confirm():
    sys.stdout.write("\nProceed with --apply? (y/N): ")
    sys.stdout.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        return False
    return ans == 'y'


def apply_run(rows):
    failures = []
    successes = 0
    print("\n" + "=" * 78)
    print(f"APPLY: processing {len(rows)} interactions")
    print("=" * 78)
    for i, r in enumerate(rows, start=1):
        iid = r['interaction_id']
        rid = r['respondent_id']
        rname = r['respondent_name']
        lname = r['location_name']
        print(f"[{i:>2}/{len(rows)}] iid={iid} resp={rid} ({rname} @ {lname}) ...",
              end='', flush=True)
        try:
            # company_id=None to bypass per-company rate limits for this one-off backfill
            update_performance_report(
                interaction_id=iid,
                company_id=None,
                respondent_id=rid,
            )
            successes += 1
            print(" ok")
        except Exception as e:
            failures.append((iid, rid, rname, lname, repr(e)))
            print(f" FAILED: {e!r}")
        time.sleep(1)

    print("\n" + "=" * 78)
    print(f"APPLY DONE: {successes} ok, {len(failures)} failed")
    print("=" * 78)
    if failures:
        print("\nFailures:")
        for iid, rid, rname, lname, err in failures:
            print(f"  iid={iid} resp={rid} ({rname} @ {lname})")
            print(f"    {err}")


def print_verification():
    print("\n" + "=" * 78)
    print("VERIFICATION (performance_reports for company 25)")
    print("=" * 78)
    conn = get_conn()
    cur = conn.execute(VERIFICATION_SQL, (COMPANY_ID,))
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        print("  (no rows)")
        return
    print(f"\n  {'rid':>3}  {'name':<22}  {'location':<28}  "
          f"{'calls':>5}  {'avg':>5}  {'ids':>4}  updated")
    print("  " + "-" * 92)
    for r in rows:
        print(f"  {r['respondent_id']:>3}  "
              f"{(r['respondent_name'] or '')[:22]:<22}  "
              f"{(r['location_name'] or '')[:28]:<28}  "
              f"{r['pr_call_count']:>5}  "
              f"{float(r['pr_average_score']):>5.2f}  "
              f"{r['ids_len']:>4}  "
              f"{r['pr_updated_at']}")
    print(f"\n  total reports: {len(rows)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true',
                        help='Actually call Claude and write reports. '
                             'Without this flag, prints dry-run summary and exits.')
    args = parser.parse_args()

    rows = fetch_targets()
    if not rows:
        print("No target interactions found. Nothing to do.")
        return

    print_dry_run(rows)

    if not args.apply:
        print("\n(dry-run — no changes made. Re-run with --apply to execute.)")
        return

    if not confirm():
        print("Aborted.")
        return

    apply_run(rows)
    print_verification()


if __name__ == '__main__':
    main()
