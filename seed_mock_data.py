#!/usr/bin/env python3
"""
seed_mock_data.py — Populate Echo Audit V2 with realistic sample data
so we can visualize how the tables connect without a UI.

Idempotent: skips insertion if "Mayfair Management" or "AutoNation Dallas"
already exists in the companies table.

Prerequisites:
    - Schema created (run schema.sql)
    - Defaults seeded (run db.seed_defaults)
    - DATABASE_URL environment variable set
    - bcrypt and psycopg2-binary installed

Usage:
    DATABASE_URL=postgres://... python3 seed_mock_data.py
"""

import os
import json
import sys
from datetime import date, datetime

try:
    import bcrypt
    import psycopg2
    import psycopg2.extras
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install bcrypt psycopg2-binary")
    sys.exit(1)


DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL environment variable not set")
    sys.exit(1)


def get_conn():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if "sslmode" not in url and "localhost" not in url:
        url += "?sslmode=require" if "?" not in url else "&sslmode=require"
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_id(cur, sql, params, id_col):
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[id_col] if row else None


PASSWORD_HASH = bcrypt.hashpw(b"TestPass2026!", bcrypt.gensalt()).decode("utf-8")


# ── Status ID constants (mirror seed_defaults) ───────────────────
STATUS_ACTIVE                         = 1
STATUS_INACTIVE                       = 2
STATUS_COMPANY_SUSPENDED              = 10
STATUS_COMPANY_CHURNED                = 11
STATUS_USER_PENDING                   = 20
STATUS_PROJECT_COMPLETED              = 30
STATUS_PROJECT_ARCHIVED               = 31
STATUS_INTERACTION_TRANSCRIBING       = 40
STATUS_INTERACTION_AWAITING_CLARIFY   = 41
STATUS_INTERACTION_GRADING            = 42
STATUS_INTERACTION_GRADED             = 43
STATUS_INTERACTION_NO_ANSWER          = 44
STATUS_INTERACTION_PENDING            = 45
STATUS_API_KEY_REVOKED                = 50

PROJECT_STATUS_MAP = {
    "active":    STATUS_ACTIVE,
    "completed": STATUS_PROJECT_COMPLETED,
    "archived":  STATUS_PROJECT_ARCHIVED,
}

INTERACTION_STATUS_MAP = {
    "graded":    STATUS_INTERACTION_GRADED,
    "no_answer": STATUS_INTERACTION_NO_ANSWER,
    "pending":   STATUS_INTERACTION_PENDING,
}

# ── Audit log lookup IDs ─────────────────────────────────────────
ACTION_IMPERSONATE = None  # 'impersonate_company' wasn't in the spec's seed list
ACTION_REGRADED    = 5
ACTION_CREATED     = 1
ACTION_UPDATED     = 2

TARGET_COMPANY     = 5
TARGET_INTERACTION = 2


# ── Data blueprint ────────────────────────────────────────────────

MAYFAIR = {
    "company_name": "Mayfair Management",
    "industry":     "Property Management",
    "engagement_date": date(2026, 1, 15),
    "departments": ["Leasing Department", "Maintenance Department"],
    "locations": [
        ("Desert Villas", "(915) 779-3380"),
        ("Fonseca",       "(915) 779-3380"),
        ("Dahlia Villas", "(956) 278-3970"),
    ],
    "users": [
        ("Nolan",  "Van Tol",   "nolan@mayfair.example",   "admin",   "Leasing Department"),
        ("Jacob",  "Cervantes", "jacob@mayfair.example",   "manager", "Leasing Department"),
        ("Jordan", "Cervantes", "jordan@mayfair.example",  "caller",  "Leasing Department"),
        ("Carlos", "Cepeda",    "carlos@mayfair.example",  "caller",  "Leasing Department"),
    ],
    "respondents": [
        ("Toya",   "Williams"),
        ("Teresa", "Vela"),
    ],
    "rubric_source_name": "Leasing Call Evaluation",
    "campaign_name":      "Secret Shopping",
    "projects": [
        # Projects link to campaigns, which are per-location. We'll attach
        # projects to the FIRST location's campaign for simplicity.
        ("April Secret Shopping", date(2026, 4, 1), date(2026, 4, 30), "active"),
        ("March Secret Shopping", date(2026, 3, 1), date(2026, 3, 31), "completed"),
    ],
}

