"""
bulk_export_routes.py — Per-property bulk PDF + audio export.

Two routes:
    GET /api/locations/<id>/export/preflight  — counts + size estimate
    GET /api/locations/<id>/export            — streams the multi-PDF ZIP

Both require admin / super_admin. The preflight is cheap (single SUM query)
and intentionally NOT audit-logged — only the actual download leaves data.
"""

import io
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file
from flask_login import current_user, login_required

import pdf_export
from audit_log import ACTION_EXPORTED, ENTITY_LOCATION, write_audit_log
from auth import role_required
from db import IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id
from interactions_routes import _safe_filename_segment, _sniff_audio_ext

logger = logging.getLogger(__name__)

bulk_export_bp = Blueprint("bulk_export", __name__, url_prefix="/api")


# ── Status sets ──
# 43 always; 44 toggleable; 45 (failed-grade reverts) toggleable.
# Mid-flight statuses (40/41/42) deliberately excluded from "failed" — those
# are operational bugs that should be resolved in-app, not bundled into
# client-facing exports. See db.py:_STATUS_SEEDS for the source of truth.
_GRADED_STATUS    = 43
_NO_ANSWER_STATUS = 44
_FAILED_STATUSES  = (45,)

# Avg PDF size from Commit 1 measurements (graded≈113KB, no-answer≈105KB).
# Used in the size estimate; deflate compression brings actual ZIP entries
# down further, but the estimate intentionally errs on the high side.
_AVG_PDF_BYTES = 110_000

# Defensive cap on `interaction_ids` array stored in audit log metadata.
# Realistic exports stay well under this (current largest pair: 2 calls).
_AUDIT_IDS_HARDCAP = 1000


# ── Local helpers (mirrors export_routes.py pattern) ──

def _err(msg, code):
    return jsonify({"error": msg}), code


def _require_company():
    cid = get_effective_company_id()
    if cid is None:
        return None, _err(
            "No company context. Super admins must select an organization first.",
            400,
        )
    return cid, None


def _resolve_status_set(args):
    """Build the SQL IN-list of status IDs from the toggle query params."""
    statuses = [_GRADED_STATUS]
    if args.get("include_no_answer") == "1":
        statuses.append(_NO_ANSWER_STATUS)
    if args.get("include_failed") == "1":
        statuses.extend(_FAILED_STATUSES)
    return statuses


def _resolve_campaign_filter(args):
    """Parse campaign_ids + include_uncategorized into a WHERE-clause snippet.

    Returns (campaign_ids, include_uncategorized, where_snippet, extra_params).
    When neither is provided, the snippet is empty (no filter applied → all
    campaigns + uncategorized are included). Cross-tenant campaign IDs are
    harmless because the surrounding query filters by project_id.
    """
    raw = (args.get("campaign_ids") or "").strip()
    ids = []
    if raw:
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok: continue
            try: ids.append(int(tok))
            except (TypeError, ValueError): pass   # silently drop bad tokens
    inc_unc = args.get("include_uncategorized") == "1"
    if not ids and not inc_unc:
        return [], False, "", []
    parts, params = [], []
    if ids:
        ph = ",".join(["?"] * len(ids))
        parts.append(f"i.campaign_id IN ({ph})")
        params.extend(ids)
    if inc_unc:
        parts.append("i.campaign_id IS NULL")
    return ids, inc_unc, "AND (" + " OR ".join(parts) + ")", params


# ── GET /api/locations/<id>/export/preflight ──

