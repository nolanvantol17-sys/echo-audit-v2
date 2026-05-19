"""
mayfair_sync.py — reconcile Echo Audit's locations / users against the
Mayfair MPL bulk feed ("Exile Island"), update-in-place by stable ID.

This replaces the OLD fuzzy /api/properties/managers path (one
name-search per location, Levenshtein-guessed, needed a manual re-link
UI). The new design pulls two full bulk dumps and keys every write on a
stable ID — never a name:

  - Properties  → matched to `locations` by `location_yardi_code`
                  (= feed `YardiCode`).
  - Users       → matched to `users`     by `mayfair_user_id`
                  (= feed user `ID`).

Locked constraints (do not violate — see EXILE_ISLAND_SYNC_DESIGN.md):
  1. Never wipe. Update-in-place only. A row the feed stopped mentioning
     is status-flagged (`*_inactive_since`), NEVER deleted.
  2. Match on stable ID, never name.
  3. `users.user_email` IS the SSO login key — NEVER overwritten from the
     feed. Mismatches are logged for human review, never applied.
  4. User inactivation is scoped to feed-origin users only
     (`mayfair_user_id IS NOT NULL`). Echo-Audit-native accounts (the AI
     Caller bot, super-admins) have NULL mayfair_user_id and are never
     touched. A belt-and-suspenders invariant double-checks this.
  5. Properties absent from the feed are auto-created (Q3). Users are
     NOT auto-created in v1 — account creation is the deferred
     provisioning/permission workstream. Unmatched feed users are
     reported in the run plan, never inserted.

dry_run (default True): runs the entire reconciliation, records exactly
what it WOULD do (per-row plan + counts), then rolls back without
mutating either table. A dry_run=False run additionally snapshots both
tables, verifies invariants, and commits only if every invariant passes.

Failure modes:
  - Feed unreachable / non-200 / empty / malformed: MPLFeedError →
    status='failed', zero writes, run row closed. A bad pull never wipes.
  - Invariant failure on a real run: full rollback, status='failed'.
  - The dormant fuzzy path (_stamp_location, get_last_run) is preserved
    for the platform-admin re-link UI during the one-cycle handover.
"""

import json
import logging
import time
from typing import Optional

from db import get_conn, q, IS_POSTGRES
from mpl_feed_client import (
    fetch_property_directory,
    fetch_active_users,
    MPLFeedError,
)

logger = logging.getLogger(__name__)

# Feed role bucket → locations column. Stored raw (CSV as delivered);
# NO permission logic is wired — that is the deferred workstream.
_ROLE_BUCKET_COLUMNS = {
    "PropertyManagerUserIds":      "location_pm_user_ids",
    "RegionalMaintenanceUserIds":  "location_rm_user_ids",
    "ComplianceUserIds":           "location_compliance_user_ids",
    "OnsiteUserIds":               "location_onsite_user_ids",
    "AllAssignedUserIds":          "location_all_assigned_user_ids",
}


# ── small value helpers ───────────────────────────────────────


