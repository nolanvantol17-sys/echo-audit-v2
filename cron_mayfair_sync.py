"""
cron_mayfair_sync.py — unattended daily driver for the Exile Island /
MPL property+user sync. Run by a Railway cron service:

    python cron_mayfair_sync.py

It does NOT contain sync logic — it reuses the tested
`mayfair_sync.run_sync`. What it adds on top, for running with nobody
watching the screen:

  1. Dry-run FIRST. Compute the full plan without writing.
  2. Sanity gate. The invariants inside run_sync prove the change is
     *structurally* safe (no SSO-email writes, no protected-user /
     property deletes, no user auto-create). They do NOT judge whether
     the change is *reasonable*. A glitched feed that returned half its
     properties would "correctly" want to inactivate dozens — and pass
     every invariant. So if the dry-run plan exceeds safe thresholds,
     the cron HOLDS: it does not commit, it alerts a human to review +
     Apply manually from the platform page.
  3. Conditional real run. Within thresholds → run_sync(dry_run=False),
     which snapshots both tables and commits only if invariants pass.
  4. Backup retention. A daily real Apply leaves two
     backup_*_exile_sync_<ts> tables behind; prune ones older than
     MPL_CRON_BACKUP_RETENTION_DAYS so they don't accumulate forever.
  5. Alerting. Post to a Microsoft Teams incoming webhook on
     failure / hold / large-change ONLY (a clean normal run is silent
     to avoid noise). Log-only if the webhook var is unset.

Exit codes: 0 for every *handled* outcome (clean run, hold, feed
outage, invariant rollback — all alerted via Teams so a human is
informed). 1 only on an unexpected crash, so Railway surfaces the
cron service as failed for genuinely unforeseen problems.
"""

import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

import db
from mayfair_sync import run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cron_mayfair_sync] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

COMPANY_ID = int(os.getenv("MPL_CRON_COMPANY_ID", "25"))  # Mayfair Mgmt

# Sanity-gate thresholds. Today's real baseline: 8 created, 0
# inactivated, 115 updated — these sit comfortably above normal daily
# churn but well below a "the feed broke" event. Tunable via env.
MAX_PROPERTIES_INACTIVATED = int(os.getenv("MPL_CRON_MAX_INACTIVATE", "10"))
MAX_PROPERTIES_CREATED     = int(os.getenv("MPL_CRON_MAX_CREATE", "25"))
MAX_USERS_INACTIVATED      = int(os.getenv("MPL_CRON_MAX_USER_INACTIVATE", "5"))

BACKUP_RETENTION_DAYS = int(os.getenv("MPL_CRON_BACKUP_RETENTION_DAYS", "14"))

_BACKUP_RE = re.compile(
    r"^backup_(?:locations|users)_exile_sync_(\d{8}_\d{6})$")


# ── Teams alerting ────────────────────────────────────────────


def _alert(title: str, lines: list[str]) -> None:
    """Post a short message to the Teams incoming webhook. Never raises
    — a broken webhook must not also break the cron. Logs always, so
    there is a record even with no webhook configured."""
    logger.warning(
        "ALERT — %s | %s", title, " | ".join(lines))
    url = (os.getenv("MS_TEAMS_WEBHOOK_URL") or "").strip()
    if not url:
        logger.warning("MS_TEAMS_WEBHOOK_URL unset — alert logged only.")
        return
    try:
        # Power Automate "Workflows" webhook (the current Microsoft path;
        # classic O365 MessageCard connectors are deprecated). It expects
        # a message envelope wrapping an Adaptive Card.
        requests.post(url, timeout=10, json={
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "size": "Medium",
                         "weight": "Bolder", "wrap": True,
                         "text": f"Echo Audit — {title}"},
                    ] + [
                        {"type": "TextBlock", "wrap": True, "text": f"• {l}"}
                        for l in lines
                    ],
                },
            }],
        })
    except requests.RequestException as exc:
        logger.error("Teams webhook post failed: %s", exc)


# ── backup retention ──────────────────────────────────────────