AUTONATION = {
    "company_name": "AutoNation Dallas",
    "industry":     "Auto Dealership",
    "engagement_date": date(2026, 3, 1),
    "departments": ["Sales Department", "Finance Department"],
    "locations": [
        ("North Dallas Location", "(214) 555-0101"),
        ("Plano Location",        "(972) 555-0188"),
        ("Irving Location",       "(469) 555-0177"),
    ],
    "users": [
        ("Sarah", "Mitchell", "sarah@autonation-dallas.example", "admin",   "Sales Department"),
        ("Mike",  "Torres",   "mike@autonation-dallas.example",  "manager", "Sales Department"),
        ("Dana",  "Reyes",    "dana@autonation-dallas.example",  "caller",  "Sales Department"),
        ("Alex",  "Rivera",   "alex@autonation-dallas.example",  "caller",  "Finance Department"),
    ],
    "respondents": [
        ("Mark",     "Stevens"),
        ("Jennifer", "Walsh"),
        ("David",    "Chen"),
    ],
    "rubric_source_name": None,  # No Auto Dealership template in seeds
    "rubric_custom_items": [
        ("Greeting & Rapport",          "out_of_10", 1.0, 0),
        ("Needs Discovery",             "out_of_10", 1.0, 1),
        ("Inventory Knowledge",         "out_of_10", 1.0, 2),
        ("Pricing & Financing Clarity", "out_of_10", 1.0, 3),
        ("Test Drive Offer",            "out_of_10", 1.5, 4),
        ("Closing & Next Steps",        "out_of_10", 1.0, 5),
        ("Appointment Scheduled",       "yes_no",    2.0, 6),
    ],
    "campaign_name": "Inbound Sales",
    "projects": [
        ("Q1 Sales Review", date(2026, 1, 1), date(2026, 3, 31), "completed"),
        ("Q2 Sales Review", date(2026, 4, 1), date(2026, 6, 30), "active"),
    ],
}


