"""
explore_query.py — Shared interaction-query builder for the Ad Hoc Explore page.

One builder serves every report variation. Report-specific code picks columns,
group_by, sort_by; the builder emits parameterized SQL against a fixed join
graph and a fixed tenancy + soft-delete + no-answer + test-flag preamble.

Phase 1 callers: /api/explore/site-history.
Phase 2 callers: /api/explore/regional-performance, plus overlay endpoints.
Phase 3 callers: /api/v1/reports/*, with scope_clause wired from
helpers.location_scope_for_user(...) to enforce RM/VP visibility limits.

The join graph is always-emitted; Postgres's planner drops the LEFT JOINs that
no column in the request references. Keeping the FROM clause static means
every consumer composes SQL the same way and the only point of variation is
the column / filter expression whitelists below.
"""

from db import q


# ── Whitelists ─────────────────────────────────────────────────
# Each value is the SQL expression substituted into SELECT / GROUP BY /
# ORDER BY. The key is the public name accepted from the route layer.

COLUMN_EXPRESSIONS = {
    "interaction_id":     "i.interaction_id",
    "interaction_date":   "i.interaction_date",
    "score":              "i.interaction_overall_score",
    "summary":            "i.interaction_overall_assessment",
    "strengths":          "i.interaction_strengths",
    "weaknesses":         "i.interaction_weaknesses",
    "location_id":        "l.location_id",
    "location_name":      "l.location_name",
    "campaign_id":        "c.campaign_id",
    "campaign_name":      "c.campaign_name",
    "project_id":         "p.project_id",
    "project_name":       "p.project_name",
    "caller_user_id":     "i.caller_user_id",
    "caller_name":        ("TRIM(caller_u.user_first_name || ' ' || "
                           "caller_u.user_last_name)"),
    "respondent_user_id": "i.respondent_user_id",
    "respondent_name":    ("COALESCE("
                           "TRIM(resp_u.user_first_name || ' ' || "
                           "resp_u.user_last_name), "
                           "r.respondent_name)"),
    "avg_score":          "AVG(i.interaction_overall_score)",
    "call_count":         "COUNT(*)",
}

# Sort directions are normalized to ASC / DESC here so route handlers
# can pass "asc"/"desc" without case anxiety.
ALLOWED_SORT_DIRS = {"asc": "ASC", "desc": "DESC"}


def _column_sql(name):
    """Return the SQL expression for a public column name, or raise."""
    if name not in COLUMN_EXPRESSIONS:
        raise ValueError(f"explore_query: unknown column {name!r}")
    return COLUMN_EXPRESSIONS[name]


# ── The builder ────────────────────────────────────────────────


