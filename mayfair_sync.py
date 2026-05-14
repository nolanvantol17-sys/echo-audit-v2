"""
mayfair_sync.py — Pull property + RM identity from Mayfair's Property
Directory API and stamp the local locations / users tables.

One-pass design: every active location in the target company gets one API
call. On match we set locations.mayfair_property_id + mayfair_rm_user_id;
on the same pass we build an email→mayfair_user_id map for RMs and stamp
the matching users.mayfair_user_id rows.

This module is sync-only. The downstream permission-filter SQL helper
(helpers.location_scope_for_user) ships in a separate commit and reads
these columns; it doesn't need anything from mayfair_sync at request time.

Failure modes:
  - API down: the run completes with status='failed'. Already-populated
    rows from prior runs stay valid; reads never call Mayfair live.
  - 404 on a name (no fuzzy match): logged in msr_unmatched, sync continues.
  - Transport error mid-run: that one location is skipped, status flips
    to 'partial' at end-of-run.
"""

import json
import logging
import time
from typing import Optional

from db import get_conn, q, IS_POSTGRES
from mayfairnet_client import get_property_managers, MayfairnetError

logger = logging.getLogger(__name__)


def run_sync(company_id: int,
             triggered_by_user_id: Optional[int] = None) -> dict:
    """Sync property + RM identity for one company. Returns a summary dict.

    Idempotent — running twice with no upstream changes leaves the DB
    untouched. Safe to call from any thread (creates its own connection).
    """
    started = time.time()
    summary = {
        "company_id":       company_id,
        "status":           "running",
        "locations_total":  0,
        "locations_matched": 0,
        "users_linked":     0,
        "unmatched":        [],
        "error":            None,
        "run_id":           None,
        "elapsed_seconds":  0,
    }

    conn = get_conn()
    run_id = None
    try:
        # 1. Open a mayfair_sync_runs row so the platform page can show
        #    "running…" the moment the button is clicked.
        run_id = _open_run_row(conn, company_id, triggered_by_user_id)
        summary["run_id"] = run_id

        # 2. Pull every live location for this company.
        locations = _live_locations(conn, company_id)
        summary["locations_total"] = len(locations)

        # 3. Walk each location, hit the API, stamp the row, accumulate
        #    a mapping of {rm_email_lower → rm_mayfair_user_id} so step 4
        #    can link users.mayfair_user_id without a second pass.
        rm_email_to_id = {}
        for loc_id, loc_name in locations:
            try:
                match = get_property_managers(loc_name)
            except MayfairnetError as exc:
                logger.warning(
                    "[mayfair_sync] api error loc=%s err=%s", loc_name, exc,
                )
                summary["unmatched"].append({
                    "location_id": loc_id, "location_name": loc_name,
                    "reason":      f"api_error: {exc}",
                })
                continue

            if not match:
                summary["unmatched"].append({
                    "location_id": loc_id, "location_name": loc_name,
                    "reason":      "no_match",
                })
                continue

            prop_id   = match.get("PropertyId")
            rm_id     = match.get("RMUserId")
            rm_email  = (match.get("RMEmail") or "").strip().lower()
            if rm_id and rm_email:
                rm_email_to_id.setdefault(rm_email, rm_id)

            _stamp_location(conn, loc_id, prop_id, rm_id)
            summary["locations_matched"] += 1

        # 4. Link Echo Audit users to their Mayfair UserId via email match.
        #    Only fires when an email collision actually exists, so a 17-RM
        #    mapping with zero Echo Audit accounts is a no-op (current state).
        if rm_email_to_id:
            summary["users_linked"] = _link_users(conn, rm_email_to_id)

        # 5. Mark the run done. 'partial' when some locations had problems.
        status = "ok" if not summary["unmatched"] else "partial"
        if summary["locations_matched"] == 0 and summary["locations_total"] > 0:
            status = "failed"
        summary["status"] = status
        _close_run_row(conn, run_id, status, summary)

        conn.commit()
    except Exception as exc:
        # Catch-all — never leave the run row in 'running' state.
        logger.exception("[mayfair_sync] unexpected failure")
        summary["status"] = "failed"
        summary["error"] = str(exc)
        try:
            conn.rollback()
            if run_id is not None:
                conn2 = get_conn()
                try:
                    _close_run_row(conn2, run_id, "failed", summary)
                    conn2.commit()
                finally:
                    conn2.close()
        except Exception:
            logger.exception("[mayfair_sync] also failed to close run row")
        raise
    finally:
        conn.close()
        summary["elapsed_seconds"] = round(time.time() - started, 1)
    return summary


# ── DB helpers ────────────────────────────────────────────────


