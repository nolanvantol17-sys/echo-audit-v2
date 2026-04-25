"""One-shot migration: hard-delete every soft-deleted interaction.

Background
----------
Until 2026-04-25, the per-row "Remove" button on Past Grades + interaction
detail wrote `interaction_deleted_at = NOW()` (soft delete). That endpoint
was removed in commit 72bd4a2 — every Remove now goes through the
strong-confirm hard-delete flow. This script purges the backlog of rows
left in the soft-deleted state so nothing dangles in the schema with
non-NULL `interaction_deleted_at`.

What this does (per row, mirroring interactions_routes.hard_delete_interaction)
-----------------------------------------------------------------------------
  1. INSERT interaction_deletions receipt
       deleted_by_user_id = NULL  (backlog has no surviving audit trail
                                   for the original soft-deleter; NULL is
                                   the honest signal "from migration")
  2. DELETE FROM audit_log entries targeting this interaction
       (al_target_entity_type_id=ENTITY_INTERACTION,
        al_target_entity_id=str(interaction_id))
  3. DELETE FROM interactions
       (cascades interaction_rubric_scores, clarifying_questions,
        interaction_audio_data, etc. via FK ON DELETE CASCADE)
  4. Per-row commit (idempotent — partial run can resume safely)

Modes
-----
  --dry-run (default)  : show the rows + cascade counts, change nothing
  --apply              : run the deletes; report succeeded/failed; verify
                         post-state invariants

Run via
-------
  railway run python3 migrate_purge_soft_deleted_interactions.py --dry-run
  railway run python3 migrate_purge_soft_deleted_interactions.py --apply
"""

import argparse
import logging
import sys

from audit_log import ENTITY_INTERACTION
from db import IS_POSTGRES, get_conn, q

logger = logging.getLogger("migrate_purge_soft_deleted")


# ── Helpers ─────────────────────────────────────────────────────


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _fmt_bytes(n):
    if not n:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def _load_soft_deleted_rows(conn):
    """Returns the list of soft-deleted interactions with cascade counts."""
    if IS_POSTGRES:
        cur = conn.execute("""
            SELECT i.interaction_id,
                   i.project_id,
                   p.company_id        AS owning_company_id,
                   p.project_id        AS owning_project_id,
                   p.project_name,
                   i.interaction_responder_name,
                   i.interaction_overall_score,
                   i.interaction_deleted_at,
                   (i.interaction_audio_data IS NOT NULL) AS has_audio,
                   OCTET_LENGTH(COALESCE(i.interaction_audio_data, ''::bytea)) AS audio_bytes,
                   (SELECT COUNT(*) FROM interaction_rubric_scores
                     WHERE interaction_id = i.interaction_id) AS rubric_scores,
                   (SELECT COUNT(*) FROM clarifying_questions
                     WHERE interaction_id = i.interaction_id) AS clarifying_qs,
                   (SELECT COUNT(*) FROM audit_log
                     WHERE audit_log_target_entity_type_id = %s
                       AND al_target_entity_id = i.interaction_id::text) AS audit_entries
              FROM interactions i
              LEFT JOIN projects p ON p.project_id = i.project_id
             WHERE i.interaction_deleted_at IS NOT NULL
             ORDER BY i.interaction_deleted_at DESC
        """, (ENTITY_INTERACTION,))
    else:
        # SQLite fallback — paths the dev env hits, never production.
        cur = conn.execute("""
            SELECT i.interaction_id,
                   i.project_id,
                   p.company_id        AS owning_company_id,
                   p.project_id        AS owning_project_id,
                   p.project_name,
                   i.interaction_responder_name,
                   i.interaction_overall_score,
                   i.interaction_deleted_at,
                   (i.interaction_audio_data IS NOT NULL) AS has_audio,
                   COALESCE(LENGTH(i.interaction_audio_data), 0) AS audio_bytes,
                   (SELECT COUNT(*) FROM interaction_rubric_scores
                     WHERE interaction_id = i.interaction_id) AS rubric_scores,
                   (SELECT COUNT(*) FROM clarifying_questions
                     WHERE interaction_id = i.interaction_id) AS clarifying_qs,
                   (SELECT COUNT(*) FROM audit_log
                     WHERE audit_log_target_entity_type_id = ?
                       AND al_target_entity_id = CAST(i.interaction_id AS TEXT)) AS audit_entries
              FROM interactions i
              LEFT JOIN projects p ON p.project_id = i.project_id
             WHERE i.interaction_deleted_at IS NOT NULL
             ORDER BY i.interaction_deleted_at DESC
        """, (ENTITY_INTERACTION,))
    return [_row_to_dict(r) for r in cur.fetchall()]


def _purge_one(conn, row):
    """Mirror interactions_routes.hard_delete_interaction() per row.

    Three SQL statements inside a single transaction:
      1. INSERT interaction_deletions (deleted_by_user_id=NULL).
      2. DELETE FROM audit_log targeting this interaction.
      3. DELETE FROM interactions (FK cascades children).

    Caller commits/rollbacks; this function does not.
    """
    iid = row["interaction_id"]
    owning_company_id = row["owning_company_id"]
    owning_project_id = row["owning_project_id"]

    if owning_company_id is None:
        # company_id is NOT NULL on interaction_deletions. If the project
        # was already orphaned somehow, refuse — bail loudly.
        raise RuntimeError(
            f"interaction {iid}: owning_company_id is NULL; cannot insert receipt"
        )

    if IS_POSTGRES:
        conn.execute(
            "INSERT INTO interaction_deletions "
            "(interaction_id_was, deleted_by_user_id, company_id, project_id) "
            "VALUES (%s, NULL, %s, %s)",
            (iid, owning_company_id, owning_project_id),
        )
        conn.execute(
            "DELETE FROM audit_log "
            "WHERE audit_log_target_entity_type_id = %s "
            "  AND al_target_entity_id = %s",
            (ENTITY_INTERACTION, str(iid)),
        )
        conn.execute(
            "DELETE FROM interactions WHERE interaction_id = %s",
            (iid,),
        )
    else:
        conn.execute(
            "INSERT INTO interaction_deletions "
            "(interaction_id_was, deleted_by_user_id, company_id, project_id) "
            "VALUES (?, NULL, ?, ?)",
            (iid, owning_company_id, owning_project_id),
        )
        conn.execute(
            "DELETE FROM audit_log "
            "WHERE audit_log_target_entity_type_id = ? "
            "  AND al_target_entity_id = ?",
            (ENTITY_INTERACTION, str(iid)),
        )
        conn.execute(
            "DELETE FROM interactions WHERE interaction_id = ?",
            (iid,),
        )


