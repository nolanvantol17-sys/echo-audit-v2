"""
teardown_test_data.py — Remove rows created by the e2e test harness.

Identifies test-created companies by name pattern (Test Company / OtherCo /
PlatOrg / Signup Co / API-Created / API-Renamed — all either timestamp-
suffixed or bearing the API-* prefix) and deletes them plus their entire
dependency subtree in reverse-FK order.

Preserves any company whose name doesn't match those patterns.
"""

import sys
from dotenv import load_dotenv
load_dotenv(".env")

import os
from cryptography.fernet import Fernet
os.environ.setdefault("VOIP_ENCRYPTION_KEY", Fernet.generate_key().decode())

import logging
logging.disable(logging.CRITICAL)

import db

TEST_NAME_CLAUSE = """(
    company_name ~ '[0-9]{10}'
    OR company_name LIKE 'API-Created %%'
    OR company_name LIKE 'API-Renamed %%'
    OR company_name LIKE 'Test Company %%'
    OR company_name LIKE 'OtherCo %%'
    OR company_name LIKE 'PlatOrg %%'
    OR company_name LIKE 'Signup Co %%'
)"""


def fetchall(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def main():
    conn = db.get_conn()
    deleted = {}
    try:
        test_cos = fetchall(
            conn,
            f"SELECT company_id, company_name FROM companies WHERE {TEST_NAME_CLAUSE} ORDER BY company_id",
        )
        preserved = fetchall(
            conn,
            f"SELECT company_id, company_name FROM companies WHERE NOT {TEST_NAME_CLAUSE} ORDER BY company_id",
        )
        print(f"Test companies to delete:   {len(test_cos)}")
        for r in test_cos:
            print(f"  {r['company_id']:3d}  {r['company_name']}")
        print(f"Companies to preserve:      {len(preserved)}")
        for r in preserved:
            print(f"  {r['company_id']:3d}  {r['company_name']}")
        print()

        if not test_cos:
            print("Nothing to delete.")
            return

        co_ids = tuple(r["company_id"] for r in test_cos)

        # Resolve all dependent IDs up front so we can delete in order without
        # having to re-query after each step.
        dept_ids = [r["department_id"] for r in fetchall(
            conn,
            f"SELECT department_id FROM departments WHERE company_id IN ({','.join(['%s'] * len(co_ids))})",
            co_ids,
        )]
        user_ids = [r["user_id"] for r in fetchall(
            conn,
            f"SELECT user_id FROM users WHERE department_id IN ({','.join(['%s'] * len(dept_ids))})"
            if dept_ids else "SELECT user_id FROM users WHERE FALSE",
            tuple(dept_ids),
        )] if dept_ids else []
        loc_ids = [r["location_id"] for r in fetchall(
            conn,
            f"SELECT location_id FROM locations WHERE company_id IN ({','.join(['%s'] * len(co_ids))})",
            co_ids,
        )]
        proj_ids = [r["project_id"] for r in fetchall(
            conn,
            f"SELECT project_id FROM projects WHERE company_id IN ({','.join(['%s'] * len(co_ids))})",
            co_ids,
        )]
        rg_ids = [r["rubric_group_id"] for r in fetchall(
            conn,
            f"SELECT rubric_group_id FROM rubric_groups WHERE location_id IN ({','.join(['%s'] * len(loc_ids))})"
            if loc_ids else "SELECT rubric_group_id FROM rubric_groups WHERE FALSE",
            tuple(loc_ids),
        )] if loc_ids else []
        ri_ids = [r["rubric_item_id"] for r in fetchall(
            conn,
            f"SELECT rubric_item_id FROM rubric_items WHERE rubric_group_id IN ({','.join(['%s'] * len(rg_ids))})"
            if rg_ids else "SELECT rubric_item_id FROM rubric_items WHERE FALSE",
            tuple(rg_ids),
        )] if rg_ids else []
        interaction_ids = [r["interaction_id"] for r in fetchall(
            conn,
            f"SELECT interaction_id FROM interactions WHERE project_id IN ({','.join(['%s'] * len(proj_ids))})"
            if proj_ids else "SELECT interaction_id FROM interactions WHERE FALSE",
            tuple(proj_ids),
        )] if proj_ids else []

        print(f"Dependent rows:")
        print(f"  departments        {len(dept_ids)}")
        print(f"  users              {len(user_ids)}")
        print(f"  locations          {len(loc_ids)}")
        print(f"  projects           {len(proj_ids)}")
        print(f"  rubric_groups      {len(rg_ids)}")
        print(f"  rubric_items       {len(ri_ids)}")
        print(f"  interactions       {len(interaction_ids)}")
        print()

        def delete_in(table, col, ids):
            if not ids:
                deleted[table] = 0
                return
            placeholders = ",".join(["%s"] * len(ids))
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {col} IN ({placeholders})",
                tuple(ids),
            )
            n = cur.rowcount if cur.rowcount is not None else 0
            deleted[table] = n
            print(f"  DELETE {table:30s} ({col}) → {n} rows")

        print("Executing deletes (reverse FK order):")

        # Leaf-most: interaction children
        delete_in("interaction_rubric_scores", "interaction_id", interaction_ids)
        delete_in("clarifying_questions",      "interaction_id", interaction_ids)
        # performance_reports: scoped by subject_user_id (no company FK)
        delete_in("performance_reports",       "subject_user_id", user_ids)
        # api_call_log references interactions + company; company CASCADE will catch
        # the rest, but unref interaction_ids first for cleanliness.
        delete_in("api_call_log",              "interaction_id", interaction_ids)
        # voip_call_queue references interactions — null out first via company cascade later
        delete_in("voip_call_queue",           "voip_queue_interaction_id", interaction_ids)

        # interactions → projects (SET NULL on project_id), but we delete them outright
        delete_in("interactions",              "interaction_id",  interaction_ids)

        # Projects (RESTRICT from companies)
        delete_in("projects",                  "project_id",      proj_ids)

        # Rubric items (CASCADE from rubric_groups anyway, but explicit is fine)
        delete_in("rubric_items",              "rubric_item_id",  ri_ids)
        # Rubric groups (RESTRICT from locations)
        delete_in("rubric_groups",             "rubric_group_id", rg_ids)

        # Campaigns (CASCADE from locations anyway)
        delete_in("campaigns",                 "location_id",     loc_ids)

        # Locations (RESTRICT from companies)
        delete_in("locations",                 "location_id",     loc_ids)

        # audit_log: actor_user_id SET NULL on user delete, but we want to
        # remove records referencing our test users entirely to keep the table
        # from being polluted with dangling NULL-actor rows.
        delete_in("audit_log",                 "actor_user_id",   user_ids)

        # Users BEFORE departments (dept SET NULL on users would orphan them)
        delete_in("users",                     "user_id",         user_ids)

        # Companies — CASCADE removes company_settings, company_labels,
        # voip_configs, voip_call_queue, api_keys, api_usage, api_call_log,
        # departments.
        delete_in("companies",                 "company_id",      list(co_ids))

        conn.commit()
        print()
        print("Commit OK.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Verification pass
    conn = db.get_conn()
    try:
        remaining = fetchall(
            conn,
            f"SELECT COUNT(*) AS c FROM companies WHERE {TEST_NAME_CLAUSE}",
        )[0]["c"]
        # Note: the DB-API sees `%` as a parameter placeholder even inside a
        # quoted string literal. Use `%%` when the SQL is passed to
        # psycopg2's parameter-substitution path.
        leftover_users = fetchall(
            conn,
            "SELECT COUNT(*) AS c FROM users WHERE user_email LIKE '%%+%%@test.com'",
        )[0]["c"]
        orphan_users = fetchall(
            conn,
            "SELECT COUNT(*) AS c FROM users WHERE department_id IS NULL",
        )[0]["c"]
        remaining_cos = fetchall(
            conn,
            "SELECT company_id, company_name FROM companies ORDER BY company_id",
        )
        print()
        print(f"Verification:")
        print(f"  test-named companies remaining: {remaining}")
        print(f"  users with @test.com emails:    {leftover_users}")
        print(f"  users with NULL department_id:  {orphan_users}")
        print(f"  companies still in DB:          {len(remaining_cos)}")
        for r in remaining_cos:
            print(f"    {r['company_id']:3d}  {r['company_name']}")
        ok = (remaining == 0 and leftover_users == 0)
        print()
        print("CLEAN ✓" if ok else "LEFTOVERS ✗")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
