"""Split the 'campaign' concept into phone_routing + new campaigns.

Purpose
-------
The single legacy `campaigns` table currently models VoIP phone-tree
configuration (location-scoped). The product is introducing a second,
unrelated concept also called "campaign" — a time-bounded batch within a
project (e.g. "April 2026"). To remove the name collision before any
product code is written, we split the two concepts in a single atomic
migration:

  1. Legacy campaigns  → RENAMED IN PLACE to phone_routing
       - table, PK column, non-key columns, sequence, pk index,
         location_id index, trigger, trigger function all renamed
       - FK projects.campaign_id   → projects.phone_routing_id
         (index and constraint also renamed)
       - company_settings EAV key  campaign_label → phone_routing_label
         (value preserved forward)
       - audit_log_target_entity_types id=4 name 'campaign' →
         'phone_routing' (id stays stable)

  2. New campaigns concept  (fresh table)
       - campaigns(campaign_id SERIAL PK,
                   project_id   FK NOT NULL ON DELETE CASCADE,
                   campaign_name, campaign_deleted_at,
                   campaign_created_at, campaign_updated_at)
       - idx on project_id (partial, excludes soft-deleted)
       - fresh set_campaign_updated_at() function + trigger
       - audit_log_target_entity_types row (11, 'campaign') allocated
         from next-available id (probe confirmed 11 free)

  3. interactions.campaign_id  (new, nullable, FK to new campaigns)
       - ON DELETE SET NULL, partial index excluding soft-deleted

Data-migration risk is essentially nil — probe 2026-04-21 confirmed:
   * 0 rows in legacy campaigns table
   * 0 projects with campaign_id IS NOT NULL
   * 0 audit_log rows targeting entity_type_id=4
   * 1 company_settings row keyed 'campaign_label' (value 'Campaign',
     which is the default — the rename preserves it as-is).

All steps run in one BEGIN/COMMIT transaction. In-tx verification asserts
the before/after shape before committing; any failure rolls back the
entire migration.

Modelling decision — campaigns.project_id
-----------------------------------------
NOT NULL with ON DELETE CASCADE. Rationale: in the new model a campaign
is always a child of exactly one project; allowing NULL invites drift
(orphan campaigns that can't be surfaced anywhere in the product UI).
When the project is deleted the campaigns below it are no longer
addressable, so cascading the delete keeps the tree consistent.
(interactions.campaign_id is SET NULL, not cascade, because an individual
call's history should survive the removal of a campaign bucket.)

App-code follow-ups (NOT part of this PR)
-----------------------------------------
schema.sql is updated alongside this script. Application code still
writes under the old names and must be updated in follow-up PRs:

    db.py                          whitelist + defaults: 'campaign_label'
                                   → 'phone_routing_label'; seed
                                   _TARGET_ENTITY_TYPE_SEEDS id=4 name
                                   → 'phone_routing', add (11, 'campaign')
    api_routes.py                  /api/campaigns CRUD → /api/phone_routing
    dashboard_routes.py            campaign filter → phone_routing filter;
                                   add new campaign filter
    interactions_routes.py         join columns (campaign_name → *_name)
    export_routes.py               export/import column names
    templates/projects.html        wizard step 4 label
    templates/grade.html           "Campaign / Type" label
    static/dashboard_widget.js     By-Campaign multiselect
    static/interaction_view.js     breadcrumb
    audit_log.py                   TARGETS comment

Usage
-----
    railway run --service Postgres python3 apply_schema_campaigns_split.py
        # dry-run (default): probes current state, prints the exact SQL
        # that would run, exits without a connection write

    railway run --service Postgres python3 apply_schema_campaigns_split.py --apply
        # same probe + plan print, prompts y/N, then runs the entire
        # migration in a single transaction. In-tx verification asserts
        # schema shape + row counts; any failure → rollback → exit 1.

Authorization
-------------
Authorized by: claude.cowork@mayfairmgt.com
Conversation:  "PR 1 of 4 — Schema split for campaigns concept rework"
Run date:      2026-04-21
"""

import argparse
import os
import sys

import psycopg2
import psycopg2.extras


# ── Probe expectations (from 2026-04-21 probe) ─────────────────
# Pre-flight asserts these numbers so a later state drift (new tenant,
# someone creates campaigns via the current UI, etc.) fails fast rather
# than silently running against a world we never tested.
EXPECTED_CAMPAIGN_ROWS             = 0
EXPECTED_PROJECTS_WITH_CAMPAIGN_ID = 0
EXPECTED_CAMPAIGN_LABEL_SETTINGS   = 1   # Callbay Blue company_id=26
EXPECTED_AUDIT_LOG_TYPE_4_ROWS     = 0   # nothing has ever audited old campaign
NEW_TARGET_ENTITY_TYPE_ID          = 11  # verified free


# ── DB URL resolution (matches backfill_mayfair_interaction_location.py) ──

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


# ── Pre-flight probe (blocks migration on unexpected state) ────

