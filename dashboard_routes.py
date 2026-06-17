"""
dashboard_routes.py — Echo Audit V2 Phase 4 dashboard + chart routes.

All routes scope to the current company through the project chain:
    interaction → project → projects.company_id.

Both /api/dashboard and /api/dashboard/chart accept the same filter vocabulary
(date_from, date_to, location_ids, caller_ids, phone_routing_ids,
campaign_ids — plus legacy location_id / campaign_id singular aliases). When
no filters are supplied the response is all-time. Active projects + recent
interactions intentionally ignore filters — they're live-state surfaces, not
slice metrics. Dashboard chart route supports view_by modes: date (line),
project / caller / location / phone_routing (bar averages). "respondent" is
accepted as a legacy alias for "caller" — both group on i.caller_user_id.
"""

from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from dashboard_helpers import (
    _report_url_for, _roll_up_locations, _trend_for_calls,
)
from db import get_conn, q
from helpers import get_effective_company_id, location_scope_for_user, to_iso_date
from insights import compute_dashboard_insights_async, fetch_cached as fetch_insights_cached

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


def _truncate_summary(s, max_chars=140):
    """First sentence of a multi-paragraph assessment, capped at max_chars.

    Used by the date-view chart points so the chart tooltip can render a
    one-sentence quick context line per call without dragging the full
    overall_assessment text through every fetch. None-safe; empty in →
    empty out.
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


def _company_avg_score(conn, company_id):
    """Company-wide all-time average overall score, ignoring all chart filters
    (date / location / caller / etc). Used as the baseline reference line on
    every chart so a slice can be visually compared against the company norm.

    Same baseline filters as the chart query (not deleted, not test, not
    no-answer, score not null) so the two numbers are comparable.
    """
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
# GET /api/dashboard
# ═══════════════════════════════════════════════════════════════


@dashboard_bp.route("/dashboard", methods=["GET"])
@login_required
def get_dashboard():
    company_id, err = _require_company()
    if err: return err

    # Filter params (vocabulary mirrors /api/dashboard/chart and
    # /api/projects/<id>/summary so the widget snapshot can be splatted
    # straight in). When no params are supplied the response is all-time.
    date_from = request.args.get("date_from")
    date_to   = request.args.get("date_to")
    location_ids      = _parse_id_list(request.args.get("location_ids"))
    caller_ids        = _parse_id_list(request.args.get("caller_ids"))
    phone_routing_ids = _parse_id_list(request.args.get("phone_routing_ids"))
    campaign_ids      = _parse_id_list(request.args.get("campaign_ids"))

    legacy_loc = request.args.get("location_id")
    if legacy_loc and not location_ids:
        location_ids = _parse_id_list(legacy_loc)
    legacy_camp = request.args.get("campaign_id")
    if legacy_camp and not campaign_ids:
        campaign_ids = _parse_id_list(legacy_camp)

    extra_where  = []
    extra_params = []
    if date_from:
        extra_where.append("i.interaction_date >= ?")
        extra_params.append(date_from)
    if date_to:
        extra_where.append("i.interaction_date <= ?")
        extra_params.append(date_to)
    if location_ids:
        extra_where.append(f"i.interaction_location_id IN {_in_clause(len(location_ids))}")
        extra_params.extend(location_ids)
    if caller_ids:
        extra_where.append(f"i.caller_user_id IN {_in_clause(len(caller_ids))}")
        extra_params.extend(caller_ids)
    if phone_routing_ids:
        extra_where.append(f"p.phone_routing_id IN {_in_clause(len(phone_routing_ids))}")
        extra_params.extend(phone_routing_ids)
    if campaign_ids:
        extra_where.append(f"i.campaign_id IN {_in_clause(len(campaign_ids))}")
        extra_params.extend(campaign_ids)
    # Permission scope (ff_permission_filtering off → empty). Goes through
    # the same WHERE-clause channel as the user-supplied filters above so
    # every interactions read in this route picks it up by composition.
    scope_sql, scope_params = location_scope_for_user(
        current_user.user_id, current_user.role, company_id,
    )
    if scope_sql:
        extra_where.append(scope_sql)
        extra_params.extend(scope_params)
    extra_clause = (" AND " + " AND ".join(extra_where)) if extra_where else ""

    # `scope_clause` is the scope predicate by itself, inlined into queries
    # that don't ride on extra_clause (rolling trend, recent, activity strip).
    scope_clause = (" AND " + scope_sql) if scope_sql else ""

    conn = get_conn()
    try:
        # total_calls, avg_score, below_threshold (scored calls only)
        cur = conn.execute(
            q(f"""SELECT
                    COUNT(*) AS total_calls,
                    AVG(i.interaction_overall_score) AS avg_score,
                    COUNT(CASE WHEN i.interaction_overall_score < 5.0 THEN 1 END)
                        AS below_threshold
                 FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 WHERE p.company_id = ?
                   AND i.interaction_deleted_at IS NULL
                   AND i.interaction_is_test = FALSE
                   AND i.status_id <> ?
                   {extra_clause}"""),
            tuple([company_id, STATUS_NO_ANSWER, *extra_params]),
        )
        scored_row = _row_to_dict(cur.fetchone()) or {}
        avg_raw = scored_row.get("avg_score")
        avg_score = round(float(avg_raw), 1) if avg_raw is not None else None

        # no_answer_count (separately counted)
        cur = conn.execute(
            q(f"""SELECT COUNT(*) AS cnt FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                 WHERE p.company_id = ?
                   AND i.interaction_deleted_at IS NULL
                   AND i.interaction_is_test = FALSE
                   AND i.status_id = ?
                   {extra_clause}"""),
            tuple([company_id, STATUS_NO_ANSWER, *extra_params]),
        )
        no_answer_count = _scalar(_row_to_dict(cur.fetchone()), "cnt", 0)

        # active_projects (status 1, non-deleted) — intentionally NOT
        # filter-scoped; this is a live-state count, not a slice metric.
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM projects
                 WHERE company_id = ? AND status_id = 1
                   AND project_deleted_at IS NULL"""),
            (company_id,),
        )
        active_projects = _scalar(_row_to_dict(cur.fetchone()), "cnt", 0)

        # leaderboard: top 3 callers in scope, keyed on respondent_id so
        # same-named respondents at different locations remain distinct cards.
        # Each row is enriched with the home-location roll-up, a rolling
        # 30-day trend, most-recent-call timestamp, and a Performance Reports
        # deep-link by respondent_id (PR is 1:1 with respondent).
        # NULL / empty / 'Name Not Detected' names are excluded.
        cur = conn.execute(
            q(f"""SELECT
                    r.respondent_id,
                    TRIM(r.respondent_name) AS respondent_name,
                    r.location_id,
                    AVG(i.interaction_overall_score) AS avg_score,
                    COUNT(*) AS call_count
                 FROM interactions i
                 JOIN projects    p ON p.project_id    = i.project_id
                 JOIN respondents r ON r.respondent_id = i.respondent_id
                 WHERE p.company_id = ?
                   AND i.interaction_deleted_at IS NULL
                   AND i.interaction_is_test = FALSE
                   AND i.status_id <> ?
                   AND i.interaction_overall_score IS NOT NULL
                   AND r.respondent_name IS NOT NULL
                   AND TRIM(r.respondent_name) <> ''
                   AND TRIM(r.respondent_name) <> 'Name Not Detected'
                   {extra_clause}
                 GROUP BY r.respondent_id, r.respondent_name, r.location_id
                 ORDER BY avg_score DESC
                 LIMIT 10"""),
            tuple([company_id, STATUS_NO_ANSWER, *extra_params]),
        )
        rolling_start = date.today() - timedelta(days=30)
        leaderboard = []
        for row in _rows(cur):
            respondent_id = row["respondent_id"]
            name = row["respondent_name"]
            avg = row.get("avg_score")

            # Per-respondent in-scope detail → locations + last_call.
            cur2 = conn.execute(
                q(f"""SELECT
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
                       AND i.interaction_is_test = FALSE
                       AND i.status_id <> ?
                       AND i.interaction_overall_score IS NOT NULL
                       AND i.respondent_id = ?
                       {extra_clause}"""),
                tuple([company_id, STATUS_NO_ANSWER, respondent_id, *extra_params]),
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

            # Per-respondent rolling-30-day trend (independent of the calendar
            # month, so early-in-the-month dashboards still surface trend
            # signal from the prior weeks).
            cur3 = conn.execute(
                q(f"""SELECT
                        i.interaction_date,
                        i.interaction_overall_score
                     FROM interactions i
                     JOIN projects    p ON p.project_id    = i.project_id
                     WHERE p.company_id = ?
                       AND i.interaction_deleted_at IS NULL
                       AND i.interaction_is_test = FALSE
                       AND i.status_id <> ?
                       AND i.interaction_overall_score IS NOT NULL
                       AND i.interaction_date >= ?
                       AND i.respondent_id = ?
                       {scope_clause}"""),
                tuple([company_id, STATUS_NO_ANSWER, rolling_start, respondent_id, *scope_params]),
            )
            trend = _trend_for_calls(_rows(cur3))

            cur4 = conn.execute(
                q("""SELECT pr.performance_report_id
                     FROM performance_reports pr
                     WHERE pr.respondent_id = ?"""),
                (respondent_id,),
            )
            report_url = _report_url_for(name, _rows(cur4))

            leaderboard.append({
                "respondent_id":   respondent_id,
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
            q(f"""SELECT
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
                   AND i.interaction_is_test = FALSE
                   {scope_clause}
                 ORDER BY i.interaction_id DESC
                 LIMIT 8"""),
            tuple([company_id, *scope_params]),
        )
        recent = _rows(cur)

        # Derived: no_answer_rate = no_ans / (graded + no_ans). Null when
        # the denominator is zero (no terminal calls in scope). Mirrors
        # the list_locations pattern so per-tile and per-row math agree.
        graded_total = _scalar(scored_row, "total_calls", 0)
        nar_denom    = (graded_total or 0) + (no_answer_count or 0)
        no_answer_rate = (no_answer_count / nar_denom) if nar_denom else None

        # Activity strip (rolling 7d vs prior 7d). Intentionally NOT
        # filter-scoped — same rationale as active_projects above. Two
        # separate queries instead of COUNT(*) FILTER (which is PG-only) so
        # the SQLite path stays happy.
        today = date.today()
        this_week_start = today - timedelta(days=6)
        prior_start     = today - timedelta(days=13)
        prior_end       = today - timedelta(days=7)
        cur = conn.execute(
            q(f"""SELECT COUNT(*) AS cnt FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                WHERE p.company_id = ? AND i.interaction_deleted_at IS NULL
                  AND i.interaction_is_test = FALSE AND i.status_id <> ?
                  AND i.interaction_date >= ?
                  {scope_clause}"""),
            tuple([company_id, STATUS_NO_ANSWER, this_week_start, *scope_params]),
        )
        this_week_count = _scalar(_row_to_dict(cur.fetchone()), "cnt", 0)
        cur = conn.execute(
            q(f"""SELECT COUNT(*) AS cnt FROM interactions i
                 JOIN projects p ON p.project_id = i.project_id
                WHERE p.company_id = ? AND i.interaction_deleted_at IS NULL
                  AND i.interaction_is_test = FALSE AND i.status_id <> ?
                  AND i.interaction_date >= ? AND i.interaction_date <= ?
                  {scope_clause}"""),
            tuple([company_id, STATUS_NO_ANSWER, prior_start, prior_end, *scope_params]),
        )
        last_week_count = _scalar(_row_to_dict(cur.fetchone()), "cnt", 0)
        # Week-over-Week % intentionally removed — a percentage on a tiny
        # denominator produced whiplash figures in a prime stat slot. The
        # two raw counts below remain as a plain activity pulse.

        return jsonify({
            "stat_cards": {
                "total_calls":      _scalar(scored_row, "total_calls", 0),
                "avg_score":        avg_score,
                "below_threshold":  _scalar(scored_row, "below_threshold", 0),
                "no_answer_count":  no_answer_count,
                "no_answer_rate":   no_answer_rate,
                "active_projects":  active_projects,
            },
            "activity": {
                "this_week": this_week_count,
                "last_week": last_week_count,
            },
            "leaderboard":        leaderboard,
            "recent_interactions": recent,
        })
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# Dashboard insights (recurring-issues mini-report) — /api/dashboard/insights
# ═══════════════════════════════════════════════════════════════
#
# GET returns the cached row (or null on cold start) and triggers a background
# refresh when the cache is missing or older than 24h. The frontend renders
# stale content immediately and picks up fresh content on next page load.
#
# POST refreshes on demand. Admin-only — generation cost is non-trivial
# (Haiku call) and we don't want regular users hammering it.

_INSIGHTS_TTL = timedelta(hours=24)


def _is_stale(generated_at):
    if generated_at is None:
        return True
    if isinstance(generated_at, str):
        # SQLite returns strings; coerce to compare.
        try:
            generated_at = datetime.fromisoformat(generated_at)
        except ValueError:
            return True
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - generated_at) > _INSIGHTS_TTL


@dashboard_bp.route("/dashboard/insights", methods=["GET"])
@login_required
def get_dashboard_insights():
    company_id, err = _require_company()
    if err: return err

    cached = fetch_insights_cached(company_id)
    if cached is None or _is_stale(cached.get("di_generated_at")):
        compute_dashboard_insights_async(company_id)

    if cached is None:
        return jsonify({
            "report_markdown":  None,
            "calls_in_window":  0,
            "generated_at":     None,
            "is_generating":    True,
        })

    gen_at = cached.get("di_generated_at")
    return jsonify({
        "report_markdown":  cached.get("di_report_markdown"),
        "calls_in_window":  cached.get("di_calls_in_window") or 0,
        "generated_at":     gen_at.isoformat() if hasattr(gen_at, "isoformat") else gen_at,
        "is_generating":    _is_stale(gen_at),
    })


@dashboard_bp.route("/dashboard/insights/refresh", methods=["POST"])
@login_required
def refresh_dashboard_insights():
    company_id, err = _require_company()
    if err: return err
    if current_user.role not in ("admin", "super_admin"):
        return _err("Admin role required to refresh insights.", 403)
    compute_dashboard_insights_async(company_id)
    return jsonify({"ok": True, "is_generating": True}), 202


# ═══════════════════════════════════════════════════════════════
# GET /api/dashboard/filters
# ═══════════════════════════════════════════════════════════════


@dashboard_bp.route("/dashboard/filters", methods=["GET"])
@login_required
def get_filters():
    """Returns the dropdown options for the dashboard widget.

    Each list contains only entities that have actually appeared on at least
    one non-deleted interaction in the company (or in the given project, if
    project_id is provided). Phone routings include their location_id so the
    UI can narrow the phone_routing list when locations are selected.

    Campaigns are returned when project_id is provided, OR when the requester
    is a manager (RM) — campaigns are project-scoped (campaigns.project_id FK)
    and a company-wide list isn't a meaningful filter for an admin, but an RM
    is already row-scoped to their locations, so their campaign list is small
    and useful (and the sealed RM portal needs it without a project_id).
    Empty list otherwise.
    """
    company_id, err = _require_company()
    if err: return err

    project_id = request.args.get("project_id")

    base_filters = ["p.company_id = ?", "i.interaction_deleted_at IS NULL"]
    base_params = [company_id]
    if project_id:
        base_filters.append("i.project_id = ?")
        base_params.append(project_id)
    # Permission scope (ff_permission_filtering off → empty). Drives the
    # location dropdown options shown to the user; an RM should only see
    # their locations in the multi-select.
    scope_sql, scope_params = location_scope_for_user(
        current_user.user_id, current_user.role, company_id,
    )
    if scope_sql:
        base_filters.append(scope_sql)
        base_params.extend(scope_params)
    where = " AND ".join(base_filters)

    conn = get_conn()
    try:
        # locations: distinct locations directly attached to each interaction.
        # Uses i.interaction_location_id (the authoritative column) so we don't
        # drop rows when projects.phone_routing_id IS NULL (project_all_locations case).
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

        # phone_routings: distinct phone_routings reachable via project → phone_routing
        cur = conn.execute(
            q(f"""SELECT DISTINCT
                      phr.phone_routing_id, phr.phone_routing_name, phr.location_id
                  FROM interactions i
                  JOIN projects       p   ON p.project_id = i.project_id
                  JOIN phone_routing  phr ON phr.phone_routing_id = p.phone_routing_id
                  WHERE {where}
                  ORDER BY phr.phone_routing_name ASC"""),
            base_params,
        )
        phone_routings = _rows(cur)

        # campaigns: project-scoped for admins — a company-wide campaign list
        # isn't a meaningful filter (you'd need to know each campaign's
        # project). But a manager (RM) is already location-scoped via `where`,
        # so their campaign list is naturally small AND useful without a
        # project_id. Required so the sealed RM portal's campaign filter has
        # options at all, and so a shared-link campaign_id survives hydration
        # instead of being pruned back to "All Campaigns".
        campaigns = []
        if project_id or current_user.role == "manager":
            # Chronological by start date (the 1st of the campaign's month);
            # undated campaigns fall to the end, then alphabetical as a
            # tiebreaker. NULLS LAST sorts the dateless ones last; it's
            # DISTINCT-safe because campaign_start_date is in the select list.
            cur = conn.execute(
                q(f"""SELECT DISTINCT c.campaign_id, c.campaign_name,
                             c.campaign_start_date
                      FROM interactions i
                      JOIN projects   p ON p.project_id   = i.project_id
                      JOIN campaigns  c ON c.campaign_id  = i.campaign_id
                      WHERE {where}
                        AND c.campaign_deleted_at IS NULL
                      ORDER BY c.campaign_start_date ASC NULLS LAST,
                               c.campaign_name ASC"""),
                base_params,
            )
            campaigns = [
                {**r, "campaign_start_date": to_iso_date(r.get("campaign_start_date"))}
                for r in _rows(cur)
            ]

        return jsonify({
            "locations":      locations,
            "callers":        callers,
            "phone_routings": phone_routings,
            "campaigns":      campaigns,
        })
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# GET /api/dashboard/chart
# ═══════════════════════════════════════════════════════════════


_ALLOWED_METRICS = {"interaction_overall_score"}
_ALLOWED_VIEW_BY = {"date", "project", "respondent", "caller", "location", "phone_routing"}


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
    # phone_routing_ids). The singular `location_id` alias is preserved for
    # backcompat with bookmarked filter URLs from older eras.
    location_ids = _parse_id_list(request.args.get("location_ids"))
    caller_ids   = _parse_id_list(request.args.get("caller_ids"))
    phone_routing_ids = _parse_id_list(request.args.get("phone_routing_ids"))
    campaign_ids = _parse_id_list(request.args.get("campaign_ids"))

    legacy_loc = request.args.get("location_id")
    if legacy_loc and not location_ids:
        location_ids = _parse_id_list(legacy_loc)
    legacy_camp = request.args.get("campaign_id")
    if legacy_camp and not campaign_ids:
        campaign_ids = _parse_id_list(legacy_camp)

    filters = [
        "p.company_id = ?",
        "i.interaction_deleted_at IS NULL",
        "i.interaction_is_test = FALSE",
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
    # Location filter goes directly through i.interaction_location_id —
    # the canonical column for "which location did this call hit". Avoids
    # depending on the phone_routing chain (empty for tenants who don't
    # populate phone_routing) and on projects.phone_routing_id (NULL on
    # all-locations projects like Mayfair "Legacy V1").
    if location_ids:
        filters.append(f"i.interaction_location_id IN {_in_clause(len(location_ids))}")
        params.extend(location_ids)
    # Phone routing filter goes through projects.phone_routing_id directly —
    # no JOIN required to filter; only required when grouping (view_by=phone_routing).
    if phone_routing_ids:
        filters.append(f"p.phone_routing_id IN {_in_clause(len(phone_routing_ids))}")
        params.extend(phone_routing_ids)
    # Campaign filter — direct on i.campaign_id (no JOIN required).
    if campaign_ids:
        filters.append(f"i.campaign_id IN {_in_clause(len(campaign_ids))}")
        params.extend(campaign_ids)

    # Permission scope (ff_permission_filtering off → empty). All chart
    # branches build on `filters`/`params` so this lands in every view_by.
    # Note: _company_avg_score below is deliberately NOT scoped — the
    # threshold line is a peer benchmark, useful even for an RM seeing only
    # their slice of the chart.
    scope_sql, scope_params = location_scope_for_user(
        current_user.user_id, current_user.role, company_id,
    )
    if scope_sql:
        filters.append(scope_sql)
        params.extend(scope_params)

    # Joins are now strictly for grouping/display, never for filtering.
    # view_by=phone_routing needs phr.phone_routing_name; view_by=location
    # needs l.location_name. Other view_by modes touch neither table.
    phone_routing_join = (
        "JOIN phone_routing phr ON phr.phone_routing_id = p.phone_routing_id"
        if view_by == "phone_routing" else ""
    )
    locations_join = (
        "JOIN locations l ON l.location_id = i.interaction_location_id"
        if view_by == "location" else ""
    )

    where_clause = " AND ".join(filters)

    conn = get_conn()
    try:
        company_avg = _company_avg_score(conn, company_id)

        if view_by == "date":
            sql = f"""
                SELECT
                    i.interaction_id,
                    i.interaction_date,
                    i.{metric} AS score,
                    i.interaction_overall_assessment AS summary,
                    p.project_name,
                    (u.user_first_name || ' ' || u.user_last_name) AS respondent_name,
                    (cu.user_first_name || ' ' || cu.user_last_name) AS caller_name
                FROM interactions i
                JOIN projects p ON p.project_id = i.project_id
                {phone_routing_join}
                {locations_join}
                LEFT JOIN users u  ON u.user_id  = i.respondent_user_id
                LEFT JOIN users cu ON cu.user_id = i.caller_user_id
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
                    "caller_name":     row.get("caller_name"),
                    "summary":         _truncate_summary(row.get("summary")),
                })
                labels.append(d_str)
                data.append(score)
            return jsonify({
                "type":     "line",
                "labels":   labels,
                "datasets": [{"label": "Score", "data": data}],
                "points":   points,
                "company_avg": company_avg,
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
                {phone_routing_join}
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
                "company_avg": company_avg,
            })

        if view_by == "caller":
            sql = f"""
                SELECT
                    i.caller_user_id,
                    (u.user_first_name || ' ' || u.user_last_name) AS caller_name,
                    AVG(i.{metric}) AS avg_score,
                    COUNT(*) AS call_count
                FROM interactions i
                JOIN projects p ON p.project_id = i.project_id
                JOIN users u    ON u.user_id    = i.caller_user_id
                WHERE {where_clause}
                  AND i.caller_user_id IS NOT NULL
                GROUP BY i.caller_user_id, caller_name
                ORDER BY avg_score DESC
            """
            cur = conn.execute(q(sql), params)
            labels, data, points = [], [], []
            for row in _rows(cur):
                avg = row["avg_score"]
                avg = round(float(avg), 2) if avg is not None else None
                labels.append(row["caller_name"])
                data.append(avg)
                points.append({
                    "caller_user_id": row["caller_user_id"],
                    "caller_name":    row["caller_name"],
                    "avg_score":      avg,
                    "call_count":     row["call_count"],
                })
            return jsonify({
                "type":     "bar",
                "labels":   labels,
                "datasets": [{"label": "Avg Score", "data": data}],
                "points":   points,
                "company_avg": company_avg,
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
                {phone_routing_join}
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
                "company_avg": company_avg,
            })

        # view_by == "phone_routing"
        sql = f"""
            SELECT
                phr.phone_routing_id,
                phr.phone_routing_name,
                AVG(i.{metric}) AS avg_score,
                COUNT(*) AS call_count
            FROM interactions i
            JOIN projects p ON p.project_id = i.project_id
            {phone_routing_join}
            {locations_join}
            WHERE {where_clause}
            GROUP BY phr.phone_routing_id, phr.phone_routing_name
            ORDER BY avg_score DESC
        """
        cur = conn.execute(q(sql), params)
        labels, data, points = [], [], []
        for row in _rows(cur):
            avg = row["avg_score"]
            avg = round(float(avg), 2) if avg is not None else None
            labels.append(row["phone_routing_name"])
            data.append(avg)
            points.append({
                "phone_routing_id":   row["phone_routing_id"],
                "phone_routing_name": row["phone_routing_name"],
                "avg_score":          avg,
                "call_count":         row["call_count"],
            })
        return jsonify({
            "type":     "bar",
            "labels":   labels,
            "datasets": [{"label": "Avg Score", "data": data}],
            "points":   points,
            "company_avg": company_avg,
        })
    finally:
        conn.close()