def _s(v) -> Optional[str]:
    """Feed value → trimmed string, or None for null/blank. Never returns
    '' (would violate NOT NULL / pollute the UNIQUE SSO email column)."""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _raw_bucket(v) -> Optional[str]:
    """Role bucket as delivered, coerced to TEXT. The feed sends a CSV
    string (or occasionally a list); store it raw, never parse it."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v) or None
    return str(v).strip() or None


# ── main entry point ──────────────────────────────────────────


def run_sync(company_id: int,
             triggered_by_user_id: Optional[int] = None,
             dry_run: bool = True) -> dict:
    """Reconcile one company's properties + the Mayfair user directory
    against the MPL feed. Returns a summary dict.

    dry_run=True (default): compute the full plan, write nothing, roll
    back. dry_run=False: snapshot both tables, apply, verify invariants,
    commit only on all-pass.

    Idempotent: a second run with no upstream change is a no-op (writes
    only on an actual value change; inactivates only a currently-active
    row; reactivates only a currently-inactive one). Safe from any
    thread (own connection).
    """
    started = time.time()
    summary = {
        "company_id":        company_id,
        "dry_run":           dry_run,
        "status":            "running",
        "locations_total":   0,   # feed property count (headline)
        "locations_matched": 0,   # feed props matched to a location
        "users_linked":      0,   # feed-origin users updated
        "counts": {
            "properties_updated":     0,
            "properties_created":     0,
            "properties_inactivated": 0,
            "properties_reactivated": 0,
            "properties_skipped_no_yardi": 0,
            "users_updated":          0,
            "users_inactivated":      0,
            "users_reactivated":      0,
            "users_unmatched_feed":   0,
            "users_email_mismatch":   0,
        },
        "plan":     {"properties": {}, "users": {}},
        "unmatched": [],          # kept a list for legacy-render safety
        "error":     None,
        "run_id":    None,
        "elapsed_seconds": 0,
    }

    conn = get_conn()
    run_id = None
    try:
        run_id = _open_run_row(conn, company_id, triggered_by_user_id)
        summary["run_id"] = run_id
        conn.commit()  # make the 'running' row visible immediately

        # 1. Pull both feeds FIRST. Any failure here = abort, touch
        #    nothing. An empty/malformed dump must never reach a write.
        try:
            feed_props = fetch_property_directory()
            feed_users = fetch_active_users()
        except MPLFeedError as exc:
            logger.warning("[mayfair_sync] feed pull failed: %s", exc)
            summary["status"] = "failed"
            summary["error"] = f"feed unavailable: {exc}"
            _close_run_row(conn, run_id, "failed", summary)
            conn.commit()
            return summary

        summary["locations_total"] = len(feed_props)

        # 2. Snapshot BEFORE any mutation (real runs only — a dry_run
        #    rolls back so it needs no backup). Snapshot lives inside the
        #    same transaction: commit keeps it, rollback drops it with
        #    everything else (consistent either way).
        backup_tag = None
        if not dry_run:
            backup_tag = time.strftime("%Y%m%d_%H%M%S")
            conn.execute(
                f"CREATE TABLE backup_locations_exile_sync_{backup_tag} "
                f"AS SELECT * FROM locations"
            )
            conn.execute(
                f"CREATE TABLE backup_users_exile_sync_{backup_tag} "
                f"AS SELECT * FROM users"
            )
            summary["backup_tag"] = backup_tag

        # Row counts before mutation — invariant baseline.
        loc_count_before = _scalar(conn, "SELECT COUNT(*) FROM locations")
        usr_count_before = _scalar(conn, "SELECT COUNT(*) FROM users")

        # 3. Users pass (company-agnostic: the feed is Mayfair-wide, and
        #    users carry no company_id; feed-origin is the only scope).
        _sync_users(conn, feed_users, summary)

        # 4. Properties pass (scoped to this company's locations).
        _sync_properties(conn, company_id, feed_props, summary)

        # 5. Invariant gate (real runs). Any failure → rollback, fail.
        if not dry_run:
            ok, why = _verify_invariants(
                conn, company_id, loc_count_before, usr_count_before,
                summary, backup_tag,
            )
            if not ok:
                conn.rollback()  # discard sync writes; run row is durable
                logger.error("[mayfair_sync] invariant failed: %s", why)
                summary["status"] = "failed"
                summary["error"] = f"invariant failed (rolled back): {why}"
                _close_run_row(conn, run_id, "failed", summary)
                conn.commit()
                return summary

        # 6. Decide status + finalize.
        status = "ok"
        if (summary["counts"]["properties_skipped_no_yardi"]
                or summary["counts"]["users_email_mismatch"]
                or summary["counts"]["users_unmatched_feed"]):
            status = "partial"  # completed, but items need a human look
        summary["status"] = status

        if dry_run:
            conn.rollback()                       # sync writes discarded
        # run row was committed up front, so a plain UPDATE + commit
        # finalizes it whether or not we just rolled back the sync.
        _close_run_row(conn, run_id, status, summary)
        conn.commit()

    except Exception as exc:
        logger.exception("[mayfair_sync] unexpected failure")
        summary["status"] = "failed"
        summary["error"] = str(exc)
        try:
            conn.rollback()
        except Exception:
            pass
        if run_id is not None:
            try:
                _close_run_row_fresh(run_id, "failed", summary)
            except Exception:
                logger.exception("[mayfair_sync] also failed to close run row")
        raise
    # NOTE: feed-pull-failure path returns early after committing the run
    # row; no rollback needed there since nothing was written.
    finally:
        conn.close()
        summary["elapsed_seconds"] = round(time.time() - started, 1)
    return summary


# ── users pass ────────────────────────────────────────────────


def _sync_users(conn, feed_users, summary) -> None:
    """Update feed-origin users in place by mayfair_user_id; inactivate
    feed-origin users absent from the feed; reactivate ones that
    reappear. NEVER writes user_email. NEVER inserts (no auto-create).
    """
    plan = {"update": [], "inactivate": [], "reactivate": [],
            "unmatched_feed": [], "email_mismatch": []}

    # Feed users keyed by stable ID (skip rows with no/!int ID — can't key).
    feed_by_id = {}
    for fu in feed_users:
        fid = fu.get("ID")
        try:
            fid = int(fid)
        except (TypeError, ValueError):
            continue
        feed_by_id[fid] = fu

    # All live Echo Audit users + role name (for the safety invariant).
    rows = conn.execute(q(
        """SELECT u.user_id, u.mayfair_user_id, u.user_email,
                  u.user_first_name, u.user_last_name,
                  u.user_inactive_since, r.role_name
             FROM users u
             LEFT JOIN user_roles ur ON u.user_role_id = ur.user_role_id
             LEFT JOIN roles r       ON ur.role_id      = r.role_id
            WHERE u.user_deleted_at IS NULL""")
    ).fetchall()

    seen_feed_ids = set()
    for row in rows:
        d = _row(row)
        mid = d["mayfair_user_id"]
        if mid is None:
            continue  # Echo-Audit-native (AI Caller bot, super-admins): skip
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            continue
        fu = feed_by_id.get(mid)

        if fu is not None:
            seen_feed_ids.add(mid)

            # Reactivate if the feed lists a previously-inactivated user.
            if d["user_inactive_since"] is not None:
                conn.execute(
                    q("UPDATE users SET user_inactive_since = NULL, "
                      "user_updated_at = NOW() WHERE user_id = ?"),
                    (d["user_id"],),
                )
                summary["counts"]["users_reactivated"] += 1
                plan["reactivate"].append(
                    {"user_id": d["user_id"], "mayfair_user_id": mid})

            # Names: NOT NULL columns — only write a present feed value
            # that actually differs. Never blank an existing name.
            new_first = _s(fu.get("FirstName")) or d["user_first_name"]
            new_last  = _s(fu.get("LastName"))  or d["user_last_name"]
            if (new_first != d["user_first_name"]
                    or new_last != d["user_last_name"]):
                conn.execute(
                    q("UPDATE users SET user_first_name = ?, "
                      "user_last_name = ?, user_updated_at = NOW() "
                      "WHERE user_id = ?"),
                    (new_first, new_last, d["user_id"]),
                )
                summary["counts"]["users_updated"] += 1
                summary["users_linked"] += 1
                plan["update"].append({
                    "user_id": d["user_id"], "mayfair_user_id": mid,
                    "from": f'{d["user_first_name"]} {d["user_last_name"]}',
                    "to":   f"{new_first} {new_last}",
                })

            # Email: SSO key. Compare ONLY — never write. Log mismatch.
            feed_email = _s(fu.get("Email"))
            if (feed_email
                    and d["user_email"]
                    and feed_email.lower() != d["user_email"].lower()):
                summary["counts"]["users_email_mismatch"] += 1
                plan["email_mismatch"].append({
                    "user_id": d["user_id"], "mayfair_user_id": mid,
                    "login_email": d["user_email"],
                    "feed_email":  feed_email,
                })
        else:
            # Feed-origin user no longer in the feed → inactivate (never
            # delete). Idempotent. Belt-and-suspenders: a super_admin or
            # the AI Caller bot must never reach here (they have NULL
            # mayfair_user_id), but refuse explicitly if one ever does.
            if _is_protected(d):
                logger.error(
                    "[mayfair_sync] REFUSED to inactivate protected user "
                    "user_id=%s role=%s — has a mayfair_user_id it should "
                    "not. Investigate.", d["user_id"], d["role_name"],
                )
                continue
            if d["user_inactive_since"] is None:
                conn.execute(
                    q("UPDATE users SET user_inactive_since = NOW(), "
                      "user_updated_at = NOW() WHERE user_id = ?"),
                    (d["user_id"],),
                )
                summary["counts"]["users_inactivated"] += 1
                plan["inactivate"].append({
                    "user_id": d["user_id"], "mayfair_user_id": mid,
                    "name": f'{d["user_first_name"]} {d["user_last_name"]}',
                })

    # Feed users with no Echo Audit account — reported, NEVER created.
    for fid, fu in feed_by_id.items():
        if fid in seen_feed_ids:
            continue
        summary["counts"]["users_unmatched_feed"] += 1
        plan["unmatched_feed"].append({
            "mayfair_user_id": fid,
            "name": f'{_s(fu.get("FirstName")) or "?"} '
                    f'{_s(fu.get("LastName")) or "?"}',
        })

    summary["plan"]["users"] = plan


def _is_protected(user_row: dict) -> bool:
    """A user that must never be auto-inactivated regardless of feed
    state: super-admins and the AI Caller bot (name convention per
    followup_ai_caller_user_convention). This is a hard refusal, not a
    filter — it should be unreachable (these have NULL mayfair_user_id),
    but blanket inactivation killing the AI Caller bot is the headline
    correctness landmine, so we guard it twice."""
    if (user_row.get("role_name") or "").strip().lower() == "super_admin":
        return True
    first = (user_row.get("user_first_name") or "").strip().lower()
    last  = (user_row.get("user_last_name") or "").strip().lower()
    return first == "ai" and last == "caller"


# ── properties pass ───────────────────────────────────────────


def _sync_properties(conn, company_id, feed_props, summary) -> None:
    """Match feed properties to this company's locations by YardiCode;
    update in place. Unmatched feed property → auto-create (active).
    Feed-origin location absent from the feed → inactivate (never
    delete). Locations with no YardiCode are not feed-origin: left
    untouched, listed in the plan for a human look."""
    plan = {"update": [], "create": [], "inactivate": [],
            "reactivate": [], "skipped_no_yardi": []}

    rows = conn.execute(q(
        """SELECT location_id, location_yardi_code, location_name,
                  location_phone, mayfair_property_id, location_inactive_since
             FROM locations
            WHERE company_id = ?
              AND location_deleted_at IS NULL"""),
        (company_id,),
    ).fetchall()

    loc_by_yardi = {}
    for row in rows:
        d = _row(row)
        yc = _s(d["location_yardi_code"])
        if yc is None:
            summary["counts"]["properties_skipped_no_yardi"] += 1
            plan["skipped_no_yardi"].append({
                "location_id": d["location_id"],
                "location_name": d["location_name"],
                "reason": "no location_yardi_code — not feed-origin, "
                          "left untouched (needs human review)",
            })
            continue
        loc_by_yardi[yc] = d

    seen_yardi = set()
    for fp in feed_props:
        yc = _s(fp.get("YardiCode"))
        if yc is None:
            continue  # feed integrity issue; YardiCode is the only key
        seen_yardi.add(yc)

        name  = _s(fp.get("LongName")) or _s(fp.get("ShortName")) \
            or f"Property {yc}"
        phone = _s(fp.get("PhoneNumber"))
        try:
            prop_id = int(fp.get("PropertyId"))
        except (TypeError, ValueError):
            prop_id = None
        buckets = {col: _raw_bucket(fp.get(feed_key))
                   for feed_key, col in _ROLE_BUCKET_COLUMNS.items()}

        existing = loc_by_yardi.get(yc)
        if existing is None:
            # AUTO-CREATE (Q3): new feed property, active, this company.
            loc_id = _insert_location(
                conn, company_id, name, phone, prop_id, yc, buckets)
            summary["counts"]["properties_created"] += 1
            plan["create"].append({
                "location_id": loc_id, "yardi_code": yc,
                "location_name": name, "mayfair_property_id": prop_id,
            })
        else:
            if existing["location_inactive_since"] is not None:
                conn.execute(
                    q("UPDATE locations SET location_inactive_since = NULL "
                      "WHERE location_id = ?"),
                    (existing["location_id"],),
                )
                summary["counts"]["properties_reactivated"] += 1
                plan["reactivate"].append({
                    "location_id": existing["location_id"], "yardi_code": yc})

            set_sql = (
                "location_name = ?, location_phone = ?, "
                "mayfair_property_id = ?, "
                + ", ".join(f"{c} = ?" for c in _ROLE_BUCKET_COLUMNS.values())
                + ", locations_mayfair_synced_at = NOW()"
            )
            params = [name, phone, prop_id]
            params += [buckets[c] for c in _ROLE_BUCKET_COLUMNS.values()]
            params.append(existing["location_id"])
            conn.execute(
                q(f"UPDATE locations SET {set_sql} WHERE location_id = ?"),
                params,
            )
            summary["counts"]["properties_updated"] += 1
            summary["locations_matched"] += 1
            if (name != existing["location_name"]
                    or phone != existing["location_phone"]):
                plan["update"].append({
                    "location_id": existing["location_id"], "yardi_code": yc,
                    "name_from": existing["location_name"], "name_to": name,
                })

    # Feed-origin locations (have a YardiCode) absent from today's feed →
    # inactivate. Never delete. Idempotent.
    for yc, d in loc_by_yardi.items():
        if yc in seen_yardi:
            continue
        if d["location_inactive_since"] is None:
            conn.execute(
                q("UPDATE locations SET location_inactive_since = NOW() "
                  "WHERE location_id = ?"),
                (d["location_id"],),
            )
            summary["counts"]["properties_inactivated"] += 1
            plan["inactivate"].append({
                "location_id": d["location_id"], "yardi_code": yc,
                "location_name": d["location_name"],
            })

    summary["plan"]["properties"] = plan


def _insert_location(conn, company_id, name, phone, prop_id, yardi, buckets):
    bucket_cols = list(_ROLE_BUCKET_COLUMNS.values())
    cols = (["company_id", "location_name", "location_phone",
             "mayfair_property_id", "location_yardi_code"]
            + bucket_cols + ["locations_mayfair_synced_at"])
    placeholders = ", ".join(["?"] * (len(cols) - 1) + ["NOW()"])
    vals = ([company_id, name, phone, prop_id, yardi]
            + [buckets[c] for c in bucket_cols])
    sql = (f"INSERT INTO locations ({', '.join(cols)}) "
           f"VALUES ({placeholders})")
    if IS_POSTGRES:
        cur = conn.execute(q(sql + " RETURNING location_id"), vals)
        return cur.fetchone()["location_id"]
    conn.execute(q(sql), vals)
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ── invariant gate (real runs only) ───────────────────────────


def _verify_invariants(conn, company_id, loc_before, usr_before,
                        summary, backup_tag):
    """Return (ok, reason). Any False → caller rolls back the whole run.
    These are the can't-ship-without-them guards, not nice-to-haves."""
    c = summary["counts"]

    # No users ever created or deleted (v1 has no user auto-create).
    usr_after = _scalar(conn, "SELECT COUNT(*) FROM users")
    if usr_after != usr_before:
        return False, (f"users rowcount changed {usr_before}->{usr_after} "
                       f"(sync must never insert/delete users)")

    # locations changes by exactly the auto-created count, no deletions.
    loc_after = _scalar(conn, "SELECT COUNT(*) FROM locations")
    if loc_after != loc_before + c["properties_created"]:
        return False, (f"locations rowcount {loc_before}->{loc_after} != "
                       f"before + created({c['properties_created']})")

    # SSO email is sacred: not one user_email may differ from the
    # pre-mutation snapshot.
    changed = _scalar(conn, q(
        f"""SELECT COUNT(*) FROM users u
              JOIN backup_users_exile_sync_{backup_tag} b
                ON u.user_id = b.user_id
             WHERE u.user_email IS DISTINCT FROM b.user_email"""))
    if changed:
        return False, f"{changed} user_email value(s) changed — SSO key"

    # No protected account (super_admin / AI Caller bot) got newly
    # inactivated by this run.
    bad = _scalar(conn, q(
        f"""SELECT COUNT(*) FROM users u
              JOIN backup_users_exile_sync_{backup_tag} b
                ON u.user_id = b.user_id
              LEFT JOIN user_roles ur ON u.user_role_id = ur.user_role_id
              LEFT JOIN roles r       ON ur.role_id      = r.role_id
             WHERE b.user_inactive_since IS NULL
               AND u.user_inactive_since IS NOT NULL
               AND ( LOWER(COALESCE(r.role_name,'')) = 'super_admin'
                  OR ( LOWER(TRIM(COALESCE(u.user_first_name,''))) = 'ai'
                   AND LOWER(TRIM(COALESCE(u.user_last_name,'')))  = 'caller')
                  OR u.mayfair_user_id IS NULL ) """))
    if bad:
        return False, (f"{bad} protected/native user(s) were inactivated "
                        f"(AI Caller bot / super-admin guard tripped)")

    # Rows that exist now but were NOT in the pre-mutation snapshot are
    # exactly the ones THIS run auto-created. Scope every new-location
    # check to those — never to locations the old fuzzy sync stamped or
    # to other companies' rows (that broadness would false-fail).
    new_ids_sql = (
        f"""SELECT l.location_id, l.location_yardi_code, l.company_id
              FROM locations l
              LEFT JOIN backup_locations_exile_sync_{backup_tag} b
                ON l.location_id = b.location_id
             WHERE b.location_id IS NULL""")
    new_rows = conn.execute(new_ids_sql).fetchall()

    if len(new_rows) != c["properties_created"]:
        return False, (f"{len(new_rows)} new location row(s) but "
                       f"properties_created={c['properties_created']}")
    for r in new_rows:
        d = _row(r)
        if d["location_yardi_code"] is None or d["company_id"] != company_id:
            return False, (f"auto-created location {d['location_id']} "
                            f"missing YardiCode or wrong company")

    return True, "ok"