def preflight(cur):
    print("=" * 88)
    print("PRE-FLIGHT")
    print("=" * 88)

    checks = []

    cur.execute("SELECT COUNT(*) AS n FROM campaigns")
    n = cur.fetchone()["n"]
    checks.append(("campaigns rows", n, EXPECTED_CAMPAIGN_ROWS))

    cur.execute("SELECT COUNT(*) AS n FROM projects WHERE campaign_id IS NOT NULL")
    n = cur.fetchone()["n"]
    checks.append(("projects with campaign_id", n, EXPECTED_PROJECTS_WITH_CAMPAIGN_ID))

    cur.execute("""
        SELECT COUNT(*) AS n FROM company_settings
         WHERE company_setting_key = 'campaign_label'
    """)
    n = cur.fetchone()["n"]
    checks.append(("company_settings campaign_label rows", n, EXPECTED_CAMPAIGN_LABEL_SETTINGS))

    cur.execute("SELECT COUNT(*) AS n FROM audit_log WHERE audit_log_target_entity_type_id = 4")
    n = cur.fetchone()["n"]
    checks.append(("audit_log rows target_entity_type_id=4", n, EXPECTED_AUDIT_LOG_TYPE_4_ROWS))

    cur.execute("""
        SELECT COUNT(*) AS n FROM audit_log_target_entity_types
         WHERE audit_log_target_entity_type_id = %s
    """, (NEW_TARGET_ENTITY_TYPE_ID,))
    n = cur.fetchone()["n"]
    checks.append((
        f"audit_log_target_entity_types.id={NEW_TARGET_ENTITY_TYPE_ID} free (expect 0)",
        n, 0,
    ))

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
-- 1. Drop old trigger + function (function is 1:1 with old table)
DROP TRIGGER IF EXISTS trg_campaigns_updated_at ON campaigns;
DROP FUNCTION IF EXISTS set_campaign_updated_at();

-- 2. Rename legacy campaigns → phone_routing (columns, table, seq, indexes)
ALTER TABLE campaigns RENAME COLUMN campaign_name       TO phone_routing_name;
ALTER TABLE campaigns RENAME COLUMN campaign_created_at TO phone_routing_created_at;
ALTER TABLE campaigns RENAME COLUMN campaign_updated_at TO phone_routing_updated_at;
ALTER TABLE campaigns RENAME COLUMN campaign_id         TO phone_routing_id;
ALTER TABLE campaigns RENAME TO phone_routing;
ALTER SEQUENCE campaigns_campaign_id_seq    RENAME TO phone_routing_phone_routing_id_seq;
ALTER INDEX    campaigns_pkey               RENAME TO phone_routing_pkey;
ALTER INDEX    idx_campaigns_location_id    RENAME TO idx_phone_routing_location_id;

-- 3. Fresh trigger + function bound to the renamed table
CREATE FUNCTION set_phone_routing_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.phone_routing_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_phone_routing_updated_at BEFORE UPDATE ON phone_routing
    FOR EACH ROW EXECUTE FUNCTION set_phone_routing_updated_at();

-- 4. Point projects FK at the renamed table (column + constraint + index)
ALTER TABLE projects RENAME COLUMN campaign_id TO phone_routing_id;
ALTER TABLE projects RENAME CONSTRAINT projects_campaign_id_fkey
                                    TO projects_phone_routing_id_fkey;
ALTER INDEX idx_projects_campaign_id RENAME TO idx_projects_phone_routing_id;

-- 5. Rename EAV setting key (value preserved)
UPDATE company_settings
   SET company_setting_key = 'phone_routing_label'
 WHERE company_setting_key = 'campaign_label';

-- 6. Re-point audit entity-type id=4 (id stable, name updated)
UPDATE audit_log_target_entity_types
   SET audit_log_target_entity_type_name = 'phone_routing'
 WHERE audit_log_target_entity_type_id   = 4;

-- 7. Allocate new audit entity type for the NEW campaigns concept
INSERT INTO audit_log_target_entity_types
    (audit_log_target_entity_type_id, audit_log_target_entity_type_name)
VALUES (11, 'campaign');

