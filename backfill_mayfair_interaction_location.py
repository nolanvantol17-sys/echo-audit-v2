"""One-time backfill of interactions.interaction_location_id for Mayfair project_id=30.

Purpose
-------
The new interaction_location_id column (added 2026-04-17 by
apply_schema_interaction_location_id.py) makes the caller's UI-selected
property the source-of-truth for "which property was this call targeting"
on every row, both answered and no-answer. The forward path populates it
from the UI selection at row-creation. This script backfills the 36 V1-
migrated Mayfair rows that were inserted before that column existed.

Two-subset logic
----------------
Project 30 currently has two subsets that need backfill:

  (a) 22 graded rows (status_id=43, respondent_id NOT NULL):
      For graded interactions the answering respondent's location IS the
      target property — V1 only ever recorded a single property per
      respondent. So we copy interactions.interaction_location_id from
      respondents.location_id (joined via interactions.respondent_id).

  (b) 14 no-answer rows (status_id=44, respondent_id NULL):
      No respondent is linked because nobody answered. The original V1
      property selection is preserved in migration_legacy_v1
      (entity_type='grade', v2_id=interaction_id) inside v1_payload as a
      'property_name' string. We resolve that name to a V2 location_id
      via case-insensitive locations.location_name match scoped to
      company_id=25.

Both subsets run inside a single BEGIN/COMMIT transaction so the backfill
either fully succeeds or rolls back entirely. No partial state.

Guards (all run BEFORE any UPDATE fires)
----------------------------------------
1. Pre-flight COUNT(*) assertions — separately for graded (expect 22)
   and no-answer (expect 14). If either count is not exactly as expected,
   halt with a clear error before any planning output is printed.

2. Graded-subset location guard — for each of the 22 graded rows, the
   respondent's location_id MUST be NOT NULL. If any respondent lacks a
   location, halt with the offending interaction_id list. (Verified
   2026-04-17 that all 22 are resolvable; this guard catches future drift.)

3. No-answer-subset resolution guard — for each of the 14 no-answer rows
   the migration_legacy_v1 row must exist, v1_payload->>'property_name'
   must be present, and exactly one company-25 location must match
   case-insensitive on location_name. Zero matches OR multiple matches
   OR missing payload → halt the entire transaction, surface the failing
   interaction_ids, rollback. Do NOT silently leave rows unresolved and
   commit the rest.

Idempotency
-----------
The pre-flight COUNT(*) restricts to rows where interaction_location_id
IS NULL, so re-running after a successful apply will report 0/0 and
short-circuit cleanly. The actual UPDATE statements also include the
IS NULL predicate so a partial re-run never overwrites an existing value.

Authorization
-------------
Run date: 2026-04-17
Authorized by: claude.cowork@mayfairmgt.com
Conversation: STEP B of the interaction_location_id rollout
              (STEP A = apply_schema_interaction_location_id.py, applied
              and committed earlier the same day).

Usage
-----
    railway run --service Postgres python3 backfill_mayfair_interaction_location.py
        # dry-run (default) — prints both subsets' plans, no writes

    railway run --service Postgres python3 backfill_mayfair_interaction_location.py --apply
        # prints dry-run, prompts y/N, runs both UPDATEs in a single
        # transaction, prints verification table, hard-asserts all 36
        # rows have non-NULL interaction_location_id matching their
        # source-of-truth. Exit 1 on any assertion failure.
"""

import argparse
import os
import sys

import psycopg2
import psycopg2.extras

PROJECT_ID = 30
COMPANY_ID = 25
GRADED_STATUS_ID = 43
NO_ANSWER_STATUS_ID = 44
EXPECTED_GRADED = 22
EXPECTED_NO_ANSWER = 14


# ── Database URL resolution ─────────────────────────────────────

def resolve_database_url():
    url = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.stderr.write(
            "ERROR: neither DATABASE_PUBLIC_URL nor DATABASE_URL is set. "
            "Run via `railway run --service Postgres python3 ...`.\n"
        )
        sys.exit(2)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode" not in url:
        url += "?sslmode=require" if "?" not in url else "&sslmode=require"
    return url


def host_hint(url):
    try:
        return url.split("@", 1)[1].split("/", 1)[0].split("?", 1)[0]
    except Exception:
        return "(unparseable)"


# ── Pre-flight count assertions ─────────────────────────────────

