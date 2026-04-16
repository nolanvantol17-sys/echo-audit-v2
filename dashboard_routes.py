"""
dashboard_routes.py — Echo Audit V2 Phase 4 dashboard + chart routes.

Both routes scope to the current company through the project chain:
    interaction → project → projects.company_id.

Month-scoping uses the current calendar month in UTC. Dashboard chart route
supports three view_by modes: date (per-interaction line points), project
(bar averages), respondent (bar averages).
"""

from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_login import login_required

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


def _month_bounds(today=None):
    """Return (start_date, end_date_exclusive) for the calendar month of `today`."""
    today = today or date.today()
    start = today.replace(day=1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end


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

        # leaderboard: top 3 respondents by avg score this month.
        # Groups by respondents.respondent_id — the detected external person
        # from the transcript. Rows with no respondent_id are excluded: an
        # unnamed interaction can't be ranked against a person.
        cur = conn.execute(
            q("""SELECT
                    r.respondent_id,
                    r.respondent_name,
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
                 GROUP BY r.respondent_id, r.respondent_name
                 ORDER BY avg_score DESC
                 LIMIT 3"""),
            (company_id, STATUS_NO_ANSWER, month_start, month_end),
        )
        leaderboard = []
        for row in _rows(cur):
            avg = row.get("avg_score")
            leaderboard.append({
                "respondent_id":   row["respondent_id"],
                "respondent_name": row["respondent_name"],
                "avg_score":       round(float(avg), 1) if avg is not None else None,
                "call_count":      row["call_count"],
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
                    p.project_name,
                    p.project_all_locations,
                    COALESCE(loc_c.location_name, loc_rg.location_name) AS location_name,
                    COALESCE(
                        r.respondent_name,
                        NULLIF(TRIM(u.user_first_name || ' ' || u.user_last_name), ''),
                        i.interaction_responder_name
                    ) AS respondent_name
                 FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 LEFT JOIN campaigns     cmp     ON cmp.campaign_id     = p.campaign_id
                 LEFT JOIN locations     loc_c   ON loc_c.location_id   = cmp.location_id
                 LEFT JOIN rubric_groups rg      ON rg.rubric_group_id  = p.rubric_group_id
                 LEFT JOIN locations     loc_rg  ON loc_rg.location_id  = rg.location_id
                 LEFT JOIN users       u ON u.user_id       = i.respondent_user_id
                 LEFT JOIN respondents r ON r.respondent_id = i.respondent_id
                 WHERE p.company_id = ? AND i.interaction_deleted_at IS NULL
                 ORDER BY i.interaction_id DESC
                 LIMIT 5"""),
            (company_id,),
        )
        recent = _rows(cur)
        # Mask the resolved location for all-locations projects so the UI
        # shows "All Locations" rather than a specific (and misleading) name.
        for row in recent:
            if row.get("project_all_locations"):
                row["location_name"] = "All Locations"

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
# GET /api/dashboard/chart
# ═══════════════════════════════════════════════════════════════


_ALLOWED_METRICS = {"interaction_overall_score"}
_ALLOWED_VIEW_BY = {"date", "project", "respondent"}


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

    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    project_id = request.args.get("project_id")
    respondent_user_id = request.args.get("respondent_user_id")
    location_id = request.args.get("location_id")
    campaign_id = request.args.get("campaign_id")

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
    if respondent_user_id:
        filters.append("i.respondent_user_id = ?")
        params.append(respondent_user_id)

    # Location / campaign filters walk the project → campaigns → locations
    # chain. Only join campaigns when we need to — avoids excluding projects
    # with NULL campaign_id on unfiltered requests.
    campaigns_join = ""
    if campaign_id or location_id:
        campaigns_join = "JOIN campaigns c ON c.campaign_id = p.campaign_id"
        if campaign_id:
            filters.append("c.campaign_id = ?")
            params.append(campaign_id)
        if location_id:
            filters.append("c.location_id = ?")
            params.append(location_id)

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

        # view_by == "respondent"
        sql = f"""
            SELECT
                i.respondent_user_id,
                (u.user_first_name || ' ' || u.user_last_name) AS respondent_name,
                AVG(i.{metric}) AS avg_score,
                COUNT(*) AS call_count
            FROM interactions i
            JOIN projects p ON p.project_id = i.project_id
            {campaigns_join}
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
    finally:
        conn.close()
