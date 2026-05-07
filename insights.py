"""
insights.py — Company-wide dashboard insights ("recurring issues" mini-report).

compute_dashboard_insights(company_id) is the public entrypoint. It scans the
last 30 days of graded calls, finds the lowest-scoring rubric items across the
entire tenant, and asks Claude Haiku to synthesize a 3-5 bullet narrative
naming the recurring weaknesses + which properties they cluster at.

Cached in dashboard_insights table; refresh on demand or after a 24h TTL via
compute_dashboard_insights_async() from request handlers.
"""

import logging
import os
import threading
from datetime import date, timedelta

import anthropic

from db import IS_POSTGRES, get_conn, q
from helpers import check_rate_limit, increment_usage

logger = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL = "claude-haiku-4-5-20251001"

_STATUS_NO_ANSWER = 44
_WINDOW_DAYS      = 30
_MIN_OCCURRENCES  = 5    # rubric items scored fewer times are excluded as noise
_MAX_ITEMS        = 8    # top-N worst items fed to Haiku
_MAX_PROPERTIES   = 5    # per-item property names included in the prompt


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


def compute_dashboard_insights_async(company_id):
    """Fire-and-forget background refresh. Safe to call from request handlers."""
    if not company_id:
        return
    t = threading.Thread(target=_compute_safe, args=(company_id,), daemon=True)
    t.start()


def _compute_safe(company_id):
    try:
        compute_dashboard_insights(company_id)
    except Exception:
        logger.exception("Background dashboard insights refresh failed (company=%s)", company_id)


def compute_dashboard_insights(company_id):
    """Refresh the dashboard_insights row for a company.

    Pulls the lowest-scoring rubric items from the last 30 days of graded calls,
    asks Claude Haiku for a plain-English mini-report, and upserts the row.

    Skipped when there are zero qualifying calls — leaves any prior cached row
    in place rather than overwriting with empty.
    """
    window_start = date.today() - timedelta(days=_WINDOW_DAYS)
    aggregates, calls_in_window = _aggregate_weak_items(company_id, window_start)

    if not aggregates:
        logger.info(
            "Dashboard insights: no qualifying weak items for company=%s; skipping report.",
            company_id,
        )
        return

    report_md = _generate_report(company_id, aggregates, calls_in_window)
    if report_md is None:
        # AI call skipped/failed — preserve any existing cached row.
        return

    _upsert(company_id, report_md, calls_in_window)


