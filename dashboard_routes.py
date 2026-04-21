"""
dashboard_routes.py — Echo Audit V2 Phase 4 dashboard + chart routes.

All routes scope to the current company through the project chain:
    interaction → project → projects.company_id.

Month-scoping uses the current calendar month in UTC. Dashboard chart route
supports view_by modes: date (line), project / respondent / caller / location
/ campaign (bar averages).
"""

from datetime import date, timedelta

from flask import Blueprint, jsonify, request
from flask_login import login_required

from dashboard_helpers import (
    _month_bounds, _report_url_for, _roll_up_locations, _trend_for_calls,
)
from db import IS_POSTGRES, get_conn, q
from helpers import get_effective_company_id

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/api")

# status_id = 44 is the no_answer category; exclude it from score aggregates
STATUS_NO_ANSWER = 44


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


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def _rows(cur):
    return [_row_to_dict(r) for r in cur.fetchall()]


def _scalar(row, key, fallback=0):
    """Pull a single value from a row that might be a dict or a tuple."""
    if row is None:
        return fallback
    try:
        val = row[key]
    except (KeyError, IndexError, TypeError):
        try:
            val = row[0]
        except Exception:
            val = fallback
    return fallback if val is None else val


def _parse_id_list(raw):
    """Parse a comma-separated id list query param into a list of ints.
    Returns [] if no value or all values are non-numeric."""
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


def _in_clause(n):
    """Return a parenthesized placeholder list for IN (?, ?, ...) of length n.
    Caller must guarantee n >= 1."""
    return "(" + ",".join(["?"] * n) + ")"


# ═══════════════════════════════════════════════════════════════
# GET /api/dashboard
# ═══════════════════════════════════════════════════════════════