MAYFAIR_INTERACTIONS = [
    {
        "location": "Desert Villas",
        "respondent_name": ("Toya", "Williams"),
        "project": "April Secret Shopping",
        "overall_score": 6.2,
        "submitted_at": datetime(2026, 4, 7, 14, 34),
        "call_outcome": "graded",
        "scores": {
            "Greeting & Tone":         (7, "Agent answered promptly with warm greeting but did not introduce themselves."),
            "Qualification Questions": (6, "Asked about move-in date and bedrooms; missed pet and budget."),
            "Property Knowledge":      (8, "Accurate pricing and unit-layout answers."),
            "Availability & Pricing":  (8, "Quoted two available units with correct pricing."),
            "Appointment Setting":     (5, "Mentioned tours but did not propose a time."),
            "Urgency & Follow-Up":     (3, "No urgency conveyed."),
            "Closing & Next Steps":    (5, "Said 'call back if interested.' No defined next step."),
            "Overall Impression":      (6, "Professional but passive."),
            "Appointment Secured":     (0, "No tour scheduled."),
            "Contact Info Collected":  (10, "Name and phone captured."),
        },
        "clarifying": [
            ("Did the agent offer to schedule a tour before ending the call?",
             "I could not determine whether a tour was explicitly offered or only mentioned",
             "yes_no", "No", 1),
            ("How would you rate the agent's overall urgency in trying to convert?",
             "Transcript showed information delivery but not motivational intent",
             "scale_1_10", "3", 2),
        ],
    },
    {
        "location": "Fonseca",
        "respondent_name": None,
        "project": "April Secret Shopping",
        "overall_score": None,
        "submitted_at": datetime(2026, 4, 9, 10, 15),
        "call_outcome": "no_answer",
        "scores": {}, "clarifying": [],
    },
    {
        "location": "Dahlia Villas",
        "respondent_name": ("Teresa", "Vela"),
        "project": "April Secret Shopping",
        "overall_score": 7.8,
        "submitted_at": datetime(2026, 4, 9, 11, 22),
        "call_outcome": "graded",
        "scores": {
            "Greeting & Tone":         (9, "Excellent greeting with name and property introduction."),
            "Qualification Questions": (8, "Covered all key qualification points."),
            "Property Knowledge":      (9, "Strong command of features and amenities."),
            "Availability & Pricing":  (8, "Clear pricing and honest availability."),
            "Appointment Setting":     (7, "Proposed a specific tour time."),
            "Urgency & Follow-Up":     (7, "Mentioned limited availability and follow-up."),
            "Closing & Next Steps":    (8, "Confirmed follow-up plan."),
            "Overall Impression":      (8, "Engaged and consultative."),
            "Appointment Secured":     (0, "Tour proposed but not confirmed."),
            "Contact Info Collected":  (10, "Full contact info captured."),
        },
        "clarifying": [],
    },
    {
        "location": "Desert Villas",
        "respondent_name": ("Toya", "Williams"),
        "project": "April Secret Shopping",
        "overall_score": 7.1,
        "submitted_at": datetime(2026, 4, 9, 13, 45),
        "call_outcome": "graded",
        "regraded_with_context": True,
        "reviewer_context": "Background noise from caller side affected speed of answer score",
        "scores": {
            "Greeting & Tone":         (8, "Prompt answer with polite greeting."),
            "Qualification Questions": (7, "Asked most qualifying questions."),
            "Property Knowledge":      (9, "Excellent detail on floor plans."),
            "Availability & Pricing":  (8, "Provided current pricing and honest availability."),
            "Appointment Setting":     (6, "Suggested touring but did not schedule."),
            "Urgency & Follow-Up":     (4, "Low urgency."),
            "Closing & Next Steps":    (7, "Promised to send unit details via email."),
            "Overall Impression":      (7, "Solid information delivery, improving sales posture."),
            "Appointment Secured":     (0, "No appointment booked."),
            "Contact Info Collected":  (10, "Full contact info captured."),
        },
        "clarifying": [],
    },
    {
        "location": "Fonseca",
        "respondent_name": None,
        "project": "April Secret Shopping",
        "overall_score": 4.4,
        "submitted_at": datetime(2026, 4, 9, 15, 30),
        "call_outcome": "graded",
        "scores": {
            "Greeting & Tone":         (5, "Rushed greeting, no self-introduction."),
            "Qualification Questions": (4, "Only asked move-in date."),
            "Property Knowledge":      (6, "Basic pricing and availability."),
            "Availability & Pricing":  (6, "Pricing quoted accurately."),
            "Appointment Setting":     (3, "Did not propose a tour."),
            "Urgency & Follow-Up":     (2, "No urgency."),
            "Closing & Next Steps":    (3, "Ended call abruptly."),
            "Overall Impression":      (4, "Call felt transactional."),
            "Appointment Secured":     (0, "No tour scheduled."),
            "Contact Info Collected":  (10, "Caller name and phone captured."),
        },
        "clarifying": [],
    },
]