@bulk_export_bp.route("/locations/<int:location_id>/export/preflight", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def export_preflight(location_id):
    company_id, err = _require_company()
    if err: return err

    project_id = request.args.get("project_id")
    if not project_id:
        return _err("Missing project_id", 400)
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return _err("Invalid project_id", 400)

    statuses = _resolve_status_set(request.args)
    placeholders = ",".join(["?"] * len(statuses))
    _, _, camp_where, camp_params = _resolve_campaign_filter(request.args)

    conn = get_conn()
    try:
        # Tenant scope: location must be in this company.
        loc = conn.execute(
            q("""SELECT location_name FROM locations
                 WHERE location_id = ? AND company_id = ?
                   AND location_deleted_at IS NULL"""),
            (location_id, company_id),
        ).fetchone()
        if not loc:
            return _err("Location not found", 404)

        # Tenant scope: project must be in this company.
        proj = conn.execute(
            q("""SELECT project_name FROM projects
                 WHERE project_id = ? AND company_id = ?
                   AND project_deleted_at IS NULL"""),
            (project_id, company_id),
        ).fetchone()
        if not proj:
            return _err("Project not found", 404)

        # Single aggregation query — counts, audio bytes, date range.
        # COALESCE handles the no-rows case where SUM returns NULL.
        row = conn.execute(
            q(f"""SELECT
                    COUNT(*)                                                         AS total_count,
                    SUM(CASE WHEN i.status_id = 43 THEN 1 ELSE 0 END)                AS graded_count,
                    SUM(CASE WHEN i.status_id = 44 THEN 1 ELSE 0 END)                AS no_answer_count,
                    COUNT(*) FILTER (WHERE i.interaction_audio_data IS NOT NULL)     AS audio_count,
                    COALESCE(SUM(LENGTH(i.interaction_audio_data)), 0)               AS audio_bytes,
                    MIN(i.interaction_date)                                          AS oldest_date,
                    MAX(i.interaction_date)                                          AS newest_date
                  FROM interactions i
                  WHERE i.interaction_location_id = ?
                    AND i.project_id              = ?
                    AND i.interaction_deleted_at IS NULL
                    AND i.status_id IN ({placeholders})
                    {camp_where}"""),
            [location_id, project_id, *statuses, *camp_params],
        ).fetchone()
    finally:
        conn.close()

    total_count  = int(row["total_count"]    or 0)
    graded_ct    = int(row["graded_count"]   or 0)
    no_answer_ct = int(row["no_answer_count"] or 0)
    audio_count  = int(row["audio_count"]    or 0)
    audio_bytes  = int(row["audio_bytes"]    or 0)
    est_zip_bytes = audio_bytes + (total_count * _AVG_PDF_BYTES)
    est_zip_mb    = round(est_zip_bytes / 1_048_576, 1)

    return jsonify({
        "count":           total_count,
        "graded_count":    graded_ct,
        "no_answer_count": no_answer_ct,
        "audio_count":     audio_count,
        "est_zip_mb":      est_zip_mb,
        "oldest_date":     row["oldest_date"].isoformat() if row["oldest_date"] else None,
        "newest_date":     row["newest_date"].isoformat() if row["newest_date"] else None,
        "location_name":   loc["location_name"],
        "project_name":    proj["project_name"],
    })


# ── Filename builder (Commit 7 — campaign-aware) ──

def _build_zip_filename(loc_name, proj_name, selected_campaign_names,
                        include_uncategorized):
    """Per Commit 7 spec, filename varies with campaign filter:
       - No filter:   {Loc}_{Proj}_Calls.zip
       - 1-3 picks:   {Loc}_{Proj}_{Camp1}_{Camp2}_Calls.zip
       - 4+ picks:    {Loc}_{Proj}_MultipleCampaigns_Calls.zip
    Selected-name list is alphabetized for filename stability across requests.
    "Uncategorized" counts as one selection toward the 1-3 vs 4+ threshold.
    """
    safe_loc  = _safe_filename_segment(loc_name)
    safe_proj = _safe_filename_segment(proj_name)
    selected = list(selected_campaign_names)
    if include_uncategorized:
        selected.append("Uncategorized")
    if not selected:
        return f"{safe_loc}_{safe_proj}_Calls.zip"
    if len(selected) >= 4:
        return f"{safe_loc}_{safe_proj}_MultipleCampaigns_Calls.zip"
    safe_camps = "_".join(_safe_filename_segment(c, max_len=30) for c in sorted(selected))
    return f"{safe_loc}_{safe_proj}_{safe_camps}_Calls.zip"


# ── Manifest helper (only emitted on partial failure) ──

def _build_manifest(location_name, project_name, args, count_requested,
                    count_succeeded, skipped):
    """Render the _export_manifest.txt body for a partial-failure export.

    `skipped` is a list of (interaction_id, date_str, caller_name, reason).
    Reasons split into PDF-render-failed vs audio-attach-failed for clarity.
    """
    filter_label = "graded only"
    if args.get("include_no_answer") == "1": filter_label += " + no-answer"
    if args.get("include_failed")    == "1": filter_label += " + failed"

    lines = [
        "Echo Audit — Per-Property Export",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Location:  {location_name}",
        f"Project:   {project_name}",
        f"Filters:   {filter_label}",
        "",
        f"Included: {count_succeeded} of {count_requested} interactions",
        "",
    ]
    pdf_skips   = [s for s in skipped if s[3].startswith("PDF render")]
    audio_skips = [s for s in skipped if s[3].startswith("audio")]
    if pdf_skips:
        lines.append("Skipped (PDF render failed — no entry in ZIP):")
        for iid, date, caller, reason in pdf_skips:
            lines.append(f"  #{iid} ({date}, Caller: {caller}) — {reason}")
        lines.append("")
    if audio_skips:
        lines.append("Audio not attached (PDF still included):")
        for iid, date, caller, reason in audio_skips:
            lines.append(f"  #{iid} ({date}, Caller: {caller}) — {reason}")
        lines.append("")
    return "\n".join(lines)


# ── GET /api/locations/<id>/export — bulk per-property ZIP ──

@bulk_export_bp.route("/locations/<int:location_id>/export", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def export_location_bulk(location_id):
    company_id, err = _require_company()
    if err: return err

    project_id = request.args.get("project_id")
    if not project_id:
        return _err("Missing project_id", 400)
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return _err("Invalid project_id", 400)

    statuses = _resolve_status_set(request.args)
    placeholders = ",".join(["?"] * len(statuses))
    campaign_ids, inc_unc, camp_where, camp_params = _resolve_campaign_filter(request.args)

    succeeded_ids = []
    skipped       = []   # list of (iid, date_str, caller_name, reason)
    audio_count   = 0

    conn = get_conn()
    try:
        # Tenant scope (mirror preflight).
        loc = conn.execute(
            q("""SELECT location_name FROM locations
                 WHERE location_id = ? AND company_id = ?
                   AND location_deleted_at IS NULL"""),
            (location_id, company_id),
        ).fetchone()
        if not loc:
            return _err("Location not found", 404)
        proj = conn.execute(
            q("""SELECT project_name FROM projects
                 WHERE project_id = ? AND company_id = ?
                   AND project_deleted_at IS NULL"""),
            (project_id, company_id),
        ).fetchone()
        if not proj:
            return _err("Project not found", 404)

        # List matching interactions (light query — no audio bytes here;
        # render_interaction_pdf and the audio fetch each do their own SELECT
        # so a single row's failure doesn't poison the whole result set).
        rows = conn.execute(
            q(f"""SELECT i.interaction_id, i.interaction_date,
                         (caller.user_first_name || ' ' || caller.user_last_name) AS caller_name
                  FROM interactions i
                  LEFT JOIN users caller ON caller.user_id = i.caller_user_id
                  WHERE i.interaction_location_id = ?
                    AND i.project_id              = ?
                    AND i.interaction_deleted_at IS NULL
                    AND i.status_id IN ({placeholders})
                    {camp_where}
                  ORDER BY i.interaction_date DESC, i.interaction_id DESC"""),
            [location_id, project_id, *statuses, *camp_params],
        ).fetchall()

        if not rows:
            return _err("No interactions match these filters", 404)

        # Build the ZIP in memory.
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            for r in rows:
                iid         = r["interaction_id"]
                date_str    = (r["interaction_date"].isoformat()
                               if r["interaction_date"] else "unknown-date")
                caller_raw  = (r["caller_name"] or "Unknown").strip() or "Unknown"
                caller_safe = _safe_filename_segment(caller_raw, max_len=30)
                entry_base  = f"{date_str}_{caller_safe}_#{iid}"

                # PDF: per-row try/except so one malformed row doesn't kill the export.
                try:
                    pdf_bytes = pdf_export.render_interaction_pdf(conn, iid)
                    zf.writestr(f"{entry_base}.pdf", pdf_bytes,
                                compress_type=zipfile.ZIP_DEFLATED)
                    succeeded_ids.append(iid)
                except Exception as e:
                    logger.exception(
                        "Bulk export: PDF render failed for interaction %s", iid)
                    skipped.append((iid, date_str, caller_raw,
                                    f"PDF render failed: {e}"))
                    continue   # no PDF means no audio either

                # Audio: separate try; PDF already in ZIP. Per-row fetch keeps
                # one row's BYTEA in memory at a time (until written to buf).
                try:
                    ar = conn.execute(
                        q("""SELECT interaction_audio_data, interaction_audio_url
                             FROM interactions WHERE interaction_id = ?"""),
                        (iid,),
                    ).fetchone()
                    audio_bytes = None; audio_ext = None
                    if IS_POSTGRES and ar and ar["interaction_audio_data"]:
                        audio_bytes = bytes(ar["interaction_audio_data"])
                        audio_ext   = _sniff_audio_ext(audio_bytes)
                    elif not IS_POSTGRES and ar and ar["interaction_audio_url"]:
                        p = Path(ar["interaction_audio_url"])
                        if p.exists():
                            audio_bytes = p.read_bytes()
                            audio_ext   = p.suffix.lower() or _sniff_audio_ext(audio_bytes)

                    if audio_bytes:
                        zf.writestr(f"{entry_base}{audio_ext}", audio_bytes,
                                    compress_type=zipfile.ZIP_STORED)
                        audio_count += 1
                except Exception as e:
                    logger.exception(
                        "Bulk export: audio attach failed for interaction %s", iid)
                    skipped.append((iid, date_str, caller_raw,
                                    f"audio attach failed (PDF still included): {e}"))

            # Manifest only on partial failure
            if skipped:
                manifest = _build_manifest(
                    loc["location_name"], proj["project_name"],
                    request.args, len(rows), len(succeeded_ids), skipped,
                )
                zf.writestr("_export_manifest.txt", manifest,
                            compress_type=zipfile.ZIP_DEFLATED)

        zip_buf.seek(0)
        zip_size = len(zip_buf.getvalue())
    finally:
        conn.close()

    # Hard failure: every row in the result set failed to render.
    if not succeeded_ids:
        write_audit_log(
            current_user.user_id, ACTION_EXPORTED, ENTITY_LOCATION,
            location_id,
            metadata={
                "location_id":     location_id,
                "location_name":   loc["location_name"],
                "project_id":      project_id,
                "project_name":    proj["project_name"],
                "filters": {
                    "include_no_answer":      request.args.get("include_no_answer") == "1",
                    "include_failed":         request.args.get("include_failed")    == "1",
                    "campaign_filter_active": bool(campaign_ids) or inc_unc,
                    "campaign_ids":           campaign_ids,
                    "include_uncategorized":  inc_unc,
                },
                "count_requested": len(rows),
                "count_succeeded": 0,
                "skipped_count":   len(skipped),
                "outcome":         "all_failed",
            },
        )
        return _err("Export failed: no interactions could be rendered", 500)

    # Look up names of selected campaigns for the filename. Tenant scope is
    # implicit: campaigns are project-scoped, and the row query already
    # filtered by project_id, so a malicious campaign_id from another
    # project simply yields no rows above and no name here.
    selected_camp_names = []
    if campaign_ids:
        conn2 = get_conn()
        try:
            ph = ",".join(["?"] * len(campaign_ids))
            cur = conn2.execute(
                q(f"""SELECT campaign_name FROM campaigns
                      WHERE campaign_id IN ({ph}) AND project_id = ?
                        AND campaign_deleted_at IS NULL"""),
                [*campaign_ids, project_id],
            )
            selected_camp_names = [r["campaign_name"] for r in cur.fetchall()]
        finally:
            conn2.close()
    zip_filename = _build_zip_filename(
        loc["location_name"], proj["project_name"],
        selected_camp_names, inc_unc,
    )

    # Audit log: full ID list under the cap; degrade to count-only above.
    metadata = {
        "location_id":     location_id,
        "location_name":   loc["location_name"],
        "project_id":      project_id,
        "project_name":    proj["project_name"],
        "filters": {
            "include_no_answer":      request.args.get("include_no_answer") == "1",
            "include_failed":         request.args.get("include_failed")    == "1",
            "campaign_filter_active": bool(campaign_ids) or inc_unc,
            "campaign_ids":           campaign_ids,
            "include_uncategorized":  inc_unc,
        },
        "count_requested": len(rows),
        "count_succeeded": len(succeeded_ids),
        "audio_count":     audio_count,
        "skipped_count":   len(skipped),
        "zip_size_bytes":  zip_size,
    }
    if len(succeeded_ids) <= _AUDIT_IDS_HARDCAP:
        metadata["interaction_ids"] = succeeded_ids
    else:
        metadata["interaction_ids_count"] = len(succeeded_ids)
    write_audit_log(
        current_user.user_id, ACTION_EXPORTED, ENTITY_LOCATION,
        location_id, metadata=metadata,
    )

    return send_file(
        zip_buf, mimetype="application/zip",
        as_attachment=True, download_name=zip_filename,
    )