@dashboard_bp.route("/dashboard", methods=["GET"])
@login_required
def get_dashboard():
    company_id, err = _require_company()
    if err: return err

    month_start, month_end = _month_bounds()

    conn = get_conn()
    try:
        # total_calls, avg_score, below_threshold (scored calls only)
        cur = conn.execute(
            q("""SELECT
                    COUNT(*) AS total_calls,
                    AVG(i.interaction_overall_score) AS avg_score,
                    COUNT(CASE WHEN i.interaction_overall_score < 5.0 THEN 1 END)
                        AS below_threshold
                 FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 WHERE p.company_id = ?
                   AND i.interaction_deleted_at IS NULL
                   AND i.status_id <> ?
                   AND i.interaction_date >= ?
                   AND i.interaction_date <  ?"""),
            (company_id, STATUS_NO_ANSWER, month_start, month_end),
        )
        scored_row = _row_to_dict(cur.fetchone()) or {}
        avg_raw = scored_row.get("avg_score")
        avg_score = round(float(avg_raw), 1) if avg_raw is not None else None

        # no_answer_count (separately counted)
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 WHERE p.company_id = ?
                   AND i.interaction_deleted_at IS NULL
                   AND i.status_id = ?
                   AND i.interaction_date >= ?
                   AND i.interaction_date <  ?"""),
            (company_id, STATUS_NO_ANSWER, month_start, month_end),
        )
        no_answer_count = _scalar(_row_to_dict(cur.fetchone()), "cnt", 0)

        # active_projects (status 1, non-deleted)
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM projects
                 WHERE company_id = ? AND status_id = 1
                   AND project_deleted_at IS NULL"""),
            (company_id,),
        )
        active_projects = _scalar(_row_to_dict(cur.fetchone()), "cnt", 0)

        # leaderboard: top 3 callers this month, aggregated by respondent name
        # (so the same person at multiple locations rolls up to one row).
        # Each row is enriched with a locations roll-up (this month), a trend
        # signal (rolling 30-day window), the most-recent-call timestamp, and
        # a deep-link into Performance Reports keyed on either pr_id (1 PR
        # under that name) or focus_name (multiple PRs across locations).
        # NULL / empty / 'Name Not Detected' names are excluded.
        cur = conn.execute(
            q("""SELECT
                    TRIM(r.respondent_name) AS respondent_name,
                    AVG(i.interaction_overall_score) AS avg_score,
                    COUNT(*) AS call_count
                 FROM interactions i
                 JOIN projects    p ON p.project_id    = i.project_id
                 JOIN respondents r ON r.respondent_id = i.respondent_id
                 WHERE p.company_id = ?
                   AND i.interaction_deleted_at IS NULL
                   AND i.status_id <> ?
                   AND i.interaction_overall_score IS NOT NULL
                   AND i.interaction_date >= ?
                   AND i.interaction_date <  ?
                   AND r.respondent_name IS NOT NULL
                   AND TRIM(r.respondent_name) <> ''
                   AND TRIM(r.respondent_name) <> 'Name Not Detected'
                 GROUP BY TRIM(r.respondent_name)
                 ORDER BY avg_score DESC
                 LIMIT 3"""),
            (company_id, STATUS_NO_ANSWER, month_start, month_end),
        )
        rolling_start = date.today() - timedelta(days=30)
        leaderboard = []
        for row in _rows(cur):
            name = row["respondent_name"]
            avg = row.get("avg_score")

            # Per-name month-scoped detail → locations + last_call.
            cur2 = conn.execute(
                q("""SELECT
                        l.location_name,
                        i.interaction_date,
                        i.interaction_call_start_time,
                        i.interaction_uploaded_at
                     FROM interactions i
                     JOIN projects    p ON p.project_id    = i.project_id
                     JOIN respondents r ON r.respondent_id = i.respondent_id
                     LEFT JOIN locations l ON l.location_id = r.location_id
                     WHERE p.company_id = ?
                       AND i.interaction_deleted_at IS NULL
                       AND i.status_id <> ?
                       AND i.interaction_overall_score IS NOT NULL
                       AND i.interaction_date >= ?
                       AND i.interaction_date <  ?
                       AND TRIM(r.respondent_name) = ?"""),
                (company_id, STATUS_NO_ANSWER, month_start, month_end, name),
            )
            month_calls = _rows(cur2)
            locations = _roll_up_locations(month_calls)

            ts_values = []
            for r in month_calls:
                ts = (r.get("interaction_call_start_time")
                      or r.get("interaction_uploaded_at")
                      or r.get("interaction_date"))
                if ts is not None:
                    ts_values.append(ts)
            last_call = max(ts_values) if ts_values else None
            last_call_iso = (last_call.isoformat()
                             if hasattr(last_call, "isoformat")
                             else (str(last_call) if last_call else None))

            # Per-name rolling-30-day trend (independent of the calendar
            # month, so early-in-the-month dashboards still surface trend
            # signal from the prior weeks).
            cur3 = conn.execute(
                q("""SELECT
                        i.interaction_date,
                        i.interaction_overall_score
                     FROM interactions i
                     JOIN projects    p ON p.project_id    = i.project_id
                     JOIN respondents r ON r.respondent_id = i.respondent_id
                     WHERE p.company_id = ?
                       AND i.interaction_deleted_at IS NULL
                       AND i.status_id <> ?
                       AND i.interaction_overall_score IS NOT NULL
                       AND i.interaction_date >= ?
                       AND TRIM(r.respondent_name) = ?"""),
                (company_id, STATUS_NO_ANSWER, rolling_start, name),
            )
            trend = _trend_for_calls(_rows(cur3))

            cur4 = conn.execute(
                q("""SELECT pr.performance_report_id
                     FROM performance_reports pr
                     JOIN respondents r ON r.respondent_id = pr.respondent_id
                     WHERE r.company_id = ?
                       AND TRIM(r.respondent_name) = ?"""),
                (company_id, name),
            )
            report_url = _report_url_for(name, _rows(cur4))

            leaderboard.append({
                "respondent_name": name,
                "avg_score":       round(float(avg), 1) if avg is not None else None,
                "call_count":      row["call_count"],
                "locations":       locations,
                "trend":           trend,
                "last_call":       last_call_iso,
                "report_url":      report_url,
            })

        # recent interactions: last 5 across the company. Respondent name
        # resolves from respondents first, then from users (known-user path),
        # then from the legacy interaction_responder_name free-text column.
        cur = conn.execute(
            q("""SELECT
                    i.interaction_id,
                    i.interaction_date,
                    i.interaction_call_start_time,
                    i.interaction_uploaded_at,
                    i.interaction_overall_score,
                    i.interaction_flags,
                    p.project_name,
                    loc.location_name,
                    COALESCE(
                        r.respondent_name,
                        NULLIF(TRIM(u.user_first_name || ' ' || u.user_last_name), ''),
                        i.interaction_responder_name
                    ) AS respondent_name
                 FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 LEFT JOIN locations   loc ON loc.location_id   = i.interaction_location_id
                 LEFT JOIN users       u   ON u.user_id         = i.respondent_user_id
                 LEFT JOIN respondents r   ON r.respondent_id   = i.respondent_id
                 WHERE p.company_id = ? AND i.interaction_deleted_at IS NULL
                 ORDER BY i.interaction_id DESC
                 LIMIT 5"""),
            (company_id,),
        )
        recent = _rows(cur)

        return jsonify({
            "stat_cards": {
                "total_calls":      _scalar(scored_row, "total_calls", 0),
                "avg_score":        avg_score,
                "below_threshold":  _scalar(scored_row, "below_threshold", 0),
                "no_answer_count":  no_answer_count,
                "active_projects":  active_projects,
            },
            "leaderboard":        leaderboard,
            "recent_interactions": recent,
            "month_start":        month_start.isoformat(),
            "month_end":          (month_end - timedelta(days=1)).isoformat(),
        })
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# GET /api/dashboard/filters
# ═══════════════════════════════════════════════════════════════


