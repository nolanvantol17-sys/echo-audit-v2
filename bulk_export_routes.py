"""
bulk_export_routes.py — Per-property bulk PDF + audio export.

Two routes:
    GET /api/locations/<id>/export/preflight  — counts + size estimate
    GET /api/locations/<id>/export            — streams the multi-PDF ZIP

Both require admin / super_admin. The preflight is cheap (single SUM query)
and intentionally NOT audit-logged — only the actual download leaves data.
"""

import logging

from flask import Blueprint, jsonify, request
from flask_login import login_required

from auth import role_required
from db import get_conn, q
from helpers import get_effective_company_id

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
                    COUNT(*) FILTER (WHERE i.interaction_audio_data IS NOT NULL)     AS audio_count,
                    COALESCE(SUM(LENGTH(i.interaction_audio_data)), 0)               AS audio_bytes,
                    MIN(i.interaction_date)                                          AS oldest_date,
                    MAX(i.interaction_date)                                          AS newest_date
                  FROM interactions i
                  WHERE i.interaction_location_id = ?
                    AND i.project_id              = ?
                    AND i.interaction_deleted_at IS NULL
                    AND i.status_id IN ({placeholders})"""),
            [location_id, project_id, *statuses],
        ).fetchone()
    finally:
        conn.close()

    total_count = int(row["total_count"] or 0)
    audio_count = int(row["audio_count"] or 0)
    audio_bytes = int(row["audio_bytes"] or 0)
    est_zip_bytes = audio_bytes + (total_count * _AVG_PDF_BYTES)
    est_zip_mb    = round(est_zip_bytes / 1_048_576, 1)

    return jsonify({
        "count":         total_count,
        "audio_count":   audio_count,
        "est_zip_mb":    est_zip_mb,
        "oldest_date":   row["oldest_date"].isoformat() if row["oldest_date"] else None,
        "newest_date":   row["newest_date"].isoformat() if row["newest_date"] else None,
        "location_name": loc["location_name"],
        "project_name":  proj["project_name"],
    })