-- 8. Create NEW campaigns table (time-bounded batches within a project)
CREATE TABLE campaigns (
    campaign_id         SERIAL PRIMARY KEY,
    project_id          INTEGER NOT NULL
                            REFERENCES projects (project_id) ON DELETE CASCADE,
    campaign_name       TEXT NOT NULL,
    campaign_deleted_at TIMESTAMPTZ,
    campaign_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    campaign_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_campaigns_project_id ON campaigns (project_id)
    WHERE campaign_deleted_at IS NULL;

CREATE FUNCTION set_campaign_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.campaign_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_campaigns_updated_at BEFORE UPDATE ON campaigns
    FOR EACH ROW EXECUTE FUNCTION set_campaign_updated_at();

-- 9. interactions.campaign_id (nullable, FK to NEW campaigns)
ALTER TABLE interactions
    ADD COLUMN campaign_id INTEGER
        REFERENCES campaigns (campaign_id) ON DELETE SET NULL;
CREATE INDEX idx_interactions_campaign_id ON interactions (campaign_id)
    WHERE interaction_deleted_at IS NULL AND campaign_id IS NOT NULL;
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

    # Execute the whole block as one multi-statement script. psycopg2
    # sends it as a single extended query; any error raises and the outer
    # `with conn:` / manual commit below won't fire.
    cur.execute(PLAN_SQL)
    print("  plan SQL executed without error")

    # ── In-transaction verification ───────────────────────────
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

    # 1. old campaigns table gone
    expect(
        "legacy 'campaigns' table should NOT exist as the old shape",
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'campaigns' AND table_schema = 'public' "
        "AND EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'campaigns' AND column_name = 'phone_routing_id')",
        (), 0,
    )

    # 2. phone_routing table has expected columns
    for col in ("phone_routing_id", "location_id", "phone_routing_name",
                "phone_routing_created_at", "phone_routing_updated_at"):
        expect(
            f"phone_routing.{col} exists",
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='phone_routing' AND column_name=%s",
            (col,), 1,
        )

    # 3. projects.phone_routing_id exists and campaign_id does not
    expect(
        "projects.phone_routing_id exists",
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='projects' AND column_name='phone_routing_id'",
        (), 1,
    )
    expect(
        "projects.campaign_id NO LONGER exists",
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='projects' AND column_name='campaign_id'",
        (), 0,
    )

    # 4. new campaigns table with its new columns
    for col, not_null in (
        ("campaign_id", True),
        ("project_id", True),
        ("campaign_name", True),
        ("campaign_deleted_at", False),
        ("campaign_created_at", True),
        ("campaign_updated_at", True),
    ):
        expect(
            f"new campaigns.{col} exists",
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name='campaigns' AND column_name=%s",
            (col,), 1,
        )

    # 5. interactions.campaign_id exists
    expect(
        "interactions.campaign_id exists",
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name='interactions' AND column_name='campaign_id'",
        (), 1,
    )

    # 6. FK graph
    expect(
        "FK projects.phone_routing_id → phone_routing(phone_routing_id)",
        """
        SELECT COUNT(*)
          FROM pg_constraint c
          JOIN pg_class   cr ON cr.oid = c.conrelid
          JOIN pg_class   cf ON cf.oid = c.confrelid
         WHERE c.contype='f'
           AND cr.relname='projects'
           AND cf.relname='phone_routing'
           AND c.conname='projects_phone_routing_id_fkey'
        """, (), 1,
    )
    expect(
        "FK campaigns.project_id → projects(project_id)",
        """
        SELECT COUNT(*)
          FROM pg_constraint c
          JOIN pg_class   cr ON cr.oid = c.conrelid
          JOIN pg_class   cf ON cf.oid = c.confrelid
         WHERE c.contype='f'
           AND cr.relname='campaigns'
           AND cf.relname='projects'
        """, (), 1,
    )
    expect(
        "FK interactions.campaign_id → campaigns(campaign_id)",
        """
        SELECT COUNT(*)
          FROM pg_constraint c
          JOIN pg_class   cr ON cr.oid = c.conrelid
          JOIN pg_class   cf ON cf.oid = c.confrelid
         WHERE c.contype='f'
           AND cr.relname='interactions'
           AND cf.relname='campaigns'
        """, (), 1,
    )

    # 7. audit_log_target_entity_types updates
    expect(
        "audit_log_target_entity_types id=4 name = 'phone_routing'",
        "SELECT COUNT(*) FROM audit_log_target_entity_types "
        "WHERE audit_log_target_entity_type_id=4 "
        "AND audit_log_target_entity_type_name='phone_routing'",
        (), 1,
    )
    expect(
        f"audit_log_target_entity_types id={NEW_TARGET_ENTITY_TYPE_ID} name = 'campaign'",
        "SELECT COUNT(*) FROM audit_log_target_entity_types "
        "WHERE audit_log_target_entity_type_id=%s "
        "AND audit_log_target_entity_type_name='campaign'",
        (NEW_TARGET_ENTITY_TYPE_ID,), 1,
    )

    # 8. EAV key renamed, value preserved
    expect(
        "company_settings no rows with key='campaign_label'",
        "SELECT COUNT(*) FROM company_settings "
        "WHERE company_setting_key='campaign_label'", (), 0,
    )
    expect(
        "company_settings rows with key='phone_routing_label' match prior count",
        "SELECT COUNT(*) FROM company_settings "
        "WHERE company_setting_key='phone_routing_label'",
        (), EXPECTED_CAMPAIGN_LABEL_SETTINGS,
    )

    # 9. indexes renamed / created
    for idx in (
        "phone_routing_pkey",
        "idx_phone_routing_location_id",
        "idx_projects_phone_routing_id",
        "idx_campaigns_project_id",
        "idx_interactions_campaign_id",
    ):
        expect(
            f"index {idx} exists",
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE schemaname='public' AND indexname=%s", (idx,), 1,
        )

    # 10. triggers
    for trig, tbl in (
        ("trg_phone_routing_updated_at", "phone_routing"),
        ("trg_campaigns_updated_at",     "campaigns"),
    ):
        expect(
            f"trigger {trig} on {tbl} exists",
            "SELECT COUNT(*) FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname=%s AND c.relname=%s",
            (trig, tbl), 1,
        )

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