@dashboard_bp.route("/dashboard/filters", methods=["GET"])
@login_required
def get_filters():
    """Returns the dropdown options for the dashboard widget.

    Each list contains only entities that have actually appeared on at least
    one non-deleted interaction in the company (or in the given project, if
    project_id is provided). Campaigns include their location_id so the UI
    can narrow the campaign list when locations are selected.
    """
    company_id, err = _require_company()
    if err: return err

    project_id = request.args.get("project_id")

    base_filters = ["p.company_id = ?", "i.interaction_deleted_at IS NULL"]
    base_params = [company_id]
    if project_id:
        base_filters.append("i.project_id = ?")
        base_params.append(project_id)
    where = " AND ".join(base_filters)

    conn = get_conn()
    try:
        # locations: distinct locations directly attached to each interaction.
        # Uses i.interaction_location_id (the authoritative column) so we don't
        # drop rows when projects.campaign_id IS NULL (project_all_locations case).
        cur = conn.execute(
            q(f"""SELECT DISTINCT l.location_id, l.location_name
                  FROM interactions i
                  JOIN projects  p ON p.project_id  = i.project_id
                  JOIN locations l ON l.location_id = i.interaction_location_id
                  WHERE {where}
                    AND l.location_deleted_at IS NULL
                  ORDER BY l.location_name ASC"""),
            base_params,
        )
        locations = _rows(cur)

        # callers: distinct caller users
        cur = conn.execute(
            q(f"""SELECT DISTINCT
                      u.user_id,
                      TRIM(u.user_first_name || ' ' || u.user_last_name) AS user_name
                  FROM interactions i
                  JOIN projects p ON p.project_id = i.project_id
                  JOIN users    u ON u.user_id    = i.caller_user_id
                  WHERE {where}
                    AND i.caller_user_id IS NOT NULL
                  ORDER BY user_name ASC"""),
            base_params,
        )
        callers = _rows(cur)

        # campaigns: distinct campaigns reachable via project → campaign
        cur = conn.execute(
            q(f"""SELECT DISTINCT
                      c.campaign_id, c.campaign_name, c.location_id
                  FROM interactions i
                  JOIN projects  p ON p.project_id  = i.project_id
                  JOIN campaigns c ON c.campaign_id = p.campaign_id
                  WHERE {where}
                  ORDER BY c.campaign_name ASC"""),
            base_params,
        )
        campaigns = _rows(cur)

        return jsonify({
            "locations": locations,
            "callers":   callers,
            "campaigns": campaigns,
        })
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# GET /api/dashboard/chart
# ═══════════════════════════════════════════════════════════════


_ALLOWED_METRICS = {"interaction_overall_score"}
_ALLOWED_VIEW_BY = {"date", "project", "respondent", "caller", "location", "campaign"}


