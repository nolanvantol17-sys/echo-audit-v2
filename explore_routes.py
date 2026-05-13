"""
explore_routes.py — Ad Hoc Reporting page backend.

Two endpoints in Phase 1:
  GET /api/explore/filters       — dropdown options for the selector bar
  GET /api/explore/site-history  — Report #1 (dot plot: properties × dates)

Phase 2 adds /api/explore/regional-performance + /api/explore/campaign/<id>;
Phase 3 wires permission filtering via helpers.location_scope_for_user.

The data path: explore_query.build_interaction_query produces the SQL.
This file's job is parsing request params, calling the builder, and
shaping the response. Report-specific code stays small on purpose so the
two phase-2 reports can land without touching the query layer.
"""

from flask import Blueprint, g, jsonify, request
from flask_login import login_required

from db import get_conn, q
from helpers import get_effective_company_id, is_feature_enabled
from explore_query import build_interaction_query

explore_bp = Blueprint("explore", __name__, url_prefix="/api")

STATUS_NO_ANSWER = 44

# Server-side cap on dot count for Report #1. Without it, a no-filter query
# against a large tenant returns thousands of points; Chart.js scatter slows
# noticeably past ~2000 dots and axis ticks crowd unreadable.
SITE_HISTORY_HARD_LIMIT = 2000

# When the user hasn't picked a location subset, fall back to the top-N
# busiest locations so the X-axis stays scannable. UI shows a banner.
SITE_HISTORY_DEFAULT_TOP_LOCATIONS = 50


# ── Helpers ─────────────────────────────────────────────────────


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


def _require_feature(company_id, flag_key):
    """Return (None, err_response) when the feature is disabled, else (None, None)."""
    if not is_feature_enabled(company_id, flag_key, default=False):
        return None, _err("Feature not enabled for this company.", 404)
    return None, None


def _parse_id_list(raw):
    if not raw:
        return []
    out = []
    for piece in str(raw).split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except (TypeError, ValueError):
            continue
    return out


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _rows(cur):
    return [_row_to_dict(r) for r in cur.fetchall()]


def _truncate_summary(s, max_chars=140):
    """First sentence of a multi-paragraph assessment, capped at max_chars.

    Falls back to a hard char-truncate (with ellipsis) when no sentence
    boundary is found in the first max_chars. None-safe; empty string in
    means empty string out.
    """
    if not s:
        return ""
    s = str(s).strip()
    if not s:
        return ""
    # First sentence — split on ". " not bare ".", to avoid breaking on "Inc."
    dot = s.find(". ")
    if 0 < dot <= max_chars:
        return s[:dot + 1]
    if len(s) <= max_chars:
        return s
    return s[:max_chars - 1].rstrip() + "…"


def _company_avg_score(conn, company_id):
    """All-time company average — same exclusion rules as the chart."""
    cur = conn.execute(q("""
        SELECT AVG(i.interaction_overall_score) AS avg_score
        FROM interactions i
        JOIN projects p ON p.project_id = i.project_id
        WHERE p.company_id = ?
          AND i.interaction_deleted_at IS NULL
          AND i.interaction_is_test = FALSE
          AND i.status_id <> ?
          AND i.interaction_overall_score IS NOT NULL
    """), [company_id, STATUS_NO_ANSWER])
    row = cur.fetchone()
    if not row:
        return None
    val = row["avg_score"] if hasattr(row, "keys") else row[0]
    return round(float(val), 2) if val is not None else None


# ═══════════════════════════════════════════════════════════════
# GET /api/explore/filters
# ═══════════════════════════════════════════════════════════════


@explore_bp.route("/explore/filters", methods=["GET"])
@login_required
def get_filters():
    """Return dropdown options for the Explore page.

    Differs from /api/dashboard/filters in two ways:
      1. `users` returns BOTH callers and respondents in one list — Report #1
         lets the user filter on either side without picking a role first.
      2. `campaigns` is company-wide (not project-scoped). Carlos's vision
         lets you slice by campaign across projects.

    Each list returns only entities with at least one non-deleted interaction
    in the company, so empty buckets don't clutter the dropdowns.
    """
    company_id, err = _require_company()
    if err: return err
    _, ferr = _require_feature(company_id, "ff_explore_v1")
    if ferr: return ferr

    conn = get_conn()
    try:
        base_where = (
            "p.company_id = ? "
            "AND i.interaction_deleted_at IS NULL"
        )

        # locations — directly attached to interactions
        cur = conn.execute(
            q(f"""SELECT DISTINCT l.location_id, l.location_name
                    FROM interactions i
                    JOIN projects  p ON p.project_id  = i.project_id
                    JOIN locations l ON l.location_id = i.interaction_location_id
                   WHERE {base_where}
                     AND l.location_deleted_at IS NULL
                   ORDER BY l.location_name ASC"""),
            [company_id],
        )
        locations = _rows(cur)

        # users — UNION of callers and respondents (Echo Audit users only;
        # external respondents live in the respondents table and are handled
        # separately if/when Carlos wants them as a filter target).
        cur = conn.execute(
            q(f"""SELECT DISTINCT
                      u.user_id,
                      TRIM(u.user_first_name || ' ' || u.user_last_name) AS user_name
                    FROM interactions i
                    JOIN projects p ON p.project_id = i.project_id
                    JOIN users    u ON u.user_id IN (i.caller_user_id, i.respondent_user_id)
                   WHERE {base_where}
                   ORDER BY user_name ASC"""),
            [company_id],
        )
        users = _rows(cur)

        # campaigns — company-wide, distinct via interactions
        cur = conn.execute(
            q(f"""SELECT DISTINCT c.campaign_id, c.campaign_name, c.project_id
                    FROM interactions i
                    JOIN projects   p ON p.project_id   = i.project_id
                    JOIN campaigns  c ON c.campaign_id  = i.campaign_id
                   WHERE {base_where}
                     AND c.campaign_deleted_at IS NULL
                   ORDER BY c.campaign_name ASC"""),
            [company_id],
        )
        campaigns = _rows(cur)

        # projects — for the (Phase 2+) optional project narrowing
        cur = conn.execute(
            q("""SELECT project_id, project_name
                   FROM projects
                  WHERE company_id = ? AND project_deleted_at IS NULL
                  ORDER BY project_name ASC"""),
            [company_id],
        )
        projects = _rows(cur)

        return jsonify({
            "locations": locations,
            "users":     users,
            "campaigns": campaigns,
            "projects":  projects,
        })
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# GET /api/explore/site-history  — Report #1
# ═══════════════════════════════════════════════════════════════