def _live_locations(conn, company_id):
    cur = conn.execute(
        q("""SELECT location_id, location_name
               FROM locations
              WHERE company_id = ?
                AND location_deleted_at IS NULL
              ORDER BY location_name"""),
        (company_id,),
    )
    out = []
    for row in cur.fetchall():
        try:
            out.append((row["location_id"], row["location_name"]))
        except (KeyError, TypeError, IndexError):
            out.append((row[0], row[1]))
    return out


def _stamp_location(conn, location_id, prop_id, rm_user_id):
    conn.execute(
        q("""UPDATE locations
                SET mayfair_property_id        = ?,
                    mayfair_rm_user_id         = ?,
                    locations_mayfair_synced_at = NOW()
              WHERE location_id = ?"""),
        (prop_id, rm_user_id, location_id),
    )


def _link_users(conn, rm_email_to_id) -> int:
    """For each rm_email_to_id entry, set users.mayfair_user_id when an
    Echo Audit user shares the email (case-insensitive). Returns the count
    of rows actually changed (skips rows already at the correct value)."""
    if not rm_email_to_id:
        return 0

    # Pull all candidate Echo Audit users in one query, then update only
    # those that changed. Avoids a per-email UPDATE roundtrip.
    cur = conn.execute(
        q("""SELECT user_id, LOWER(user_email) AS email_lower, mayfair_user_id
               FROM users
              WHERE user_deleted_at IS NULL
                AND user_email IS NOT NULL"""),
        (),
    )
    linked = 0
    for row in cur.fetchall():
        try:
            uid    = row["user_id"]
            email  = row["email_lower"]
            curval = row["mayfair_user_id"]
        except (KeyError, TypeError, IndexError):
            uid, email, curval = row[0], row[1], row[2]
        new_val = rm_email_to_id.get(email)
        if new_val is not None and curval != new_val:
            conn.execute(
                q("UPDATE users SET mayfair_user_id = ? WHERE user_id = ?"),
                (new_val, uid),
            )
            linked += 1
    return linked


def _open_run_row(conn, company_id, triggered_by_user_id) -> int:
    if IS_POSTGRES:
        cur = conn.execute(
            """INSERT INTO mayfair_sync_runs (company_id, msr_triggered_by_user_id)
               VALUES (%s, %s)
               RETURNING mayfair_sync_run_id""",
            (company_id, triggered_by_user_id),
        )
        return cur.fetchone()["mayfair_sync_run_id"]
    # SQLite fallback for local-dev parity. We don't run sync on SQLite in
    # practice (no Mayfair tenant), but the column exists either way.
    conn.execute(
        "INSERT INTO mayfair_sync_runs (company_id, msr_triggered_by_user_id) "
        "VALUES (?, ?)",
        (company_id, triggered_by_user_id),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _close_run_row(conn, run_id, status, summary):
    unmatched_json = json.dumps(summary.get("unmatched") or [])
    conn.execute(
        q("""UPDATE mayfair_sync_runs
                SET msr_status            = ?,
                    msr_completed_at      = NOW(),
                    msr_locations_total   = ?,
                    msr_locations_matched = ?,
                    msr_users_linked      = ?,
                    msr_unmatched         = ?,
                    msr_error             = ?
              WHERE mayfair_sync_run_id   = ?"""),
        (status,
         summary.get("locations_total", 0),
         summary.get("locations_matched", 0),
         summary.get("users_linked", 0),
         unmatched_json,
         summary.get("error"),
         run_id),
    )


def get_last_run(company_id: int) -> Optional[dict]:
    """Latest run row for the company — used by the platform admin page to
    show 'last sync: X minutes ago'."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT mayfair_sync_run_id, msr_started_at, msr_completed_at,
                        msr_status, msr_locations_total, msr_locations_matched,
                        msr_users_linked, msr_unmatched, msr_error
                   FROM mayfair_sync_runs
                  WHERE company_id = ?
                  ORDER BY msr_started_at DESC
                  LIMIT 1"""),
            (company_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        try:
            out = dict(row)
        except Exception:
            out = {k: row[k] for k in row.keys()}
        # Postgres JSONB comes back as a list/dict already; SQLite would give us
        # a string. Normalize for the JSON response.
        um = out.get("msr_unmatched")
        if isinstance(um, str):
            try:
                out["msr_unmatched"] = json.loads(um)
            except Exception:
                out["msr_unmatched"] = []
        # Coerce timestamps to ISO strings for cross-driver consistency
        for k in ("msr_started_at", "msr_completed_at"):
            v = out.get(k)
            if v is not None and hasattr(v, "isoformat"):
                out[k] = v.isoformat()
        return out
    finally:
        conn.close()