def _prune_old_backups() -> None:
    """Drop backup_{locations,users}_exile_sync_<YYYYMMDD_HHMMSS> tables
    older than the retention window. Best-effort: a prune failure logs
    and is swallowed (it must never fail the sync itself)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_RETENTION_DAYS)
    conn = db.get_conn()
    try:
        rows = conn.execute(db.q(
            """SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                  AND tablename LIKE 'backup_%_exile_sync_%'"""), ()).fetchall()
        names = [dict(r)["tablename"] for r in rows]
        dropped = 0
        for name in names:
            m = _BACKUP_RE.match(name)
            if not m:
                continue
            try:
                ts = datetime.strptime(
                    m.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if ts < cutoff:
                # Identifier is regex-validated (date suffix only) — safe
                # to inline; table names can't be bound as parameters.
                conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                dropped += 1
        conn.commit()
        if dropped:
            logger.info("pruned %d backup table(s) older than %d days",
                        dropped, BACKUP_RETENTION_DAYS)
    except Exception:
        conn.rollback()
        logger.exception("backup prune failed (non-fatal)")
    finally:
        conn.close()


# ── main ──────────────────────────────────────────────────────


def _gate_reasons(counts: dict) -> list[str]:
    """Return human-readable reasons the plan should HOLD, or [] to go."""
    reasons = []
    pi = counts.get("properties_inactivated", 0)
    pc = counts.get("properties_created", 0)
    ui = counts.get("users_inactivated", 0)
    if pi > MAX_PROPERTIES_INACTIVATED:
        reasons.append(
            f"{pi} properties would be inactivated "
            f"(limit {MAX_PROPERTIES_INACTIVATED})")
    if pc > MAX_PROPERTIES_CREATED:
        reasons.append(
            f"{pc} properties would be created "
            f"(limit {MAX_PROPERTIES_CREATED})")
    if ui > MAX_USERS_INACTIVATED:
        reasons.append(
            f"{ui} users would be inactivated "
            f"(limit {MAX_USERS_INACTIVATED})")
    return reasons


def main() -> int:
    logger.info("daily MPL sync starting (company_id=%s)", COMPANY_ID)

    # 1. Dry-run: compute the plan, write nothing.
    preview = run_sync(COMPANY_ID, triggered_by_user_id=None, dry_run=True)

    if preview["status"] == "failed":
        _alert("MPL sync FAILED (feed/preview)", [
            f"Company {COMPANY_ID}", f"Error: {preview.get('error')}",
            "No changes written. Feed likely unreachable — will retry "
            "next scheduled run.",
        ])
        return 0  # handled: upstream issue, alerted, not a crash

    counts = preview.get("counts", {})
    logger.info("preview counts: %s", counts)

    # 2. Sanity gate.
    holds = _gate_reasons(counts)
    if holds:
        _alert("MPL sync HELD — large change, not applied", holds + [
            f"Company {COMPANY_ID}",
            "Nothing was written. Review the preview on the platform "
            "admin → Mayfair Sync page and Apply manually if correct.",
        ])
        return 0  # handled: deliberate hold, human notified

    # 3. Within thresholds — commit for real.
    result = run_sync(COMPANY_ID, triggered_by_user_id=None, dry_run=False)

    if result["status"] == "failed":
        _alert("MPL sync FAILED on Apply (rolled back)", [
            f"Company {COMPANY_ID}", f"Error: {result.get('error')}",
            "The run rolled back automatically — live tables unchanged. "
            "Review the platform admin page.",
        ])
        # 4. Prune regardless (older backups are still safe to drop).
        _prune_old_backups()
        return 0  # handled: invariant rollback, alerted

    rc = result.get("counts", {})
    logger.info(
        "APPLIED ok status=%s created=%s updated=%s inactivated=%s "
        "users_updated=%s backup=%s",
        result["status"], rc.get("properties_created"),
        rc.get("properties_updated"), rc.get("properties_inactivated"),
        rc.get("users_updated"), result.get("backup_tag"))

    # 4. Retention sweep. Clean run = silent (no Teams), per scope.
    _prune_old_backups()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # truly unexpected — let Railway see red
        logger.exception("cron crashed unexpectedly")
        try:
            _alert("MPL sync CRON CRASHED", [
                f"Unexpected error: {exc}",
                "Live tables are protected by the in-sync snapshot + "
                "rollback, but the cron itself errored. Investigate.",
            ])
        finally:
            sys.exit(1)