@explore_bp.route("/explore/site-history", methods=["GET"])
@login_required
def get_site_history():
    """Return dot-plot points for Report #1 (Site History).

    Each point = one interaction. The chart plots property name (X, categorical)
    by date (Y) with the dot colored by score. Hover card uses caller_name,
    respondent_name, score, and a one-sentence summary.

    When no location filter is supplied, server caps to the top N busiest
    locations to keep the X-axis scannable. UI surfaces the cap via a banner.
    """
    company_id, err = _require_company()
    if err: return err
    _, ferr = _require_feature(company_id, "ff_explore_v1")
    if ferr: return ferr

    location_ids = _parse_id_list(request.args.get("location_ids"))
    user_ids     = _parse_id_list(request.args.get("user_ids"))
    campaign_ids = _parse_id_list(request.args.get("campaign_ids"))
    date_from    = request.args.get("date_from") or None
    date_to      = request.args.get("date_to") or None

    # When no location filter is given, derive a top-N location subset by
    # call count and inject it. This caps the X-axis at a readable width
    # without dropping any of the user's explicit filters.
    capped_to_top = False
    if not location_ids:
        conn = get_conn()
        try:
            cur = conn.execute(q("""
                SELECT i.interaction_location_id AS location_id, COUNT(*) AS n
                FROM interactions i
                JOIN projects p ON p.project_id = i.project_id
                WHERE p.company_id = ?
                  AND i.interaction_deleted_at IS NULL
                  AND i.interaction_is_test = FALSE
                  AND i.status_id <> ?
                  AND i.interaction_location_id IS NOT NULL
                GROUP BY i.interaction_location_id
                ORDER BY n DESC
                LIMIT ?
            """), [company_id, STATUS_NO_ANSWER, SITE_HISTORY_DEFAULT_TOP_LOCATIONS])
            rows = _rows(cur)
            top_ids = [r["location_id"] for r in rows]
        finally:
            conn.close()
        if top_ids:
            location_ids = top_ids
            capped_to_top = True

    sql, params = build_interaction_query(
        company_id,
        columns=[
            "interaction_id", "location_id", "location_name",
            "interaction_date", "score",
            "caller_name", "respondent_name", "summary",
        ],
        location_ids=location_ids or None,
        user_ids=user_ids or None,
        campaign_ids=campaign_ids or None,
        date_from=date_from,
        date_to=date_to,
        order_by=[("interaction_date", "asc")],
        limit=SITE_HISTORY_HARD_LIMIT,
    )

    conn = get_conn()
    try:
        cur = conn.execute(sql, params)
        raw_points = _rows(cur)
        company_avg = _company_avg_score(conn, company_id)
    finally:
        conn.close()

    points = []
    for r in raw_points:
        # interaction_date is a date object in Postgres; emit ISO string
        # so the JS layer can hand it straight to a Chart.js time/category
        # scale without per-row parsing.
        d = r.get("interaction_date")
        date_str = d.isoformat() if hasattr(d, "isoformat") else (d or None)
        score = r.get("score")
        points.append({
            "interaction_id":   r.get("interaction_id"),
            "location_id":      r.get("location_id"),
            "location_name":    r.get("location_name") or "(unknown)",
            "interaction_date": date_str,
            "score":            float(score) if score is not None else None,
            "caller_name":      r.get("caller_name") or "",
            "respondent_name":  r.get("respondent_name") or "",
            "summary":          _truncate_summary(r.get("summary")),
        })

    return jsonify({
        "points":         points,
        "company_avg":    company_avg,
        "capped_to_top":  capped_to_top,
        "cap":            SITE_HISTORY_DEFAULT_TOP_LOCATIONS if capped_to_top else None,
        "limit_reached":  len(points) >= SITE_HISTORY_HARD_LIMIT,
    })
