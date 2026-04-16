"""
platform_routes.py — Echo Audit V2 Phase 4 platform-level usage route.

Super-admin visibility into per-company API usage. No company scoping — this
is cross-tenant by design, hence the super_admin role gate.
"""

from datetime import datetime

from flask import Blueprint, jsonify
from flask_login import login_required

from auth import role_required
from db import get_conn, q
from helpers import RATE_LIMITS

platform_bp = Blueprint("platform", __name__, url_prefix="/api/platform")


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except Exception:
        return {k: row[k] for k in row.keys()}


@platform_bp.route("/usage", methods=["GET"])
@login_required
@role_required("super_admin")
def platform_usage():
    """Per-company × service API usage for today AND this month.

    Phase 6 enhancement over the original Phase 4 surface:
      - Adds a monthly aggregation alongside the daily one
      - Adds percentage-of-daily-limit per service
      - Sets `flagged=true` on companies at or above 80% of any daily limit

    Response shape is backward compatible with Phase 4 callers — `limits`
    and `companies[*].usage` are still present.
    """
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)

    services = ("assemblyai", "anthropic", "twilio")
    limits = {svc: RATE_LIMITS.get((svc, "day"), 0) for svc in services}

    conn = get_conn()
    try:
        # Daily snapshot (exact period match)
        cur = conn.execute(
            q("""SELECT au.company_id, c.company_name,
                        au.au_service, au.au_request_count
                 FROM api_usage au
                 JOIN companies c ON c.company_id = au.company_id
                 WHERE au.au_period_type = 'day'
                   AND au.au_period_start = ?"""),
            (today_start,),
        )
        daily_rows = [_row_to_dict(r) for r in cur.fetchall()]

        # Monthly aggregate across all day-buckets in this month
        cur = conn.execute(
            q("""SELECT au.company_id, c.company_name,
                        au.au_service, SUM(au.au_request_count) AS total
                 FROM api_usage au
                 JOIN companies c ON c.company_id = au.company_id
                 WHERE au.au_period_type = 'day'
                   AND au.au_period_start >= ?
                 GROUP BY au.company_id, c.company_name, au.au_service"""),
            (month_start,),
        )
        monthly_rows = [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    companies = {}
    for r in daily_rows:
        cid = r["company_id"]
        comp = companies.setdefault(cid, {
            "company_id":       cid,
            "company_name":     r["company_name"],
            "usage":            {svc: 0 for svc in services},  # today (legacy field)
            "usage_today":      {svc: 0 for svc in services},
            "usage_month":      {svc: 0 for svc in services},
            "percent_of_daily": {svc: 0.0 for svc in services},
            "flagged":          False,
        })
        svc = r["au_service"]
        if svc in services:
            count = int(r.get("au_request_count") or 0)
            comp["usage"][svc] = count
            comp["usage_today"][svc] = count
    for r in monthly_rows:
        cid = r["company_id"]
        comp = companies.setdefault(cid, {
            "company_id":       cid,
            "company_name":     r["company_name"],
            "usage":            {svc: 0 for svc in services},
            "usage_today":      {svc: 0 for svc in services},
            "usage_month":      {svc: 0 for svc in services},
            "percent_of_daily": {svc: 0.0 for svc in services},
            "flagged":          False,
        })
        svc = r["au_service"]
        if svc in services:
            comp["usage_month"][svc] = int(r.get("total") or 0)

    for comp in companies.values():
        for svc in services:
            limit = limits[svc]
            if limit > 0:
                pct = (comp["usage_today"][svc] / limit) * 100.0
                comp["percent_of_daily"][svc] = round(pct, 1)
                if pct >= 80.0:
                    comp["flagged"] = True

    return jsonify({
        "date":      today_start.date().isoformat(),
        "month_start": month_start.date().isoformat(),
        "limits":    limits,
        "companies": list(companies.values()),
    })
