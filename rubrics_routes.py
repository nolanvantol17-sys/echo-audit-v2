"""
rubrics_routes.py — Echo Audit V2 Phase 4 rubric management routes.

Scope:
    /api/rubric-groups                         CRUD (soft-delete)
    /api/rubric-groups/<id>/items              CRUD (soft-delete)
    /api/rubric-groups/<id>/items/reorder      bulk ri_order update
    /api/rubric-groups/<id>/items/<id>/generate-guidance   Claude-backed
    /api/rubric-templates                       industry starters
    /api/rubric-templates/<key>/apply           instantiate a template

Every rubric group is tied to a location; company scope is enforced via
rubric_group.location_id → locations.company_id. Items inherit scope
through rubric_group_id.
"""

import logging
import os

import anthropic
from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from audit_log import (
    ACTION_CREATED, ACTION_DELETED, ACTION_UPDATED,
    ENTITY_RUBRIC_GROUP, ENTITY_RUBRIC_ITEM,
    write_audit_log,
)
from auth import role_required
from db import IS_POSTGRES, get_conn, q
from helpers import check_rate_limit, get_effective_company_id, increment_usage
from rubric_templates import RUBRIC_TEMPLATES, V1_TO_V2_SCORE_TYPE

logger = logging.getLogger(__name__)

rubrics_bp = Blueprint("rubrics", __name__, url_prefix="/api")

_VALID_SCORE_TYPES = ("out_of_10", "yes_no", "yes_no_pending")
_VALID_GRADE_TARGETS = ("caller", "respondent")


# ── Shared helpers ─────────────────────────────────────────────


def _err(msg, code):
    return jsonify({"error": msg}), code


def _body():
    return request.get_json(silent=True) or {}


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


def _get_location_in_company(conn, location_id, company_id):
    cur = conn.execute(
        q("""SELECT location_id FROM locations
             WHERE location_id = ? AND company_id = ?
               AND location_deleted_at IS NULL"""),
        (location_id, company_id),
    )
    return cur.fetchone() is not None


def _get_rubric_group_in_company(conn, rubric_group_id, company_id, include_deleted=False):
    """Return the rubric_group row if it belongs to the given company.

    Scope chain: rubric_group.location_id → locations.company_id. Industry
    templates (location_id IS NULL) are intentionally excluded — they are
    served separately via /api/rubric-templates.
    """
    deleted_clause = "" if include_deleted else " AND rg.rg_deleted_at IS NULL"
    cur = conn.execute(
        q(f"""SELECT rg.* FROM rubric_groups rg
              JOIN locations l ON l.location_id = rg.location_id
              WHERE rg.rubric_group_id = ? AND l.company_id = ?
                AND l.location_deleted_at IS NULL{deleted_clause}"""),
        (rubric_group_id, company_id),
    )
    return _row_to_dict(cur.fetchone())


def _get_rubric_item(conn, rubric_item_id, rubric_group_id):
    cur = conn.execute(
        q("""SELECT * FROM rubric_items
             WHERE rubric_item_id = ? AND rubric_group_id = ?
               AND ri_deleted_at IS NULL"""),
        (rubric_item_id, rubric_group_id),
    )
    return _row_to_dict(cur.fetchone())