PREFLIGHT_GRADED_COUNT = """
    SELECT COUNT(*) AS n
    FROM interactions
    WHERE project_id = %s
      AND status_id = %s
      AND respondent_id IS NOT NULL
      AND interaction_deleted_at IS NULL
      AND interaction_location_id IS NULL
"""

PREFLIGHT_NO_ANSWER_COUNT = """
    SELECT COUNT(*) AS n
    FROM interactions
    WHERE project_id = %s
      AND status_id = %s
      AND interaction_deleted_at IS NULL
      AND interaction_location_id IS NULL
"""


def assert_preflight_counts(cur):
    cur.execute(PREFLIGHT_GRADED_COUNT, (PROJECT_ID, GRADED_STATUS_ID))
    g = cur.fetchone()["n"]
    cur.execute(PREFLIGHT_NO_ANSWER_COUNT, (PROJECT_ID, NO_ANSWER_STATUS_ID))
    n = cur.fetchone()["n"]

    print(f"Pre-flight: graded={g} (expected {EXPECTED_GRADED}), "
          f"no_answer={n} (expected {EXPECTED_NO_ANSWER})")

    errors = []
    if g != EXPECTED_GRADED:
        errors.append(f"graded subset count is {g}, expected {EXPECTED_GRADED}")
    if n != EXPECTED_NO_ANSWER:
        errors.append(f"no_answer subset count is {n}, expected {EXPECTED_NO_ANSWER}")
    if errors:
        sys.stderr.write("\nPRE-FLIGHT FAILED — halting before any planning output:\n")
        for e in errors:
            sys.stderr.write(f"  - {e}\n")
        sys.stderr.write(
            "\nIf this is expected (e.g. a previous partial backfill or new\n"
            "rows arrived), reconcile manually before re-running.\n"
        )
        sys.exit(2)


# ── Plan builders ───────────────────────────────────────────────

GRADED_PLAN = """
    SELECT i.interaction_id,
           i.interaction_date,
           r.respondent_id,
           r.respondent_name,
           r.location_id           AS target_location_id,
           l.location_name         AS target_location_name
    FROM interactions i
    JOIN respondents r ON r.respondent_id = i.respondent_id
    LEFT JOIN locations l ON l.location_id = r.location_id
    WHERE i.project_id = %s
      AND i.status_id  = %s
      AND i.respondent_id IS NOT NULL
      AND i.interaction_deleted_at IS NULL
      AND i.interaction_location_id IS NULL
    ORDER BY i.interaction_date ASC, i.interaction_id ASC
"""

NO_ANSWER_PLAN = """
    SELECT i.interaction_id,
           i.interaction_date,
           m.v1_payload->>'property_name' AS payload_property_name,
           (
               SELECT l.location_id
               FROM locations l
               WHERE l.company_id = %s
                 AND LOWER(l.location_name) = LOWER(m.v1_payload->>'property_name')
           ) AS target_location_id,
           (
               SELECT l.location_name
               FROM locations l
               WHERE l.company_id = %s
                 AND LOWER(l.location_name) = LOWER(m.v1_payload->>'property_name')
           ) AS target_location_name,
           (
               SELECT COUNT(*)
               FROM locations l
               WHERE l.company_id = %s
                 AND LOWER(l.location_name) = LOWER(m.v1_payload->>'property_name')
           ) AS match_count,
           (m.v2_id IS NULL) AS missing_legacy_row
    FROM interactions i
    LEFT JOIN migration_legacy_v1 m
           ON m.entity_type = 'grade'
          AND m.v2_id = i.interaction_id
    WHERE i.project_id = %s
      AND i.status_id  = %s
      AND i.interaction_deleted_at IS NULL
      AND i.interaction_location_id IS NULL
    ORDER BY i.interaction_date ASC, i.interaction_id ASC
"""


def build_graded_plan(cur):
    cur.execute(GRADED_PLAN, (PROJECT_ID, GRADED_STATUS_ID))
    rows = [dict(r) for r in cur.fetchall()]
    unresolved = [r for r in rows if r["target_location_id"] is None]
    if unresolved:
        sys.stderr.write(
            "\nGRADED-SUBSET GUARD FAILED — respondent has no location_id:\n"
        )
        for r in unresolved:
            sys.stderr.write(
                f"  - interaction_id={r['interaction_id']} "
                f"respondent_id={r['respondent_id']} "
                f"respondent_name={r['respondent_name']!r}\n"
            )
        sys.stderr.write(
            "\nFix the respondent's location_id first, then re-run.\n"
        )
        sys.exit(2)
    return rows


