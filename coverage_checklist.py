"""
coverage_checklist.py — Coverage Checklist read model.

Per-campaign, per-property "how many calls does this property still need."
See COVERAGE_CHECKLIST_SPEC.md.

Progress is DERIVED from interactions (never stored). Targets come from
campaign_location_targets (absent row => default 1; target_calls = 0 => the
property is excluded from that campaign's checklist).

Box rule (locked with product): for a property with target N boxes, greens fill
first, then reds, capped at N:

    green = min(answered, N)
    red   = min(no_answer, N - green)
    empty = N - green - red
    covered = (N > 0 and green == N)

A no-answer shows a red box; an answered call shows a green box and REPLACES a red
one (greens are laid down before reds). "Answered" is counted LIVE — the moment a
real call is logged — grading state doesn't matter. no_answer = interaction
status 44.
"""

import logging

from db import get_conn, q

logger = logging.getLogger(__name__)

DEFAULT_TARGET = 1
STATUS_NO_ANSWER = 44


# ── Pure box math (no DB — unit-tested in test_coverage_checklist.py) ──────────

def coverage_boxes(target, answered, no_answer):
    """Compute the box state for one property. Pure function.

    Returns a dict: target, answered, no_answer, green, red, empty, covered,
    excluded. green+red+empty always == max(target, 0).
    """
    target = max(0, int(target))
    answered = max(0, int(answered or 0))
    no_answer = max(0, int(no_answer or 0))

    green = min(answered, target)
    red = min(no_answer, target - green)
    empty = target - green - red

    return {
        "target": target,
        "answered": answered,
        "no_answer": no_answer,
        "green": green,
        "red": red,
        "empty": empty,
        "covered": target > 0 and green >= target,
        "excluded": target == 0,
    }


# ── DB layer ──────────────────────────────────────────────────────────────────

def _targets_for(conn, campaign_id, location_ids):
    """{location_id: target_calls} for the explicit rows in this campaign. Any
    location without a row defaults to DEFAULT_TARGET at the call site."""
    if not location_ids:
        return {}
    ph = ",".join(["?"] * len(location_ids))
    rows = conn.execute(
        q(f"""SELECT location_id, target_calls
              FROM campaign_location_targets
              WHERE campaign_id = ? AND location_id IN ({ph})"""),
        (campaign_id, *location_ids),
    ).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["location_id"]] = r["target_calls"]
        except (KeyError, TypeError, IndexError):
            out[r[0]] = r[1]
    return out


def _counts_for(conn, campaign_id, location_ids):
    """{location_id: (answered, no_answer)} from interactions in this campaign.

    answered  = live, non-deleted, non-test calls that are NOT a no-answer.
    no_answer = interaction status 44.

    NOTE (tunable): "answered" currently counts any non-no-answer interaction row.
    If abandoned/never-submitted draft rows ever turn out to exist, tighten this
    with `AND interaction_submitted_at IS NOT NULL`. Verified against real data
    before the UI ships.
    """
    if not location_ids:
        return {}
    ph = ",".join(["?"] * len(location_ids))
    rows = conn.execute(
        q(f"""SELECT interaction_location_id AS location_id,
                     SUM(CASE WHEN status_id <> ? THEN 1 ELSE 0 END) AS answered,
                     SUM(CASE WHEN status_id  = ? THEN 1 ELSE 0 END) AS no_answer
              FROM interactions
              WHERE campaign_id = ?
                AND interaction_location_id IN ({ph})
                AND interaction_deleted_at IS NULL
                AND interaction_is_test = ?
              GROUP BY interaction_location_id"""),
        (STATUS_NO_ANSWER, STATUS_NO_ANSWER, campaign_id, *location_ids, False),
    ).fetchall()
    out = {}
    for r in rows:
        try:
            lid = r["location_id"]
            answered = r["answered"] or 0
            no_answer = r["no_answer"] or 0
        except (KeyError, TypeError, IndexError):
            lid, answered, no_answer = r[0], (r[1] or 0), (r[2] or 0)
        out[lid] = (int(answered), int(no_answer))
    return out


def coverage_for_campaign(campaign_id, location_ids, conn=None):
    """Return {location_id: coverage_boxes(...)} for the given properties in this
    campaign.

    `location_ids` is the property universe the caller wants annotated (e.g. the
    campaign's project locations, which the UI already has when it renders the
    property picker). Any location without an explicit target row defaults to 1.
    """
    if not location_ids:
        return {}
    location_ids = [int(x) for x in location_ids]

    own = conn is None
    if own:
        conn = get_conn()
    try:
        targets = _targets_for(conn, campaign_id, location_ids)
        counts = _counts_for(conn, campaign_id, location_ids)
    finally:
        if own:
            conn.close()

    out = {}
    for lid in location_ids:
        target = targets.get(lid, DEFAULT_TARGET)
        answered, no_answer = counts.get(lid, (0, 0))
        out[lid] = coverage_boxes(target, answered, no_answer)
    return out


def campaign_coverage_summary(campaign_id, location_ids, conn=None):
    """Rollup over the given universe: how many properties are on the checklist
    (target > 0) and how many are fully covered. Powers a 'covered X / Y' badge."""
    per = coverage_for_campaign(campaign_id, location_ids, conn=conn)
    on_checklist = [v for v in per.values() if v["target"] > 0]
    covered = [v for v in on_checklist if v["covered"]]
    return {
        "on_checklist": len(on_checklist),
        "covered": len(covered),
        "remaining": len(on_checklist) - len(covered),
    }