# ── run-row lifecycle + dormant re-link helper (preserved) ────


def _stamp_location(conn, location_id, prop_id, rm_user_id):
    """Preserved for the dormant platform-admin manual re-link UI
    (/mayfair/link) during the one-cycle handover. Not used by the new
    sync. Do not remove until the fuzzy path is formally retired."""
    conn.execute(
        q("""UPDATE locations
                SET mayfair_property_id        = ?,
                    mayfair_rm_user_id         = ?,
                    locations_mayfair_synced_at = NOW()
              WHERE location_id = ?"""),
        (prop_id, rm_user_id, location_id),
    )


def _open_run_row(conn, company_id, triggered_by_user_id) -> int:
    if IS_POSTGRES:
        cur = conn.execute(
            """INSERT INTO mayfair_sync_runs (company_id, msr_triggered_by_user_id)
               VALUES (%s, %s)
               RETURNING mayfair_sync_run_id""",
            (company_id, triggered_by_user_id),
        )
        return cur.fetchone()["mayfair_sync_run_id"]
    conn.execute(
        "INSERT INTO mayfair_sync_runs (company_id, msr_triggered_by_user_id) "
        "VALUES (?, ?)",
        (company_id, triggered_by_user_id),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _close_run_row(conn, run_id, status, summary):
    """Persist the run. The rich step-4 result (plan + counts +
    dry_run flag) rides in the existing msr_unmatched JSONB column so
    step 4 needs zero schema migration."""
    detail = {
        "dry_run":  summary.get("dry_run", False),
        "counts":   summary.get("counts", {}),
        "plan":     summary.get("plan", {}),
        "backup_tag": summary.get("backup_tag"),
    }
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
         json.dumps(detail),
         summary.get("error"),
         run_id),
    )


