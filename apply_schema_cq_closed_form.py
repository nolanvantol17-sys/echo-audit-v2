"""Closed-form clarifying questions: schema additions for PR 5/4 Phase 2a.

Purpose
-------
PR 5 reshapes the clarifying-question (CQ) flow into a closed-form-only,
one-question-at-a-time wizard. The grader prompt is being narrowed to ask
only yes_no, yes_no_unclear, or multiple_choice questions; if Claude ever
returns an open-text or otherwise-invalid shape, server-side validation
will coerce it to a new "skip_only" type (review-only acknowledgement,
no answer affordance). This migration extends the schema to support that:

  1. clarifying_questions.cq_options
       NEW nullable TEXT column. For multiple_choice rows we now persist
       the option list as a JSON array string so the wizard can render
       buttons without re-asking Claude. Null for every other row type
       (yes_no, yes_no_unclear, skip_only) and for legacy multiple_choice
       rows that predate this column — the grade.html legacy renderer
       handles the missing-options case.

  2. chk_cq_response_format CHECK constraint
       Existing constraint allows only ('yes_no','scale_1_10','multiple_choice').
       Replaced with ('yes_no','yes_no_unclear','multiple_choice','skip_only',
       'scale_1_10'). 'scale_1_10' is retained ONLY for backward compatibility
       with the 0 historical rows currently using it (probe 2026-04-21
       confirmed: 4 total CQ rows — 3 yes_no, 1 multiple_choice, 0
       scale_1_10). Once the new prompt ships, no fresh scale_1_10 rows
       will be written; existing ones (if any are inserted between probe
       and apply) will continue to render under the legacy static path.

Data-migration risk
-------------------
Probe 2026-04-21 confirmed:
   * cq_options column does NOT yet exist
   * 4 CQ rows total: 3 yes_no, 1 multiple_choice, 0 scale_1_10
All steps are additive — column add is nullable with no default backfill
needed; constraint replacement is a strict superset of the prior allowed
set, so no existing row can violate the new constraint.

All steps run inside one BEGIN/COMMIT transaction. In-tx verification
asserts the post-state shape; any failure rolls back the entire migration.

App-code follow-ups (in this PR's commits 2a/2b/2c)
---------------------------------------------------
schema.sql is updated alongside this script. Application code referencing
the new shape:

    grader.py                       prompt + validate_clarifying_questions
                                    rewritten to closed-form set, coerce
                                    invalids to skip_only
    interactions_routes.py          _save_clarifying_questions writes
                                    cq_options for multiple_choice rows;
                                    _apply_clarifying_answers passes through
                                    "__SKIPPED__" sentinel verbatim
    templates/grade.html            wizard renderer (commit 2b)
    static/interaction_panel.js     reuse for inline grade result (commit 2c)

Usage
-----
    railway run --service Postgres python3 apply_schema_cq_closed_form.py
        # dry-run (default): probes current state, prints the exact SQL
        # that would run, exits without a connection write

    railway run --service Postgres python3 apply_schema_cq_closed_form.py --apply
        # same probe + plan print, prompts y/N, then runs the entire
        # migration in a single transaction. In-tx verification asserts
        # the post-state shape; any failure → rollback → exit 1.

Authorization
-------------
Authorized by: claude.cowork@mayfairmgt.com
Conversation:  "PR 5 of 4 — Grade Flow UX redesign (Phase 2a backend)"
Run date:      2026-04-21
"""

import argparse
import os
import sys

import psycopg2
import psycopg2.extras


# ── Probe expectations (from 2026-04-21 probe) ─────────────────
EXPECTED_CQ_OPTIONS_COL_EXISTS = 0       # not yet added
EXPECTED_TOTAL_CQ_ROWS         = 4       # baseline; informational only
EXPECTED_SCALE_1_10_ROWS       = 0       # nothing legacy to worry about


# ── DB URL resolution (matches apply_schema_campaigns_split.py) ──

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


# ── Pre-flight probe ────────────────────────────────────────────