def _aggregate_weak_items(company_id, window_start):
    """Returns (aggregates_list, calls_in_window).

    Each aggregate item:
      {name, score_type, occurrences, avg_score,
       low_at: [{location_name, avg_at_loc, calls_at_loc, sample_explanation}, ...]}
    """
    conn = get_conn()
    try:
        # Total graded calls in window so the report can cite a denominator.
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt
                   FROM interactions i
                   JOIN projects p ON p.project_id = i.project_id
                  WHERE p.company_id = ?
                    AND i.interaction_deleted_at IS NULL
                    AND i.interaction_is_test = FALSE
                    AND i.status_id <> ?
                    AND i.interaction_overall_score IS NOT NULL
                    AND i.interaction_date >= ?"""),
            (company_id, _STATUS_NO_ANSWER, window_start),
        )
        calls_in_window = (_row_to_dict(cur.fetchone()) or {}).get("cnt") or 0
        if calls_in_window == 0:
            return ([], 0)

        # Group on snapshot name (not rubric_item_id) so renamed/replaced items
        # still cluster together — the snapshot is what the user actually saw.
        cur = conn.execute(
            q("""SELECT
                    irs.irs_snapshot_name             AS name,
                    irs.irs_snapshot_score_type       AS score_type,
                    COUNT(*)                          AS occurrences,
                    AVG(irs.irs_score_value)          AS avg_score
                  FROM interaction_rubric_scores irs
                  JOIN interactions i ON i.interaction_id = irs.interaction_id
                  JOIN projects     p ON p.project_id     = i.project_id
                 WHERE p.company_id = ?
                   AND i.interaction_deleted_at IS NULL
                   AND i.interaction_is_test = FALSE
                   AND i.status_id <> ?
                   AND i.interaction_date >= ?
                 GROUP BY irs.irs_snapshot_name, irs.irs_snapshot_score_type
                HAVING COUNT(*) >= ?
                 ORDER BY avg_score ASC
                 LIMIT ?"""),
            (company_id, _STATUS_NO_ANSWER, window_start, _MIN_OCCURRENCES, _MAX_ITEMS),
        )
        rows = [_row_to_dict(r) for r in cur.fetchall()]

        aggregates = []
        for row in rows:
            name = row["name"]
            cur2 = conn.execute(
                q("""SELECT
                        loc.location_name                 AS location_name,
                        AVG(irs.irs_score_value)          AS avg_at_loc,
                        COUNT(*)                          AS calls_at_loc,
                        MIN(irs.irs_score_ai_explanation) AS sample_explanation
                       FROM interaction_rubric_scores irs
                       JOIN interactions i  ON i.interaction_id = irs.interaction_id
                       JOIN projects     p  ON p.project_id     = i.project_id
                       LEFT JOIN locations loc ON loc.location_id = i.interaction_location_id
                      WHERE p.company_id = ?
                        AND i.interaction_deleted_at IS NULL
                        AND i.interaction_is_test = FALSE
                        AND i.status_id <> ?
                        AND i.interaction_date >= ?
                        AND irs.irs_snapshot_name = ?
                      GROUP BY loc.location_name
                      ORDER BY avg_at_loc ASC, calls_at_loc DESC
                      LIMIT ?"""),
                (company_id, _STATUS_NO_ANSWER, window_start, name, _MAX_PROPERTIES),
            )
            per_loc = [_row_to_dict(r) for r in cur2.fetchall()]
            aggregates.append({
                "name":        name,
                "score_type":  row["score_type"],
                "occurrences": row["occurrences"],
                "avg_score":   float(row["avg_score"]) if row["avg_score"] is not None else None,
                "low_at":      per_loc,
            })
        return (aggregates, calls_in_window)
    finally:
        conn.close()


def _generate_report(company_id, aggregates, calls_in_window):
    """Ask Haiku for a 3-5 bullet markdown report. Returns markdown string or None."""
    ok, _msg = check_rate_limit(company_id, "anthropic")
    if not ok:
        logger.warning(
            "Skipping dashboard insights — anthropic rate limit hit (company=%s)", company_id,
        )
        return None

    items_block_parts = []
    for agg in aggregates:
        avg = agg["avg_score"]
        avg_str = f"{avg:.2f}" if avg is not None else "n/a"
        score_basis = (
            "(0-9.9 numeric scale)" if agg["score_type"] == "out_of_10"
            else "(yes/no — 0 means failed, 9.9 means passed)"
        )
        loc_lines = []
        for loc in agg["low_at"]:
            ex = (loc.get("sample_explanation") or "").strip()
            ex_brief = (ex[:300] + "…") if len(ex) > 300 else ex
            loc_lines.append(
                f"  - {loc.get('location_name') or 'Unknown property'} "
                f"(avg {float(loc['avg_at_loc']):.2f}, {loc['calls_at_loc']} calls)"
                + (f": {ex_brief}" if ex_brief else "")
            )
        items_block_parts.append(
            f"RUBRIC ITEM: {agg['name']}\n"
            f"Type: {agg['score_type']} {score_basis}\n"
            f"Across the company: avg {avg_str} over {agg['occurrences']} occurrences\n"
            "Worst-performing properties:\n"
            + ("\n".join(loc_lines) if loc_lines else "  (no per-location breakdown)")
        )
    items_block = "\n\n".join(items_block_parts)

    prompt = (
        "You are reviewing the most common rubric weaknesses across a company's "
        f"last {_WINDOW_DAYS} days of secret-shopping calls "
        f"({calls_in_window} graded calls total). Below is the data for the "
        "worst-performing rubric items.\n\n"
        "WRITE A SHORT, ACTIONABLE REPORT IN MARKDOWN with these rules:\n"
        "- 3 to 5 bullets, no preamble or summary paragraph.\n"
        "- Each bullet starts with '- ' and a single short sentence describing the "
        "recurring weakness in plain English (NOT the rubric item's literal name — "
        "reword it as a problem the leasing team is having).\n"
        "- After each bullet, add a sub-bullet '  - Most common at: <Property>, <Property>, ...' "
        "listing 1-4 actual property names from the data. If no real property names "
        "are present, omit the sub-bullet.\n"
        "- Skip rubric items that don't suggest a real coachable problem "
        "(e.g. items with very few occurrences or near-perfect scores).\n"
        "- Tone: direct, regional-manager voice. Don't use hedge words. Don't quote "
        "the AI explanations verbatim — synthesize.\n"
        "- DO NOT include any text outside the bulleted list.\n\n"
        "DATA:\n\n"
        f"{items_block}"
    )

    try:
        response = _claude.messages.create(
            model=_MODEL,
            max_tokens=900,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        report = response.content[0].text.strip()
    except Exception:
        logger.exception("Dashboard insights Haiku call failed (company=%s)", company_id)
        return None

    increment_usage(company_id, "anthropic")
    return report or None


def _upsert(company_id, report_md, calls_in_window):
    conn = get_conn()
    try:
        if IS_POSTGRES:
            conn.execute(
                """INSERT INTO dashboard_insights (
                       company_id, di_calls_in_window, di_report_markdown, di_generated_at
                   ) VALUES (%s, %s, %s, NOW())
                   ON CONFLICT (company_id) DO UPDATE SET
                       di_calls_in_window = EXCLUDED.di_calls_in_window,
                       di_report_markdown = EXCLUDED.di_report_markdown,
                       di_generated_at    = NOW()
                """,
                (company_id, calls_in_window, report_md),
            )
        else:
            conn.execute(
                """INSERT OR REPLACE INTO dashboard_insights (
                       company_id, di_calls_in_window, di_report_markdown, di_generated_at
                   ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)""",
                (company_id, calls_in_window, report_md),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_cached(company_id):
    """Return the cached dashboard_insights row as a dict, or None if missing."""
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT di_calls_in_window, di_report_markdown, di_generated_at
                   FROM dashboard_insights
                  WHERE company_id = ?"""),
            (company_id,),
        )
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()