def build_no_answer_plan(cur):
    cur.execute(
        NO_ANSWER_PLAN,
        (COMPANY_ID, COMPANY_ID, COMPANY_ID, PROJECT_ID, NO_ANSWER_STATUS_ID),
    )
    rows = [dict(r) for r in cur.fetchall()]

    failures = []
    for r in rows:
        if r["missing_legacy_row"]:
            failures.append((r["interaction_id"], "no migration_legacy_v1 grade row"))
        elif r["payload_property_name"] is None:
            failures.append((r["interaction_id"], "v1_payload missing property_name"))
        elif r["match_count"] == 0:
            failures.append((
                r["interaction_id"],
                f"no V2 location matches property_name={r['payload_property_name']!r} "
                f"in company_id={COMPANY_ID}",
            ))
        elif r["match_count"] > 1:
            failures.append((
                r["interaction_id"],
                f"ambiguous: {r['match_count']} V2 locations match "
                f"property_name={r['payload_property_name']!r} "
                f"in company_id={COMPANY_ID}",
            ))

    if failures:
        sys.stderr.write(
            "\nNO-ANSWER-SUBSET GUARD FAILED — cannot resolve via migration_legacy_v1:\n"
        )
        for iid, reason in failures:
            sys.stderr.write(f"  - interaction_id={iid}: {reason}\n")
        sys.stderr.write(
            "\nResolve every row before re-running. The transaction will not\n"
            "commit a partial backfill.\n"
        )
        sys.exit(2)

    return rows


# ── Pretty plan printing ────────────────────────────────────────

def print_plan(graded_rows, no_answer_rows):
    print("=" * 88)
    print(f"DRY-RUN: backfill plan for project_id={PROJECT_ID} company_id={COMPANY_ID}")
    print(f"  graded subset    (status={GRADED_STATUS_ID}): {len(graded_rows):>2} rows")
    print(f"  no_answer subset (status={NO_ANSWER_STATUS_ID}): {len(no_answer_rows):>2} rows")
    print(f"  total                             : {len(graded_rows) + len(no_answer_rows):>2} rows")
    print("=" * 88)

    print("\n-- GRADED (source = respondents.location_id) " + "-" * 42)
    print(f"  {'iid':>4}  {'date':<10}  {'rid':>4}  {'respondent':<22}  →  "
          f"{'loc_id':>6}  {'location_name'}")
    print("  " + "-" * 84)
    for r in graded_rows:
        print(f"  {r['interaction_id']:>4}  {str(r['interaction_date']):<10}  "
              f"{r['respondent_id']:>4}  {(r['respondent_name'] or '')[:22]:<22}  →  "
              f"{r['target_location_id']:>6}  {r['target_location_name']}")

    print("\n-- NO-ANSWER (source = migration_legacy_v1.v1_payload->>'property_name') "
          + "-" * 13)
    print(f"  {'iid':>4}  {'date':<10}  {'payload property_name':<32}  →  "
          f"{'loc_id':>6}  {'location_name'}")
    print("  " + "-" * 84)
    for r in no_answer_rows:
        print(f"  {r['interaction_id']:>4}  {str(r['interaction_date']):<10}  "
              f"{(r['payload_property_name'] or '')[:32]:<32}  →  "
              f"{r['target_location_id']:>6}  {r['target_location_name']}")


# ── Apply + verify ──────────────────────────────────────────────

UPDATE_ONE = """
    UPDATE interactions
       SET interaction_location_id = %s
     WHERE interaction_id = %s
       AND interaction_location_id IS NULL
"""

VERIFICATION_SQL = """
    SELECT i.interaction_id,
           i.interaction_date,
           i.status_id,
           i.respondent_id,
           i.interaction_location_id,
           l.location_name,
           r.location_id                AS respondent_location_id,
           m.v1_payload->>'property_name' AS payload_property_name
    FROM interactions i
    LEFT JOIN locations l ON l.location_id = i.interaction_location_id
    LEFT JOIN respondents r ON r.respondent_id = i.respondent_id
    LEFT JOIN migration_legacy_v1 m
           ON m.entity_type = 'grade' AND m.v2_id = i.interaction_id
    WHERE i.project_id = %s
      AND i.status_id IN (%s, %s)
      AND i.interaction_deleted_at IS NULL
    ORDER BY i.interaction_date ASC, i.interaction_id ASC
"""


def confirm():
    sys.stdout.write("\nProceed with --apply? (y/N): ")
    sys.stdout.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        return False
    return ans == "y"