def preflight(cur):
    print("=" * 88)
    print("PRE-FLIGHT")
    print("=" * 88)

    checks = []

    cur.execute("""
        SELECT COUNT(*) AS n FROM information_schema.columns
         WHERE table_name = 'clarifying_questions'
           AND column_name = 'cq_options'
    """)
    n = cur.fetchone()["n"]
    checks.append(("cq_options column NOT yet present", n, EXPECTED_CQ_OPTIONS_COL_EXISTS))

    cur.execute("""
        SELECT COUNT(*) AS n FROM clarifying_questions
         WHERE cq_response_format = 'scale_1_10'
    """)
    n = cur.fetchone()["n"]
    checks.append(("legacy scale_1_10 rows (informational)", n, EXPECTED_SCALE_1_10_ROWS))

    # Total CQ rows is informational — print but don't gate on it.
    cur.execute("SELECT COUNT(*) AS n FROM clarifying_questions")
    total = cur.fetchone()["n"]
    print(f"  INFO  total clarifying_questions rows         actual={total} (baseline={EXPECTED_TOTAL_CQ_ROWS})")

    any_fail = False
    for label, actual, expected in checks:
        ok = (actual == expected)
        any_fail = any_fail or not ok
        print(f"  {'OK  ' if ok else 'FAIL'}  {label:<50} actual={actual}  expected={expected}")

    if any_fail:
        sys.stderr.write(
            "\nPRE-FLIGHT FAILED — halting before any migration output.\n"
            "The database shape no longer matches what the probe saw on\n"
            "2026-04-21. Re-run the probe, reconcile, then edit the\n"
            "EXPECTED_* constants here if the new state is acceptable.\n"
        )
        sys.exit(2)
    print("\n  (all pre-flight checks passed)\n")


# ── Plan print (dry-run body + --apply preview) ────────────────

PLAN_SQL = """
-- 1. Add nullable cq_options column. Stores a JSON array of MC option
--    strings for multiple_choice rows; NULL for every other row type.
ALTER TABLE clarifying_questions
    ADD COLUMN cq_options TEXT NULL;

-- 2. Replace the response-format CHECK to allow the new closed-form set.
--    'scale_1_10' is kept ONLY for backward compatibility with any
--    historical rows; the new prompt will not produce more.
ALTER TABLE clarifying_questions
    DROP CONSTRAINT chk_cq_response_format;
ALTER TABLE clarifying_questions
    ADD  CONSTRAINT chk_cq_response_format
    CHECK (cq_response_format IN (
        'yes_no',
        'yes_no_unclear',
        'multiple_choice',
        'skip_only',
        'scale_1_10'
    ));
""".strip()


def print_plan():
    print("=" * 88)
    print("PLAN (SQL that will be executed inside a single transaction)")
    print("=" * 88)
    print(PLAN_SQL)
    print()


# ── Apply + verify (single transaction) ─────────────────────────

def confirm():
    sys.stdout.write("\nProceed with --apply? (y/N): ")
    sys.stdout.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        return False
    return ans == "y"