def _close_run_row_fresh(run_id, status, summary):
    """Close an EXISTING run row on a brand-new connection + commit.
    Used only by the catch-all exception path, where the working
    connection may be in an aborted-transaction state. The run row was
    committed up front, so this is a plain UPDATE — never a re-insert
    (re-inserting would orphan the original row at 'running')."""
    conn = get_conn()
    try:
        _close_run_row(conn, run_id, status, summary)
        conn.commit()
    finally:
        conn.close()


def get_last_run(company_id: int) -> Optional[dict]:
    """Latest run row for the company — drives the platform-admin
    'last sync' panel. msr_unmatched now carries the rich step-4 detail
    dict; legacy rows (old fuzzy sync) carry a list — both are returned
    as-is and the caller/UI tolerates either shape."""
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
        um = out.get("msr_unmatched")
        if isinstance(um, str):
            try:
                out["msr_unmatched"] = json.loads(um)
            except Exception:
                out["msr_unmatched"] = []
        for k in ("msr_started_at", "msr_completed_at"):
            v = out.get(k)
            if v is not None and hasattr(v, "isoformat"):
                out[k] = v.isoformat()
        return out
    finally:
        conn.close()


# ── tiny db helpers ───────────────────────────────────────────


def _row(row) -> dict:
    """psycopg dict-row or tuple → dict. We always SELECT explicit
    columns so the dict path is the norm; tuple is the SQLite fallback."""
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {i: row[i] for i in range(len(row))}


def _scalar(conn, sql, params=()):
    # Callers pass final SQL (already q()-wrapped when it has placeholders);
    # _scalar never re-wraps, to avoid double ?→%s translation.
    cur = conn.execute(sql, params)
    r = cur.fetchone()
    try:
        return list(r.values())[0] if hasattr(r, "values") else r[0]
    except Exception:
        return r[0]