def apply_and_verify(conn, graded_rows, no_answer_rows):
    cur = conn.cursor()
    print("\n" + "=" * 88)
    print(f"APPLY: writing {len(graded_rows) + len(no_answer_rows)} rows in a single transaction")
    print("=" * 88)

    n_graded = 0
    for r in graded_rows:
        cur.execute(UPDATE_ONE, (r["target_location_id"], r["interaction_id"]))
        n_graded += cur.rowcount
    print(f"  graded:    {n_graded:>2} rows updated")

    n_no_answer = 0
    for r in no_answer_rows:
        cur.execute(UPDATE_ONE, (r["target_location_id"], r["interaction_id"]))
        n_no_answer += cur.rowcount
    print(f"  no_answer: {n_no_answer:>2} rows updated")

    # Verify inside the same transaction so a failed assertion rolls back.
    cur.execute(
        VERIFICATION_SQL,
        (PROJECT_ID, GRADED_STATUS_ID, NO_ANSWER_STATUS_ID),
    )
    rows = [dict(r) for r in cur.fetchall()]

    print("\n" + "=" * 88)
    print(f"VERIFICATION ({len(rows)} rows expected: "
          f"{EXPECTED_GRADED} + {EXPECTED_NO_ANSWER} = "
          f"{EXPECTED_GRADED + EXPECTED_NO_ANSWER})")
    print("=" * 88)
    print(f"\n  {'iid':>4}  {'date':<10}  {'st':>2}  {'rid':>4}  "
          f"{'loc_id':>6}  {'location_name':<28}  source_check")
    print("  " + "-" * 84)

    failures = []
    for r in rows:
        iid = r["interaction_id"]
        loc_id = r["interaction_location_id"]
        loc_name = r["location_name"] or ""
        status = r["status_id"]

        if loc_id is None:
            check = "FAIL: interaction_location_id IS NULL"
            failures.append((iid, check))
        elif status == GRADED_STATUS_ID:
            if r["respondent_location_id"] is None:
                check = "FAIL: respondent has no location_id"
                failures.append((iid, check))
            elif loc_id != r["respondent_location_id"]:
                check = (f"FAIL: loc_id={loc_id} ≠ "
                         f"respondent.location_id={r['respondent_location_id']}")
                failures.append((iid, check))
            else:
                check = "ok (matches respondent.location_id)"
        elif status == NO_ANSWER_STATUS_ID:
            payload = r["payload_property_name"]
            if payload is None:
                check = "FAIL: v1_payload missing property_name"
                failures.append((iid, check))
            elif (loc_name or "").lower() != payload.lower():
                check = (f"FAIL: location_name={loc_name!r} ≠ "
                         f"payload property_name={payload!r}")
                failures.append((iid, check))
            else:
                check = "ok (matches v1_payload property_name)"
        else:
            check = f"FAIL: unexpected status_id={status}"
            failures.append((iid, check))

        print(f"  {iid:>4}  {str(r['interaction_date']):<10}  {status:>2}  "
              f"{(r['respondent_id'] or 0):>4}  "
              f"{(loc_id or 0):>6}  {loc_name[:28]:<28}  {check}")

    print("\n  total verified rows:", len(rows))

    if len(rows) != EXPECTED_GRADED + EXPECTED_NO_ANSWER:
        failures.append((None,
                         f"row count mismatch: got {len(rows)}, "
                         f"expected {EXPECTED_GRADED + EXPECTED_NO_ANSWER}"))

    if failures:
        sys.stderr.write("\nVERIFICATION FAILED — rolling back:\n")
        for iid, reason in failures:
            sys.stderr.write(f"  - iid={iid}: {reason}\n")
        conn.rollback()
        sys.exit(1)

    conn.commit()
    print("\nCOMMIT — backfill verified and committed.")


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually run the UPDATEs. Without this flag, prints the "
             "dry-run plan and exits.",
    )
    args = parser.parse_args()

    url = resolve_database_url()
    print(f"Target DB host: {host_hint(url)}")

    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    try:
        cur = conn.cursor()

        # Pre-flight assertions BEFORE any planning output.
        assert_preflight_counts(cur)

        graded_rows = build_graded_plan(cur)
        no_answer_rows = build_no_answer_plan(cur)

        print_plan(graded_rows, no_answer_rows)

        if not args.apply:
            print("\n(dry-run — no changes made. Re-run with --apply to execute.)")
            return

        if not confirm():
            print("Aborted.")
            return

        apply_and_verify(conn, graded_rows, no_answer_rows)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