def apply_and_verify(conn):
    cur = conn.cursor()
    print("=" * 88)
    print("APPLY: executing migration")
    print("=" * 88)

    cur.execute(PLAN_SQL)
    print("  plan SQL executed without error")

    print("\n" + "=" * 88)
    print("VERIFY (inside transaction, pre-commit)")
    print("=" * 88)

    failures = []

    def expect(label, sql, params, expected):
        cur.execute(sql, params)
        row = cur.fetchone()
        actual = row[0] if not isinstance(row, dict) else row[list(row.keys())[0]]
        ok = (actual == expected)
        print(f"  {'OK  ' if ok else 'FAIL'}  {label:<60} actual={actual}  expected={expected}")
        if not ok:
            failures.append(f"{label}: actual={actual}, expected={expected}")

    # 1. cq_options column exists, nullable, type text
    expect(
        "clarifying_questions.cq_options exists",
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='clarifying_questions' AND column_name='cq_options'",
        (), 1,
    )
    expect(
        "clarifying_questions.cq_options is nullable",
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='clarifying_questions' AND column_name='cq_options' "
        "AND is_nullable='YES'",
        (), 1,
    )
    expect(
        "clarifying_questions.cq_options is text",
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='clarifying_questions' AND column_name='cq_options' "
        "AND data_type='text'",
        (), 1,
    )

    # 2. CHECK constraint exists with the new allowed set
    expect(
        "chk_cq_response_format constraint exists",
        "SELECT COUNT(*) FROM pg_constraint "
        "WHERE conname='chk_cq_response_format'",
        (), 1,
    )
    # Sanity-check by attempting an INSERT-and-rollback for each new type.
    for new_type in ("yes_no_unclear", "skip_only"):
        cur.execute("SAVEPOINT sp_check_type")
        try:
            cur.execute(
                """INSERT INTO clarifying_questions
                       (interaction_id, cq_text, cq_ai_reason,
                        cq_response_format, cq_order)
                   SELECT interaction_id, '__verify__', '__verify__',
                          %s, -1
                     FROM interactions
                    LIMIT 1""",
                (new_type,),
            )
            cur.execute("ROLLBACK TO SAVEPOINT sp_check_type")
            print(f"  OK    new CHECK accepts cq_response_format='{new_type}'")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp_check_type")
            print(f"  FAIL  new CHECK rejected cq_response_format='{new_type}': {e}")
            failures.append(f"new CHECK rejected '{new_type}': {e}")

    # 3. Legacy types still accepted (yes_no, multiple_choice, scale_1_10)
    for legacy_type in ("yes_no", "multiple_choice", "scale_1_10"):
        cur.execute("SAVEPOINT sp_check_legacy")
        try:
            cur.execute(
                """INSERT INTO clarifying_questions
                       (interaction_id, cq_text, cq_ai_reason,
                        cq_response_format, cq_order)
                   SELECT interaction_id, '__verify__', '__verify__',
                          %s, -1
                     FROM interactions
                    LIMIT 1""",
                (legacy_type,),
            )
            cur.execute("ROLLBACK TO SAVEPOINT sp_check_legacy")
            print(f"  OK    new CHECK still accepts cq_response_format='{legacy_type}'")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp_check_legacy")
            print(f"  FAIL  new CHECK rejected legacy '{legacy_type}': {e}")
            failures.append(f"new CHECK rejected legacy '{legacy_type}': {e}")

    # 4. An invalid type is still rejected
    cur.execute("SAVEPOINT sp_check_invalid")
    try:
        cur.execute(
            """INSERT INTO clarifying_questions
                   (interaction_id, cq_text, cq_ai_reason,
                    cq_response_format, cq_order)
               SELECT interaction_id, '__verify__', '__verify__',
                      'open_text', -1
                 FROM interactions
                LIMIT 1""",
        )
        cur.execute("ROLLBACK TO SAVEPOINT sp_check_invalid")
        failures.append("new CHECK accepted 'open_text' which is invalid")
        print("  FAIL  new CHECK should have rejected 'open_text'")
    except psycopg2.errors.CheckViolation:
        cur.execute("ROLLBACK TO SAVEPOINT sp_check_invalid")
        print("  OK    new CHECK correctly rejects 'open_text'")
    except Exception as e:
        cur.execute("ROLLBACK TO SAVEPOINT sp_check_invalid")
        failures.append(f"unexpected error testing 'open_text' rejection: {e}")
        print(f"  FAIL  unexpected error testing 'open_text' rejection: {e}")

    if failures:
        sys.stderr.write("\nVERIFICATION FAILED — rolling back:\n")
        for f in failures:
            sys.stderr.write(f"  - {f}\n")
        conn.rollback()
        sys.exit(1)

    conn.commit()
    print("\nCOMMIT — migration verified and committed.")


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually execute the migration inside a transaction. "
             "Without this flag, prints the pre-flight probe + SQL plan "
             "and exits.",
    )
    args = parser.parse_args()

    url = resolve_database_url()
    print(f"Target DB host: {host_hint(url)}\n")

    conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    try:
        cur = conn.cursor()

        preflight(cur)
        print_plan()

        if not args.apply:
            print("(dry-run — no changes made. Re-run with --apply to execute.)")
            return

        if not confirm():
            print("Aborted.")
            return

        apply_and_verify(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