def _insert_rubric_group(conn, *, location_id, rg_name, rg_grade_target, status_id=1):
    if IS_POSTGRES:
        cur = conn.execute(
            """INSERT INTO rubric_groups (location_id, rg_name, rg_grade_target, status_id)
               VALUES (%s, %s, %s, %s) RETURNING rubric_group_id""",
            (location_id, rg_name, rg_grade_target, status_id),
        )
        return cur.fetchone()["rubric_group_id"]
    conn.execute(
        "INSERT INTO rubric_groups (location_id, rg_name, rg_grade_target, status_id) "
        "VALUES (?, ?, ?, ?)",
        (location_id, rg_name, rg_grade_target, status_id),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_rubric_item(conn, *, rubric_group_id, ri_name, ri_score_type,
                        ri_weight=1.00, ri_scoring_guidance=None, ri_order=0):
    if IS_POSTGRES:
        cur = conn.execute(
            """INSERT INTO rubric_items
                   (rubric_group_id, ri_name, ri_score_type, ri_weight,
                    ri_scoring_guidance, ri_order)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING rubric_item_id""",
            (rubric_group_id, ri_name, ri_score_type, ri_weight,
             ri_scoring_guidance, ri_order),
        )
        return cur.fetchone()["rubric_item_id"]
    conn.execute(
        """INSERT INTO rubric_items
               (rubric_group_id, ri_name, ri_score_type, ri_weight,
                ri_scoring_guidance, ri_order)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (rubric_group_id, ri_name, ri_score_type, ri_weight,
         ri_scoring_guidance, ri_order),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ═══════════════════════════════════════════════════════════════
# RUBRIC GROUPS
# ═══════════════════════════════════════════════════════════════


@rubrics_bp.route("/rubric-groups", methods=["GET"])
@login_required
def list_rubric_groups():
    company_id, err = _require_company()
    if err: return err
    conn = get_conn()
    try:
        cur = conn.execute(
            q("""SELECT rg.rubric_group_id, rg.rg_name, rg.rg_grade_target,
                        rg.location_id, l.location_name, rg.status_id,
                        s.status_name, rg.rg_source_industry_id,
                        rg.rg_created_at, rg.rg_updated_at
                 FROM rubric_groups rg
                 JOIN locations l ON l.location_id = rg.location_id
                 LEFT JOIN statuses s ON s.status_id = rg.status_id
                 WHERE l.company_id = ?
                   AND rg.rg_deleted_at IS NULL
                   AND l.location_deleted_at IS NULL
                 ORDER BY l.location_name, rg.rg_name"""),
            (company_id,),
        )
        return jsonify(_rows(cur))
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def create_rubric_group():
    company_id, err = _require_company()
    if err: return err

    body = _body()
    rg_name = (body.get("rg_name") or "").strip()
    rg_grade_target = (body.get("rg_grade_target") or "").strip()
    location_id = body.get("location_id")

    if not rg_name or not rg_grade_target or not location_id:
        return _err("Rubric name, who we're grading, and location are all required.", 400)
    if rg_grade_target not in _VALID_GRADE_TARGETS:
        return _err("Please choose who you're grading: the person who placed the call or the person who answered the call.", 400)

    conn = get_conn()
    try:
        if not _get_location_in_company(conn, location_id, company_id):
            return _err("Location not found", 404)
        rubric_group_id = _insert_rubric_group(
            conn,
            location_id=location_id,
            rg_name=rg_name,
            rg_grade_target=rg_grade_target,
            status_id=1,
        )
        write_audit_log(
            current_user.user_id, ACTION_CREATED, ENTITY_RUBRIC_GROUP,
            rubric_group_id,
            metadata={"rg_name": rg_name, "rg_grade_target": rg_grade_target,
                      "location_id": location_id},
            conn=conn,
        )
        conn.commit()
        cur = conn.execute(
            q("SELECT * FROM rubric_groups WHERE rubric_group_id = ?"),
            (rubric_group_id,),
        )
        return jsonify(_row_to_dict(cur.fetchone())), 201
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>", methods=["PUT"])
@login_required
@role_required("admin", "super_admin")
def update_rubric_group(rubric_group_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    allowed = ("rg_name", "rg_grade_target", "location_id", "status_id")
    fields = {k: body[k] for k in allowed if k in body}

    if "rg_grade_target" in fields and fields["rg_grade_target"] not in _VALID_GRADE_TARGETS:
        return _err("Please choose who you're grading: the person who placed the call or the person who answered the call.", 400)

    conn = get_conn()
    try:
        existing = _get_rubric_group_in_company(conn, rubric_group_id, company_id)
        if not existing:
            return _err("Rubric not found", 404)

        # If location_id is being reassigned, verify the new location also belongs
        # to this company.
        if "location_id" in fields and fields["location_id"] != existing["location_id"]:
            if not _get_location_in_company(conn, fields["location_id"], company_id):
                return _err("Target location not found", 404)

        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            params = list(fields.values()) + [rubric_group_id]
            conn.execute(q(f"UPDATE rubric_groups SET {sets} WHERE rubric_group_id = ?"), params)
            write_audit_log(
                current_user.user_id, ACTION_UPDATED, ENTITY_RUBRIC_GROUP,
                rubric_group_id,
                metadata={"changes": fields},
                conn=conn,
            )

        conn.commit()
        cur = conn.execute(
            q("SELECT * FROM rubric_groups WHERE rubric_group_id = ?"),
            (rubric_group_id,),
        )
        return jsonify(_row_to_dict(cur.fetchone()))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>", methods=["DELETE"])
@login_required
@role_required("admin", "super_admin")
def delete_rubric_group(rubric_group_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        if not _get_rubric_group_in_company(conn, rubric_group_id, company_id):
            return _err("Rubric not found", 404)

        # Block if any active projects still reference this rubric group.
        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM projects
                 WHERE rubric_group_id = ? AND project_deleted_at IS NULL"""),
            (rubric_group_id,),
        )
        row = cur.fetchone()
        count = row["cnt"] if IS_POSTGRES else row[0]
        if count and count > 0:
            return _err("Cannot delete: rubric group is used by active projects", 409)

        if IS_POSTGRES:
            conn.execute(
                "UPDATE rubric_groups SET rg_deleted_at = NOW() "
                "WHERE rubric_group_id = %s",
                (rubric_group_id,),
            )
        else:
            conn.execute(
                "UPDATE rubric_groups SET rg_deleted_at = CURRENT_TIMESTAMP "
                "WHERE rubric_group_id = ?",
                (rubric_group_id,),
            )
        write_audit_log(
            current_user.user_id, ACTION_DELETED, ENTITY_RUBRIC_GROUP,
            rubric_group_id,
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>/deletion-impact", methods=["GET"])
@login_required
@role_required("admin", "super_admin")
def rubric_group_deletion_impact(rubric_group_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        rg = _get_rubric_group_in_company(conn, rubric_group_id, company_id)
        if not rg:
            return _err("Rubric not found", 404)

        cur = conn.execute(
            q("""SELECT COUNT(*) AS cnt FROM projects
                 WHERE rubric_group_id = ? AND project_deleted_at IS NULL"""),
            (rubric_group_id,),
        )
        row = cur.fetchone()
        projects_count = row["cnt"] if IS_POSTGRES else row[0]

        deletable = projects_count == 0
        payload = {
            "deletable": deletable,
            "name": rg.get("rg_name"),
            "counts": {"projects": projects_count},
        }
        if not deletable:
            payload["reason"] = "This rubric is used by active projects. Remove or reassign those projects first."
        return jsonify(payload)
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# RUBRIC ITEMS
# ═══════════════════════════════════════════════════════════════


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>/items", methods=["GET"])
@login_required
def list_rubric_items(rubric_group_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        if not _get_rubric_group_in_company(conn, rubric_group_id, company_id):
            return _err("Rubric not found", 404)
        cur = conn.execute(
            q("""SELECT * FROM rubric_items
                 WHERE rubric_group_id = ? AND ri_deleted_at IS NULL
                 ORDER BY ri_order ASC, rubric_item_id ASC"""),
            (rubric_group_id,),
        )
        return jsonify(_rows(cur))
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>/items", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def create_rubric_item(rubric_group_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    ri_name = (body.get("ri_name") or "").strip()
    ri_score_type = (body.get("ri_score_type") or "").strip()

    if not ri_name or not ri_score_type:
        return _err("Criterion name and score type are required.", 400)
    if ri_score_type not in _VALID_SCORE_TYPES:
        return _err("Score type must be 1–10 scale, Yes/No, or Yes/No/Pending.", 400)

    try:
        ri_weight = float(body.get("ri_weight", 1.00))
    except (TypeError, ValueError):
        return _err("Weight must be a number.", 400)
    if ri_weight <= 0:
        return _err("Weight must be greater than 0.", 400)

    ri_scoring_guidance = body.get("ri_scoring_guidance") or None
    try:
        ri_order = int(body.get("ri_order", 0))
    except (TypeError, ValueError):
        ri_order = 0

    conn = get_conn()
    try:
        if not _get_rubric_group_in_company(conn, rubric_group_id, company_id):
            return _err("Rubric not found", 404)
        rubric_item_id = _insert_rubric_item(
            conn,
            rubric_group_id=rubric_group_id,
            ri_name=ri_name,
            ri_score_type=ri_score_type,
            ri_weight=ri_weight,
            ri_scoring_guidance=ri_scoring_guidance,
            ri_order=ri_order,
        )
        write_audit_log(
            current_user.user_id, ACTION_CREATED, ENTITY_RUBRIC_ITEM,
            rubric_item_id,
            metadata={"rubric_group_id": rubric_group_id, "ri_name": ri_name,
                      "ri_score_type": ri_score_type, "ri_weight": ri_weight},
            conn=conn,
        )
        conn.commit()
        cur = conn.execute(
            q("SELECT * FROM rubric_items WHERE rubric_item_id = ?"),
            (rubric_item_id,),
        )
        return jsonify(_row_to_dict(cur.fetchone())), 201
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>/items/<int:rubric_item_id>",
                  methods=["PUT"])
@login_required
@role_required("admin", "super_admin")
def update_rubric_item(rubric_group_id, rubric_item_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    allowed = ("ri_name", "ri_score_type", "ri_weight",
               "ri_scoring_guidance", "ri_order", "status_id")
    fields = {k: body[k] for k in allowed if k in body}

    if "ri_score_type" in fields and fields["ri_score_type"] not in _VALID_SCORE_TYPES:
        return _err("Score type must be 1–10 scale, Yes/No, or Yes/No/Pending.", 400)
    if "ri_weight" in fields:
        try:
            fields["ri_weight"] = float(fields["ri_weight"])
        except (TypeError, ValueError):
            return _err("Weight must be a number.", 400)
        if fields["ri_weight"] <= 0:
            return _err("Weight must be greater than 0.", 400)

    conn = get_conn()
    try:
        if not _get_rubric_group_in_company(conn, rubric_group_id, company_id):
            return _err("Rubric not found", 404)
        if not _get_rubric_item(conn, rubric_item_id, rubric_group_id):
            return _err("Rubric item not found", 404)

        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            params = list(fields.values()) + [rubric_item_id]
            conn.execute(q(f"UPDATE rubric_items SET {sets} WHERE rubric_item_id = ?"), params)
            write_audit_log(
                current_user.user_id, ACTION_UPDATED, ENTITY_RUBRIC_ITEM,
                rubric_item_id,
                metadata={"rubric_group_id": rubric_group_id, "changes": fields},
                conn=conn,
            )

        conn.commit()
        cur = conn.execute(
            q("SELECT * FROM rubric_items WHERE rubric_item_id = ?"),
            (rubric_item_id,),
        )
        return jsonify(_row_to_dict(cur.fetchone()))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>/items/<int:rubric_item_id>",
                  methods=["DELETE"])
@login_required
@role_required("admin", "super_admin")
def delete_rubric_item(rubric_group_id, rubric_item_id):
    company_id, err = _require_company()
    if err: return err

    conn = get_conn()
    try:
        if not _get_rubric_group_in_company(conn, rubric_group_id, company_id):
            return _err("Rubric not found", 404)
        if not _get_rubric_item(conn, rubric_item_id, rubric_group_id):
            return _err("Rubric item not found", 404)

        if IS_POSTGRES:
            conn.execute(
                "UPDATE rubric_items SET ri_deleted_at = NOW() "
                "WHERE rubric_item_id = %s",
                (rubric_item_id,),
            )
        else:
            conn.execute(
                "UPDATE rubric_items SET ri_deleted_at = CURRENT_TIMESTAMP "
                "WHERE rubric_item_id = ?",
                (rubric_item_id,),
            )
        write_audit_log(
            current_user.user_id, ACTION_DELETED, ENTITY_RUBRIC_ITEM,
            rubric_item_id,
            metadata={"rubric_group_id": rubric_group_id},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>/items/reorder",
                  methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def reorder_rubric_items(rubric_group_id):
    """Bulk update ri_order. Body: [{rubric_item_id, ri_order}, ...].

    All items must belong to this rubric group — a single mismatch aborts the
    entire transaction so partial reorderings never land.
    """
    company_id, err = _require_company()
    if err: return err

    body = request.get_json(silent=True)
    if not isinstance(body, list):
        return _err("Request body must be a JSON array", 400)

    conn = get_conn()
    try:
        if not _get_rubric_group_in_company(conn, rubric_group_id, company_id):
            return _err("Rubric not found", 404)

        # Verify every item belongs to this group before any writes.
        for entry in body:
            item_id = entry.get("rubric_item_id")
            order = entry.get("ri_order")
            if item_id is None or order is None:
                return _err("Each entry must include rubric_item_id and ri_order", 400)
            if not _get_rubric_item(conn, item_id, rubric_group_id):
                return _err(f"rubric_item_id {item_id} not in this rubric group", 400)

        for entry in body:
            conn.execute(
                q("UPDATE rubric_items SET ri_order = ? WHERE rubric_item_id = ?"),
                (int(entry["ri_order"]), int(entry["rubric_item_id"])),
            )
        write_audit_log(
            current_user.user_id, ACTION_UPDATED, ENTITY_RUBRIC_GROUP,
            rubric_group_id,
            metadata={"reorder": body},
            conn=conn,
        )
        conn.commit()
        return jsonify({"ok": True, "reordered": len(body)})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════
# SCORING GUIDANCE GENERATION  (Claude)
# ═══════════════════════════════════════════════════════════════


_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


@rubrics_bp.route("/rubric-groups/<int:rubric_group_id>/items/<int:rubric_item_id>/generate-guidance",
                  methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def generate_guidance(rubric_group_id, rubric_item_id):
    company_id, err = _require_company()
    if err: return err

    body = _body()
    category_name = (body.get("category_name") or "").strip()
    call_type = (body.get("call_type") or "").strip()
    grade_target = (body.get("grade_target") or "respondent").strip()
    ai_context = (body.get("ai_context") or "").strip()
    agent_script = (body.get("agent_script") or "").strip()

    if not category_name:
        return _err("Category name is required.", 400)

    conn = get_conn()
    try:
        if not _get_rubric_group_in_company(conn, rubric_group_id, company_id):
            return _err("Rubric not found", 404)
        if not _get_rubric_item(conn, rubric_item_id, rubric_group_id):
            return _err("Rubric item not found", 404)
    finally:
        conn.close()

    ok, msg = check_rate_limit(company_id, "anthropic")
    if not ok:
        return _err(msg, 429)

    target_label = (
        "the person who answered the call" if grade_target in ("respondent", "answerer")
        else "the person who placed the call"
    )
    context_parts = []
    if call_type:
        context_parts.append(f"Call type: {call_type}")
    if ai_context:
        context_parts.append(f"Business context: {ai_context}")
    context_parts.append(f"The person being graded is {target_label}.")
    if agent_script:
        context_parts.append(f"Agent script excerpt: {agent_script[:500]}")
    context_block = "\n".join(context_parts) if context_parts else "General call center environment."

    prompt = f"""Generate scoring guidance for the rubric category "{category_name}" (scored 1-10).

Context:
{context_block}

Format the output exactly as:
9-10: [description]
7-8: [description]
5-6: [description]
3-4: [description]
1-2: [description]

Keep each description to 1-2 sentences. Be specific to the context provided. Make the guidance actionable."""

    try:
        resp = _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=("You are an expert in call center quality assurance and performance "
                    "management. A manager is setting up a call grading rubric and needs "
                    "scoring guidance for a specific category. Generate clear, specific "
                    "scoring guidance that describes what each score range means."),
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        guidance = resp.content[0].text.strip()
    except Exception:
        logger.exception("generate_guidance: Claude call failed")
        return _err("Scoring guidance generation failed. Please try again.", 502)

    increment_usage(company_id, "anthropic")
    return jsonify({"guidance": guidance})


# ═══════════════════════════════════════════════════════════════
# INDUSTRY TEMPLATES
# ═══════════════════════════════════════════════════════════════


@rubrics_bp.route("/rubric-templates", methods=["GET"])
@login_required
def list_rubric_templates():
    return jsonify(RUBRIC_TEMPLATES)


@rubrics_bp.route("/rubric-templates/<template_key>/apply", methods=["POST"])
@login_required
@role_required("admin", "super_admin")
def apply_rubric_template(template_key):
    company_id, err = _require_company()
    if err: return err

    template = RUBRIC_TEMPLATES.get(template_key)
    if not template:
        return _err("Unknown template", 404)

    body = _body()
    location_id = body.get("location_id")
    rg_name = (body.get("rg_name") or "").strip()
    if not location_id or not rg_name:
        return _err("Location and rubric name are both required.", 400)

    # Default grade_target: respondent, unless template declares otherwise.
    rg_grade_target = (body.get("rg_grade_target") or "respondent").strip()
    if rg_grade_target not in _VALID_GRADE_TARGETS:
        return _err("Please choose who you're grading: the person who placed the call or the person who answered the call.", 400)

    conn = get_conn()
    try:
        if not _get_location_in_company(conn, location_id, company_id):
            return _err("Location not found", 404)

        rubric_group_id = _insert_rubric_group(
            conn,
            location_id=location_id,
            rg_name=rg_name,
            rg_grade_target=rg_grade_target,
            status_id=1,
        )

        created_items = []
        for order, criterion in enumerate(template["criteria"]):
            v1_type = criterion.get("type", "numeric")
            ri_score_type = V1_TO_V2_SCORE_TYPE.get(v1_type, "out_of_10")
            item_id = _insert_rubric_item(
                conn,
                rubric_group_id=rubric_group_id,
                ri_name=criterion["name"],
                ri_score_type=ri_score_type,
                ri_weight=float(criterion.get("weight", 1.0)),
                ri_scoring_guidance=None,
                ri_order=order,
            )
            created_items.append(item_id)

        write_audit_log(
            current_user.user_id, ACTION_CREATED, ENTITY_RUBRIC_GROUP,
            rubric_group_id,
            metadata={"source": "template", "template_key": template_key,
                      "rg_name": rg_name, "location_id": location_id,
                      "item_ids": created_items},
            conn=conn,
        )
        conn.commit()

        cur = conn.execute(
            q("SELECT * FROM rubric_groups WHERE rubric_group_id = ?"),
            (rubric_group_id,),
        )
        return jsonify({
            "rubric_group": _row_to_dict(cur.fetchone()),
            "item_count": len(created_items),
        }), 201
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