AUTONATION_INTERACTIONS = [
    {
        "location": "North Dallas Location",
        "respondent_name": ("Mark", "Stevens"),
        "project": "Q2 Sales Review",
        "overall_score": 7.5,
        "submitted_at": datetime(2026, 4, 3, 9, 12),
        "call_outcome": "graded",
        "scores": {
            "Greeting & Rapport":          (8, "Warm greeting with self-introduction."),
            "Needs Discovery":              (7, "Asked about use case and budget."),
            "Inventory Knowledge":          (8, "Strong command of trims and incentives."),
            "Pricing & Financing Clarity":  (7, "Walked through MSRP and financing."),
            "Test Drive Offer":             (8, "Offered a specific test drive window."),
            "Closing & Next Steps":         (7, "Confirmed follow-up text."),
            "Appointment Scheduled":        (10, "Test drive booked for the following day."),
        },
        "clarifying": [],
    },
    {
        "location": "Plano Location",
        "respondent_name": None,
        "project": "Q2 Sales Review",
        "overall_score": None,
        "submitted_at": datetime(2026, 4, 4, 10, 30),
        "call_outcome": "no_answer",
        "scores": {}, "clarifying": [],
    },
    {
        "location": "Irving Location",
        "respondent_name": ("Jennifer", "Walsh"),
        "project": "Q2 Sales Review",
        "overall_score": 8.2,
        "submitted_at": datetime(2026, 4, 5, 14, 5),
        "call_outcome": "graded",
        "scores": {
            "Greeting & Rapport":          (9, "Exceptional rapport-building."),
            "Needs Discovery":              (8, "Thorough discovery including trade-in."),
            "Inventory Knowledge":          (9, "Recited features and comparisons confidently."),
            "Pricing & Financing Clarity":  (8, "Transparent about fees and rebates."),
            "Test Drive Offer":             (8, "Proposed same-day and weekend options."),
            "Closing & Next Steps":         (8, "Confirmed appointment and emailed summary."),
            "Appointment Scheduled":        (10, "Test drive booked same-day."),
        },
        "clarifying": [],
    },
    {
        "location": "North Dallas Location",
        "respondent_name": ("David", "Chen"),
        "project": "Q2 Sales Review",
        "overall_score": 5.8,
        "submitted_at": datetime(2026, 4, 6, 11, 40),
        "call_outcome": "graded",
        "scores": {
            "Greeting & Rapport":          (6, "Polite but generic greeting."),
            "Needs Discovery":              (5, "Skipped most discovery questions."),
            "Inventory Knowledge":          (7, "Answered inventory questions accurately."),
            "Pricing & Financing Clarity":  (6, "Financing glossed over."),
            "Test Drive Offer":             (5, "Mentioned vaguely, no specific time."),
            "Closing & Next Steps":         (5, "No defined follow-up plan."),
            "Appointment Scheduled":        (0, "No test drive scheduled."),
        },
        "clarifying": [],
    },
    {
        "location": "Plano Location",
        "respondent_name": None,
        "project": "Q2 Sales Review",
        "overall_score": None,
        "submitted_at": datetime(2026, 4, 7, 16, 20),
        "call_outcome": "no_answer",
        "scores": {}, "clarifying": [],
    },
]


def seed(conn):
    cur = conn.cursor()
    counts = {}

    cur.execute(
        "SELECT company_id FROM companies WHERE company_name IN (%s, %s)",
        (MAYFAIR["company_name"], AUTONATION["company_name"]),
    )
    if cur.fetchall():
        print("Mock data already present — skipping.")
        return None

    industry_ids = {}
    for name in ("Property Management", "Auto Dealership", "HVAC"):
        iid = fetch_id(cur, "SELECT industry_id FROM industries WHERE industry_name = %s", (name,), "industry_id")
        if not iid:
            cur.execute("INSERT INTO industries (industry_name) VALUES (%s) RETURNING industry_id", (name,))
            iid = cur.fetchone()["industry_id"]
            counts["industries"] = counts.get("industries", 0) + 1
        industry_ids[name] = iid

    role_ids = {}
    for rname in ("super_admin", "admin", "manager", "caller", "respondent"):
        rid = fetch_id(cur, "SELECT role_id FROM roles WHERE role_name = %s", (rname,), "role_id")
        role_ids[rname] = rid

    for blueprint in (MAYFAIR, AUTONATION):
        seed_company(cur, blueprint, industry_ids, role_ids, counts)

    seed_api_usage(cur, counts)
    seed_audit_log(cur, counts)

    conn.commit()
    return counts


