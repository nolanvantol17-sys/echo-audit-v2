"""
dashboard_helpers.py — Shared pure helpers for dashboard / hub aggregations.

Lives outside any blueprint so both `dashboard_routes.py` (global /app
dashboard) and `api_routes.py` (project hub /app/projects/<id>/hub) can
import the same logic without one route module depending on the other.

These functions take plain row dicts and return plain values — no Flask,
no DB, no globals. Safe to call from anywhere.
"""

from collections import Counter
from datetime import date, datetime
from urllib.parse import quote


def _month_bounds(today=None):
    """Return (start_date, end_date_exclusive) for the calendar month of `today`."""
    today = today or date.today()
    start = today.replace(day=1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start, end


def _to_date(v):
    """Coerce a Postgres date/datetime or SQLite ISO-string into a date.
    Returns None if it can't be parsed."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _roll_up_locations(call_rows):
    """Return distinct location names from a list of call dicts, ordered by
    call frequency descending (then alphabetical for ties)."""
    names = [r.get("location_name") for r in call_rows if r.get("location_name")]
    if not names:
        return []
    counts = Counter(names)
    return sorted(counts.keys(), key=lambda n: (-counts[n], n))


def _trend_for_calls(call_rows):
    """Compute trend ('up' / 'down' / 'flat') from a list of dated, scored
    calls — or None if the floor isn't met (≥4 calls AND ≥48h between
    earliest and latest call). Compares first-half vs second-half average;
    ±0.3 thresholds."""
    scored = [r for r in call_rows
              if r.get("interaction_overall_score") is not None]
    if len(scored) < 4:
        return None

    dated = [(_to_date(r.get("interaction_date")), r) for r in scored]
    dated = [(d, r) for d, r in dated if d is not None]
    if len(dated) < 4:
        return None

    span_hours = (max(d for d, _ in dated) - min(d for d, _ in dated)).total_seconds() / 3600
    if span_hours < 48:
        return None

    dated.sort(key=lambda pair: pair[0])
    mid = len(dated) // 2
    first = [float(r["interaction_overall_score"]) for _, r in dated[:mid]]
    last  = [float(r["interaction_overall_score"]) for _, r in dated[mid:]]
    diff = (sum(last) / len(last)) - (sum(first) / len(first))
    if diff >=  0.3: return "up"
    if diff <= -0.3: return "down"
    return "flat"


def _report_url_for(name, pr_rows):
    """Pick the deep-link URL for an aggregated caller name based on how many
    performance reports exist under that name in the company.

    - 0 reports → None (frontend renders a plain name, no link)
    - 1 report  → /app/reports?report=<id>     (auto-opens the detail view)
    - >1        → /app/reports?focus_name=<n>  (filters list to that name)
    """
    if not pr_rows:
        return None
    if len(pr_rows) == 1:
        return "/app/reports?report=" + str(pr_rows[0]["performance_report_id"])
    return "/app/reports?focus_name=" + quote(name)