def _verify_post_state(conn):
    """Post-apply invariant checks. Returns dict of {check: actual_count}.
    Each value should be 0; non-zero indicates a problem worth flagging."""
    checks = {}

    # 1. Zero soft-deleted rows remain.
    cur = conn.execute(q(
        "SELECT COUNT(*) AS n FROM interactions WHERE interaction_deleted_at IS NOT NULL"
    ))
    checks["soft_deleted_remaining"] = int(_row_to_dict(cur.fetchone())["n"])

    # 2. Zero orphan rubric scores (interaction_id pointing at a missing row).
    cur = conn.execute(q("""
        SELECT COUNT(*) AS n FROM interaction_rubric_scores irs
         WHERE NOT EXISTS (
           SELECT 1 FROM interactions i WHERE i.interaction_id = irs.interaction_id
         )
    """))
    checks["orphan_rubric_scores"] = int(_row_to_dict(cur.fetchone())["n"])

    # 3. Zero orphan clarifying-questions rows.
    cur = conn.execute(q("""
        SELECT COUNT(*) AS n FROM clarifying_questions cq
         WHERE NOT EXISTS (
           SELECT 1 FROM interactions i WHERE i.interaction_id = cq.interaction_id
         )
    """))
    checks["orphan_clarifying_questions"] = int(_row_to_dict(cur.fetchone())["n"])

    return checks


# ── Main ────────────────────────────────────────────────────────


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True,
                     help="(default) show plan; change nothing")
    grp.add_argument("--apply", action="store_true",
                     help="execute the deletes")
    args = ap.parse_args()

    apply_mode = bool(args.apply)
    mode_label = "APPLY" if apply_mode else "DRY-RUN"

    print(f"=== migrate_purge_soft_deleted_interactions [{mode_label}] ===\n")

    conn = get_conn()
    try:
        rows = _load_soft_deleted_rows(conn)
        if not rows:
            print("Nothing to do — zero soft-deleted interactions found.\n")
            return 0

        # ── Pre-run summary ──
        total_audio = sum(r["audio_bytes"] or 0 for r in rows)
        total_rubrics = sum(r["rubric_scores"] for r in rows)
        total_cqs = sum(r["clarifying_qs"] for r in rows)
        total_audits = sum(r["audit_entries"] for r in rows)

        print(f"Found {len(rows)} soft-deleted interaction(s):")
        print(f"  audio bytes to free:     {_fmt_bytes(total_audio)}")
        print(f"  rubric scores to drop:   {total_rubrics}")
        print(f"  clarifying qs to drop:   {total_cqs}")
        print(f"  audit_log to sweep:      {total_audits}")
        print(f"  receipts to insert:      {len(rows)}")
        print()
        print("  iid | proj_id | project_name              | responder           | score | deleted_at         | audio   | rubrics | cqs | audits")
        print("  " + "-" * 130)
        for r in rows:
            score = f"{r['interaction_overall_score']:.2f}" if r["interaction_overall_score"] is not None else "-"
            del_at = r["interaction_deleted_at"].strftime("%Y-%m-%d %H:%M:%S") if r["interaction_deleted_at"] else "-"
            audio_label = _fmt_bytes(r["audio_bytes"] or 0) if r["has_audio"] else "—"
            print("  %4d | %7s | %-25s | %-19s | %5s | %-19s | %7s | %7s | %3s | %s" % (
                r["interaction_id"],
                r["owning_project_id"] or "-",
                (r["project_name"] or "-")[:25],
                (r["interaction_responder_name"] or "-")[:19],
                score, del_at, audio_label,
                r["rubric_scores"], r["clarifying_qs"], r["audit_entries"],
            ))
        print()

        if not apply_mode:
            print("DRY-RUN complete — no changes made.")
            print("Re-run with --apply to execute.\n")
            return 0

        # ── Apply mode: per-row transactions ──
        succeeded = []
        failed    = []
        for r in rows:
            iid = r["interaction_id"]
            try:
                _purge_one(conn, r)
                conn.commit()
                succeeded.append(iid)
                print(f"  ✓ purged interaction {iid}")
            except Exception as e:
                conn.rollback()
                failed.append((iid, str(e)))
                logger.exception(f"  ✕ FAILED interaction {iid}: {e}")

        print()
        print(f"Succeeded: {len(succeeded)}  Failed: {len(failed)}")
        if failed:
            print("\nFailures:")
            for iid, err in failed:
                print(f"  iid={iid}: {err}")

        # ── Post-apply invariant checks ──
        print("\nPost-apply invariant checks:")
        checks = _verify_post_state(conn)
        all_clean = True
        for name, actual in checks.items():
            mark = "✓" if actual == 0 else "✕"
            if actual != 0:
                all_clean = False
            print(f"  {mark} {name}: {actual}")

        if all_clean and not failed:
            print("\nAll clean. Migration complete.")
            return 0
        return 1

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