@dashboard_bp.route("/dashboard/chart", methods=["GET"])
@login_required
def get_chart():
    company_id, err = _require_company()
    if err: return err

    metric = request.args.get("metric", "interaction_overall_score")
    if metric not in _ALLOWED_METRICS:
        return _err(f"metric must be one of: {', '.join(_ALLOWED_METRICS)}", 400)

    view_by = request.args.get("view_by", "date")
    if view_by not in _ALLOWED_VIEW_BY:
        return _err(f"view_by must be one of: {', '.join(_ALLOWED_VIEW_BY)}", 400)
    # "respondent" is the legacy alias for "caller"; normalise so we have one
    # downstream branch.
    if view_by == "respondent":
        view_by = "caller"

    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    project_id = request.args.get("project_id")

    # Multi-value filters: accept CSV ids (location_ids, caller_ids,
    # campaign_ids). For backward compat, also accept the singular variants
    # used by older callers.
    location_ids = _parse_id_list(request.args.get("location_ids"))
    caller_ids   = _parse_id_list(request.args.get("caller_ids"))
    if not caller_ids:
        caller_ids = _parse_id_list(request.args.get("respondent_user_ids"))
    campaign_ids = _parse_id_list(request.args.get("campaign_ids"))

    legacy_loc = request.args.get("location_id")
    if legacy_loc and not location_ids:
        location_ids = _parse_id_list(legacy_loc)
    legacy_camp = request.args.get("campaign_id")
    if legacy_camp and not campaign_ids:
        campaign_ids = _parse_id_list(legacy_camp)
    legacy_resp = request.args.get("respondent_user_id")
    if legacy_resp and not caller_ids:
        caller_ids = _parse_id_list(legacy_resp)

    filters = [
        "p.company_id = ?",
        "i.interaction_deleted_at IS NULL",
        "i.status_id <> ?",
        "i.interaction_overall_score IS NOT NULL",
    ]
    params = [company_id, STATUS_NO_ANSWER]

    if date_from:
        filters.append("i.interaction_date >= ?")
        params.append(date_from)
    if date_to:
        filters.append("i.interaction_date <= ?")
        params.append(date_to)
    if project_id:
        filters.append("i.project_id = ?")
        params.append(project_id)
    if caller_ids:
        filters.append(f"i.caller_user_id IN {_in_clause(len(caller_ids))}")
        params.extend(caller_ids)

    # Decide whether we need joins to campaigns / locations. Required if any
    # location/campaign filter is set, OR the view_by groups by them.
    needs_campaigns = bool(campaign_ids or location_ids) or view_by in ("location", "campaign")
    needs_locations = bool(location_ids) or view_by == "location"

    campaigns_join = ""
    locations_join = ""
    if needs_campaigns:
        campaigns_join = "JOIN campaigns c ON c.campaign_id = p.campaign_id"
        if campaign_ids:
            filters.append(f"c.campaign_id IN {_in_clause(len(campaign_ids))}")
            params.extend(campaign_ids)
    if needs_locations:
        locations_join = "JOIN locations l ON l.location_id = c.location_id"
        if location_ids:
            filters.append(f"c.location_id IN {_in_clause(len(location_ids))}")
            params.extend(location_ids)

    where_clause = " AND ".join(filters)

    conn = get_conn()
    try:
        if view_by == "date":
            sql = f"""
                SELECT
                    i.interaction_id,
                    i.interaction_date,
                    i.{metric} AS score,
                    p.project_name,
                    (u.user_first_name || ' ' || u.user_last_name) AS respondent_name
                FROM interactions i
                JOIN projects p ON p.project_id = i.project_id
                {campaigns_join}
                {locations_join}
                LEFT JOIN users u ON u.user_id = i.respondent_user_id
                WHERE {where_clause}
                ORDER BY i.interaction_date ASC, i.interaction_id ASC
            """
            cur = conn.execute(q(sql), params)
            points = []
            labels = []
            data = []
            for row in _rows(cur):
                d = row["interaction_date"]
                d_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
                score = row["score"]
                score = float(score) if score is not None else None
                points.append({
                    "interaction_id":  row["interaction_id"],
                    "interaction_date": d_str,
                    "score":           score,
                    "project_name":    row.get("project_name"),
                    "respondent_name": row.get("respondent_name"),
                })
                labels.append(d_str)
                data.append(score)
            return jsonify({
                "type":     "line",
                "labels":   labels,
                "datasets": [{"label": "Score", "data": data}],
                "points":   points,
            })

        if view_by == "project":
            sql = f"""
                SELECT
                    p.project_id,
                    p.project_name,
                    AVG(i.{metric}) AS avg_score,
                    COUNT(*) AS call_count
                FROM interactions i
                JOIN projects p ON p.project_id = i.project_id
                {campaigns_join}
                {locations_join}
                WHERE {where_clause}
                GROUP BY p.project_id, p.project_name
                ORDER BY avg_score DESC
            """
            cur = conn.execute(q(sql), params)
            labels, data, points = [], [], []
            for row in _rows(cur):
                avg = row["avg_score"]
                avg = round(float(avg), 2) if avg is not None else None
                labels.append(row["project_name"])
                data.append(avg)
                points.append({
                    "project_id":   row["project_id"],
                    "project_name": row["project_name"],
                    "avg_score":    avg,
                    "call_count":   row["call_count"],
                })
            return jsonify({
                "type":     "bar",
                "labels":   labels,
                "datasets": [{"label": "Avg Score", "data": data}],
                "points":   points,
            })

        if view_by == "caller":
            sql = f"""
                SELECT
                    i.respondent_user_id,
                    (u.user_first_name || ' ' || u.user_last_name) AS respondent_name,
                    AVG(i.{metric}) AS avg_score,
                    COUNT(*) AS call_count
                FROM interactions i
                JOIN projects p ON p.project_id = i.project_id
                {campaigns_join}
                {locations_join}
                JOIN users u    ON u.user_id    = i.respondent_user_id
                WHERE {where_clause}
                  AND i.respondent_user_id IS NOT NULL
                GROUP BY i.respondent_user_id, respondent_name
                ORDER BY avg_score DESC
            """
            cur = conn.execute(q(sql), params)
            labels, data, points = [], [], []
            for row in _rows(cur):
                avg = row["avg_score"]
                avg = round(float(avg), 2) if avg is not None else None
                labels.append(row["respondent_name"])
                data.append(avg)
                points.append({
                    "respondent_user_id": row["respondent_user_id"],
                    "respondent_name":    row["respondent_name"],
                    "avg_score":          avg,
                    "call_count":         row["call_count"],
                })
            return jsonify({
                "type":     "bar",
                "labels":   labels,
                "datasets": [{"label": "Avg Score", "data": data}],
                "points":   points,
            })

        if view_by == "location":
            sql = f"""
                SELECT
                    l.location_id,
                    l.location_name,
                    AVG(i.{metric}) AS avg_score,
                    COUNT(*) AS call_count
                FROM interactions i
                JOIN projects p ON p.project_id = i.project_id
                {campaigns_join}
                {locations_join}
                WHERE {where_clause}
                GROUP BY l.location_id, l.location_name
                ORDER BY avg_score DESC
            """
            cur = conn.execute(q(sql), params)
            labels, data, points = [], [], []
            for row in _rows(cur):
                avg = row["avg_score"]
                avg = round(float(avg), 2) if avg is not None else None
                labels.append(row["location_name"])
                data.append(avg)
                points.append({
                    "location_id":   row["location_id"],
                    "location_name": row["location_name"],
                    "avg_score":     avg,
                    "call_count":    row["call_count"],
                })
            return jsonify({
                "type":     "bar",
                "labels":   labels,
                "datasets": [{"label": "Avg Score", "data": data}],
                "points":   points,
            })

        # view_by == "campaign"
        sql = f"""
            SELECT
                c.campaign_id,
                c.campaign_name,
                AVG(i.{metric}) AS avg_score,
                COUNT(*) AS call_count
            FROM interactions i
            JOIN projects p ON p.project_id = i.project_id
            {campaigns_join}
            {locations_join}
            WHERE {where_clause}
            GROUP BY c.campaign_id, c.campaign_name
            ORDER BY avg_score DESC
        """
        cur = conn.execute(q(sql), params)
        labels, data, points = [], [], []
        for row in _rows(cur):
            avg = row["avg_score"]
            avg = round(float(avg), 2) if avg is not None else None
            labels.append(row["campaign_name"])
            data.append(avg)
            points.append({
                "campaign_id":   row["campaign_id"],
                "campaign_name": row["campaign_name"],
                "avg_score":     avg,
                "call_count":    row["call_count"],
            })
        return jsonify({
            "type":     "bar",
            "labels":   labels,
            "datasets": [{"label": "Avg Score", "data": data}],
            "points":   points,
        })
    finally:
        conn.close()