def build_interaction_query(
    company_id,
    *,
    columns,
    location_ids=None,
    user_ids=None,           # matches either caller OR respondent
    caller_user_ids=None,
    respondent_user_ids=None,
    campaign_ids=None,
    project_id=None,
    date_from=None,
    date_to=None,
    score_min=None,
    score_max=None,
    group_by=None,
    order_by=None,           # iterable of (column_name, "asc"|"desc")
    limit=None,
    scope_clause=None,       # ("AND ...", [params]) tuple from
                             # helpers.location_scope_for_user — Phase 3 wiring
):
    """Build a parameterized SQL string + params list against the fixed
    interaction join graph.

    Required:
      - company_id     — tenancy filter
      - columns        — list of public column names to project

    Returns: (sql, params)
    """
    if not isinstance(columns, (list, tuple)) or not columns:
        raise ValueError("build_interaction_query: columns is required")

    select_exprs = [_column_sql(c) + " AS " + c for c in columns]

    # ── Fixed join graph ──
    # Always emit; Postgres planner drops unused LEFT JOINs.
    from_clause = (
        "FROM interactions i "
        "JOIN projects p ON p.project_id = i.project_id "
        "LEFT JOIN campaigns c ON c.campaign_id = i.campaign_id "
        "LEFT JOIN locations l ON l.location_id = i.interaction_location_id "
        "LEFT JOIN users caller_u ON caller_u.user_id = i.caller_user_id "
        "LEFT JOIN users resp_u ON resp_u.user_id = i.respondent_user_id "
        "LEFT JOIN respondents r ON r.respondent_id = i.respondent_id "
    )

    # ── Mandatory preamble ──
    # tenancy + soft-delete + test-flag + no-answer (status 44) excluded
    where_parts = [
        "p.company_id = ?",
        "i.interaction_deleted_at IS NULL",
        "i.interaction_is_test = FALSE",
        "i.status_id <> 44",
    ]
    params = [company_id]

    # ── Optional filters ──
    if location_ids:
        placeholders = ",".join(["?"] * len(location_ids))
        where_parts.append(f"i.interaction_location_id IN ({placeholders})")
        params.extend(location_ids)

    if user_ids:
        placeholders = ",".join(["?"] * len(user_ids))
        where_parts.append(
            f"(i.caller_user_id IN ({placeholders}) "
            f"OR i.respondent_user_id IN ({placeholders}))"
        )
        params.extend(user_ids)
        params.extend(user_ids)

    if caller_user_ids:
        placeholders = ",".join(["?"] * len(caller_user_ids))
        where_parts.append(f"i.caller_user_id IN ({placeholders})")
        params.extend(caller_user_ids)

    if respondent_user_ids:
        placeholders = ",".join(["?"] * len(respondent_user_ids))
        where_parts.append(f"i.respondent_user_id IN ({placeholders})")
        params.extend(respondent_user_ids)

    if campaign_ids:
        placeholders = ",".join(["?"] * len(campaign_ids))
        where_parts.append(f"i.campaign_id IN ({placeholders})")
        params.extend(campaign_ids)

    if project_id:
        where_parts.append("i.project_id = ?")
        params.append(project_id)

    if date_from:
        where_parts.append("i.interaction_date >= ?")
        params.append(date_from)

    if date_to:
        where_parts.append("i.interaction_date <= ?")
        params.append(date_to)

    if score_min is not None:
        where_parts.append("i.interaction_overall_score >= ?")
        params.append(score_min)

    if score_max is not None:
        where_parts.append("i.interaction_overall_score <= ?")
        params.append(score_max)

    # Permission filtering — Phase 3 wires it; Phase 1/2 pass None.
    if scope_clause:
        clause_sql, clause_params = scope_clause
        if clause_sql:
            # Caller is responsible for the leading "AND " in the fragment.
            # We trim it if present so we can join with " AND " uniformly.
            cleaned = clause_sql.strip()
            if cleaned.upper().startswith("AND "):
                cleaned = cleaned[4:].strip()
            where_parts.append(cleaned)
            params.extend(clause_params)

    where_sql = " WHERE " + " AND ".join(where_parts)

    # ── GROUP BY ──
    group_sql = ""
    if group_by:
        group_exprs = [_column_sql(g) for g in group_by]
        group_sql = " GROUP BY " + ", ".join(group_exprs)

    # ── ORDER BY ──
    order_sql = ""
    if order_by:
        order_parts = []
        for col, direction in order_by:
            d = ALLOWED_SORT_DIRS.get(str(direction).lower())
            if d is None:
                raise ValueError(
                    f"build_interaction_query: bad sort direction {direction!r}"
                )
            order_parts.append(_column_sql(col) + " " + d)
        if order_parts:
            order_sql = " ORDER BY " + ", ".join(order_parts)

    # ── LIMIT ──
    limit_sql = ""
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("build_interaction_query: limit must be a non-negative int")
        limit_sql = f" LIMIT {limit}"

    sql = (
        "SELECT " + ", ".join(select_exprs) + " "
        + from_clause
        + where_sql
        + group_sql
        + order_sql
        + limit_sql
    )
    return q(sql), params