def get_or_create_user_role(cur, role_id):
    """Create a user_roles row wrapping a specific role_id and return its PK."""
    cur.execute(
        "SELECT user_role_id FROM user_roles WHERE role_id = %s LIMIT 1",
        (role_id,)
    )
    row = cur.fetchone()
    if row:
        return row["user_role_id"]
    cur.execute(
        "INSERT INTO user_roles (role_id) VALUES (%s) RETURNING user_role_id",
        (role_id,)
    )
    return cur.fetchone()["user_role_id"]


def seed_company(cur, bp, industry_ids, role_ids, counts):
    # Company
    cur.execute(
        """INSERT INTO companies (industry_id, company_name, status_id, company_engagement_date)
           VALUES (%s, %s, %s, %s) RETURNING company_id""",
        (industry_ids[bp["industry"]], bp["company_name"], STATUS_ACTIVE, bp["engagement_date"]),
    )
    company_id = cur.fetchone()["company_id"]
    counts["companies"] = counts.get("companies", 0) + 1

    # Departments
    department_ids = {}
    for dname in bp["departments"]:
        cur.execute(
            """INSERT INTO departments (company_id, department_name, status_id)
               VALUES (%s, %s, %s) RETURNING department_id""",
            (company_id, dname, STATUS_ACTIVE),
        )
        department_ids[dname] = cur.fetchone()["department_id"]
        counts["departments"] = counts.get("departments", 0) + 1

    # Locations
    location_ids = {}
    for (lname, phone) in bp["locations"]:
        cur.execute(
            """INSERT INTO locations (company_id, location_name, location_phone, status_id, location_engagement_date)
               VALUES (%s, %s, %s, %s, %s) RETURNING location_id""",
            (company_id, lname, phone, STATUS_ACTIVE, bp["engagement_date"]),
        )
        location_ids[lname] = cur.fetchone()["location_id"]
        counts["locations"] = counts.get("locations", 0) + 1

    # Campaigns (per-location now). Create one campaign per location using bp["campaign_name"].
    # Projects will reference the FIRST location's campaign (simplest mapping given the blueprint).
    campaign_ids_by_location = {}
    for lname, lid in location_ids.items():
        cur.execute(
            """INSERT INTO campaigns (location_id, campaign_name)
               VALUES (%s, %s) RETURNING campaign_id""",
            (lid, bp["campaign_name"]),
        )
        campaign_ids_by_location[lname] = cur.fetchone()["campaign_id"]
        counts["campaigns"] = counts.get("campaigns", 0) + 1

    # Pick the first location's campaign as the project-level campaign
    first_location_name = bp["locations"][0][0]
    project_campaign_id = campaign_ids_by_location[first_location_name]
    first_location_id = location_ids[first_location_name]

    # Rubric group — deep copy industry template (Mayfair) or build custom (AutoNation)
    rubric_group_id = None
    rubric_item_ids_by_name = {}

    if bp["rubric_source_name"]:
        cur.execute(
            """SELECT rubric_group_id, rg_grade_target FROM rubric_groups
               WHERE rg_name = %s AND location_id IS NULL""",
            (bp["rubric_source_name"],),
        )
        template = cur.fetchone()
        if template:
            tmpl_gid = template["rubric_group_id"]
            grade_target = template["rg_grade_target"]

            cur.execute(
                """INSERT INTO rubric_groups
                       (location_id, rg_name, rg_grade_target, rg_source_industry_id)
                   VALUES (%s, %s, %s, %s) RETURNING rubric_group_id""",
                (first_location_id, bp["rubric_source_name"], grade_target, industry_ids[bp["industry"]]),
            )
            rubric_group_id = cur.fetchone()["rubric_group_id"]
            counts["rubric_groups"] = counts.get("rubric_groups", 0) + 1

            cur.execute(
                """SELECT ri_name, ri_score_type, ri_weight, ri_scoring_guidance, ri_order
                   FROM rubric_items WHERE rubric_group_id = %s ORDER BY ri_order""",
                (tmpl_gid,),
            )
            for item in cur.fetchall():
                cur.execute(
                    """INSERT INTO rubric_items
                           (rubric_group_id, ri_name, ri_score_type, ri_weight, ri_scoring_guidance, ri_order)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING rubric_item_id""",
                    (rubric_group_id, item["ri_name"], item["ri_score_type"],
                     item["ri_weight"], item["ri_scoring_guidance"], item["ri_order"]),
                )
                rubric_item_ids_by_name[item["ri_name"]] = cur.fetchone()["rubric_item_id"]
                counts["rubric_items"] = counts.get("rubric_items", 0) + 1

    if rubric_group_id is None and bp.get("rubric_custom_items"):
        cur.execute(
            """INSERT INTO rubric_groups
                   (location_id, rg_name, rg_grade_target, rg_source_industry_id)
               VALUES (%s, %s, 'respondent', %s) RETURNING rubric_group_id""",
            (first_location_id, f"{bp['company_name']} Sales Rubric", industry_ids[bp["industry"]]),
        )
        rubric_group_id = cur.fetchone()["rubric_group_id"]
        counts["rubric_groups"] = counts.get("rubric_groups", 0) + 1

        for (iname, score_type, weight, order) in bp["rubric_custom_items"]:
            cur.execute(
                """INSERT INTO rubric_items
                       (rubric_group_id, ri_name, ri_score_type, ri_weight, ri_order)
                   VALUES (%s, %s, %s, %s, %s) RETURNING rubric_item_id""",
                (rubric_group_id, iname, score_type, weight, order),
            )
            rubric_item_ids_by_name[iname] = cur.fetchone()["rubric_item_id"]
            counts["rubric_items"] = counts.get("rubric_items", 0) + 1

    # Projects
    project_ids = {}
    for (pname, start, end, status) in bp["projects"]:
        cur.execute(
            """INSERT INTO projects
                   (company_id, project_name, campaign_id, rubric_group_id,
                    project_start_date, project_end_date, status_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING project_id""",
            (company_id, pname, project_campaign_id, rubric_group_id, start, end,
             PROJECT_STATUS_MAP[status]),
        )
        project_ids[pname] = cur.fetchone()["project_id"]
        counts["projects"] = counts.get("projects", 0) + 1

    # Users (employees)
    user_ids = {}
    for (first, last, email, rolename, deptname) in bp["users"]:
        user_role_id = get_or_create_user_role(cur, role_ids[rolename])
        cur.execute(
            """INSERT INTO users
                   (user_role_id, department_id, user_email, user_password_hash,
                    user_first_name, user_last_name, status_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING user_id""",
            (user_role_id, department_ids[deptname], email, PASSWORD_HASH, first, last, STATUS_ACTIVE),
        )
        uid = cur.fetchone()["user_id"]
        user_ids[(first, last)] = uid
        counts["users"] = counts.get("users", 0) + 1

    # Respondents (detected users)
    respondent_user_role_id = get_or_create_user_role(cur, role_ids["respondent"])
    for (first, last) in bp["respondents"]:
        email = f"{first.lower()}.{last.lower()}@detected.echoaudit.local"
        cur.execute(
            """INSERT INTO users
                   (user_role_id, user_email, user_first_name, user_last_name, status_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING user_id""",
            (respondent_user_role_id, email, first, last, STATUS_ACTIVE),
        )
        uid = cur.fetchone()["user_id"]
        user_ids[(first, last)] = uid
        counts["users"] = counts.get("users", 0) + 1

    # Interactions
    interactions_blueprint = (
        MAYFAIR_INTERACTIONS if bp is MAYFAIR else AUTONATION_INTERACTIONS
    )
    caller_key = ("Nolan", "Van Tol") if bp is MAYFAIR else ("Dana", "Reyes")
    caller_user_id = user_ids[caller_key]

    interaction_id_map = {}
    for idx, ix in enumerate(interactions_blueprint):
        respondent_user_id = user_ids[ix["respondent_name"]] if ix["respondent_name"] else None
        cur.execute(
            """INSERT INTO interactions (
                   project_id, caller_user_id, respondent_user_id,
                   interaction_date, interaction_submitted_at, status_id,
                   interaction_audio_url, interaction_overall_score, interaction_original_score,
                   interaction_regrade_count, interaction_regraded_with_context, interaction_reviewer_context
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING interaction_id""",
            (
                project_ids[ix["project"]],
                caller_user_id,
                respondent_user_id,
                ix["submitted_at"].date(),
                ix["submitted_at"],
                INTERACTION_STATUS_MAP[ix["call_outcome"]],
                None,  # audio_url
                ix.get("overall_score"),
                ix.get("overall_score"),
                1 if ix.get("regraded_with_context") else 0,
                bool(ix.get("regraded_with_context", False)),
                ix.get("reviewer_context"),
            ),
        )
        interaction_id = cur.fetchone()["interaction_id"]
        interaction_id_map[idx] = interaction_id
        counts["interactions"] = counts.get("interactions", 0) + 1

        # Rubric scores (graded only)
        if ix["call_outcome"] == "graded" and ix["scores"]:
            for item_name, (score_val, explanation) in ix["scores"].items():
                rubric_item_id = rubric_item_ids_by_name.get(item_name)
                if not rubric_item_id:
                    continue
                cur.execute(
                    """SELECT ri_name, ri_score_type, ri_weight, ri_scoring_guidance
                       FROM rubric_items WHERE rubric_item_id = %s""",
                    (rubric_item_id,),
                )
                snap = cur.fetchone()
                cur.execute(
                    """INSERT INTO interaction_rubric_scores (
                           interaction_id, rubric_item_id,
                           irs_snapshot_name, irs_snapshot_score_type,
                           irs_snapshot_weight, irs_snapshot_scoring_guidance,
                           irs_score_value, irs_score_ai_explanation
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        interaction_id, rubric_item_id,
                        snap["ri_name"], snap["ri_score_type"],
                        snap["ri_weight"], snap["ri_scoring_guidance"],
                        score_val, explanation,
                    ),
                )
                counts["interaction_rubric_scores"] = counts.get("interaction_rubric_scores", 0) + 1

        # Clarifying questions
        for (qtext, qreason, fmt, answer, order) in ix.get("clarifying", []):
            cur.execute(
                """INSERT INTO clarifying_questions (
                       interaction_id, cq_text, cq_ai_reason,
                       cq_response_format, cq_answer_value, cq_order
                   ) VALUES (%s, %s, %s, %s, %s, %s)""",
                (interaction_id, qtext, qreason, fmt, answer, order),
            )
            counts["clarifying_questions"] = counts.get("clarifying_questions", 0) + 1

    # Performance report for Toya Williams (Mayfair only)
    if bp is MAYFAIR and ("Toya", "Williams") in user_ids:
        toya_id = user_ids[("Toya", "Williams")]
        toya_interactions = [
            interaction_id_map[i] for i, ix in enumerate(MAYFAIR_INTERACTIONS)
            if ix["call_outcome"] == "graded"
            and ix["respondent_name"] == ("Toya", "Williams")
        ]
        report_data = {
            "strengths": [
                "Consistently provides accurate product information including pricing and availability",
                "Maintains a polite and professional tone throughout calls",
            ],
            "weaknesses": [
                "Does not proactively offer tours — waits for caller to ask",
                "Urgency and sales motivation are consistently low across both calls",
            ],
            "coaching_recommendations": [
                "Practice the tour offer script — every call should end with a direct tour invitation",
                "Work on creating urgency around availability",
            ],
            "trend_data": {
                "scores_by_call": [6.2, 7.1],
                "trend_direction": "improving",
            },
        }
        cur.execute(
            """INSERT INTO performance_reports (
                   subject_user_id, pr_data, pr_average_score,
                   pr_call_count, pr_processed_interaction_ids
               ) VALUES (%s, %s, %s, %s, %s)""",
            (toya_id, json.dumps(report_data), 6.65, 2, json.dumps(toya_interactions)),
        )
        counts["performance_reports"] = counts.get("performance_reports", 0) + 1


def seed_api_usage(cur, counts):
    cur.execute("SELECT company_id FROM companies WHERE company_name = %s", (MAYFAIR["company_name"],))
    row = cur.fetchone()
    if not row:
        return
    mayfair_id = row["company_id"]

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for (service, count) in [("assemblyai", 5), ("anthropic", 5), ("twilio", 1)]:
        cur.execute(
            """INSERT INTO api_usage
                   (company_id, au_service, au_period_start, au_period_type, au_request_count)
               VALUES (%s, %s, %s, 'day', %s)""",
            (mayfair_id, service, today_start, count),
        )
        counts["api_usage"] = counts.get("api_usage", 0) + 1


def seed_audit_log(cur, counts):
    # Find Nolan
    cur.execute("SELECT user_id FROM users WHERE user_email = %s", ("nolan@mayfair.example",))
    nolan_row = cur.fetchone()
    nolan_id = nolan_row["user_id"] if nolan_row else None

    # Find Mayfair company
    cur.execute("SELECT company_id FROM companies WHERE company_name = %s", (MAYFAIR["company_name"],))
    mayfair_row = cur.fetchone()
    mayfair_id = mayfair_row["company_id"] if mayfair_row else None

    # Find the regraded interaction
    cur.execute(
        """SELECT interaction_id FROM interactions
           WHERE interaction_regraded_with_context = TRUE
           ORDER BY interaction_created_at DESC LIMIT 1"""
    )
    regrade_row = cur.fetchone()
    regraded_iid = regrade_row["interaction_id"] if regrade_row else None

    # Entry 1: super admin impersonation — map to 'updated' since 'impersonate_company'
    # isn't in the seeded action types. Target = company.
    cur.execute(
        """INSERT INTO audit_log (
               actor_user_id, audit_log_action_type_id,
               audit_log_target_entity_type_id, al_target_entity_id, al_metadata
           ) VALUES (%s, %s, %s, %s, %s)""",
        (
            None, ACTION_UPDATED, TARGET_COMPANY, str(mayfair_id) if mayfair_id else None,
            json.dumps({
                "action_detail": "impersonate_company",
                "ip": "10.0.1.42",
                "user_agent": "Mozilla/5.0",
            }),
        ),
    )
    counts["audit_log"] = counts.get("audit_log", 0) + 1

    # Entry 2: Nolan regraded an interaction
    cur.execute(
        """INSERT INTO audit_log (
               actor_user_id, audit_log_action_type_id,
               audit_log_target_entity_type_id, al_target_entity_id, al_metadata
           ) VALUES (%s, %s, %s, %s, %s)""",
        (
            nolan_id, ACTION_REGRADED, TARGET_INTERACTION,
            str(regraded_iid) if regraded_iid else None,
            json.dumps({
                "reviewer_context": "Background noise from caller side affected speed of answer score",
                "previous_score": 6.2,
                "new_score": 7.1,
            }),
        ),
    )
    counts["audit_log"] = counts.get("audit_log", 0) + 1


def main():
    conn = get_conn()
    try:
        counts = seed(conn)
        if counts is None:
            return
        print("\nMock data seeded successfully.\n")
        print("Rows inserted per table:")
        for table in sorted(counts):
            print(f"  {table:.<35} {counts[table]:>3}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
