"""
generate_erd.py — Convert schema.sql into an ERD Editor .erd file
for VS Code visualization.

Produces a JSON document in the format expected by the
"ERD Editor" VS Code extension (@dineug/erd-editor, v3 format).
"""

import json
import secrets
import time
from pathlib import Path

HERE = Path(__file__).parent
OUTPUT = HERE / "echo_audit_v2.erd"


def nid():
    """Generate a nanoid-style 21-char ID."""
    return secrets.token_urlsafe(16)[:21]


NOW = int(time.time() * 1000)

# Column option bit flags
OPT_PK = 1
OPT_NOTNULL = 2
OPT_UNIQUE = 4
OPT_AUTOINC = 8

# UI key flags
UI_PK = 1
UI_FK = 2
UI_PK_FK = 3


# ───────────────────────────────────────────────────────────────
# Table definitions
# Columns: (name, data_type, options, ui_keys, comment)
# ───────────────────────────────────────────────────────────────

TABLES = {
    "industries": {
        "x": 40, "y": 40,
        "columns": [
            ("industry_id",   "SERIAL",       OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("industry_name", "TEXT",         OPT_NOTNULL | OPT_UNIQUE,           0,     ""),
            ("created_at",    "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
            ("updated_at",    "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
        ],
    },
    "roles": {
        "x": 40, "y": 260,
        "columns": [
            ("role_id",    "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("role_name",  "TEXT",        OPT_NOTNULL | OPT_UNIQUE,           0,     ""),
            ("role_scope", "TEXT",        OPT_NOTNULL,                        0,     "platform | company"),
            ("created_at", "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at", "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "companies": {
        "x": 440, "y": 40,
        "columns": [
            ("company_id",     "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("industryid",     "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("company_name",   "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("company_status", "TEXT",        OPT_NOTNULL,                        0,     "active | suspended | churned"),
            ("deleted_at",     "TIMESTAMPTZ", 0,                                  0,     ""),
            ("created_at",     "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",     "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "company_labels": {
        "x": 840, "y": 40,
        "columns": [
            ("company_label_id", "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",        "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("label_key",        "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("label_value",      "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("created_at",       "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",       "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "locations": {
        "x": 1240, "y": 40,
        "columns": [
            ("location_id",    "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",      "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("location_name",  "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("location_phone", "TEXT",        0,                                  0,     ""),
            ("deleted_at",     "TIMESTAMPTZ", 0,                                  0,     ""),
            ("created_at",     "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",     "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "users": {
        "x": 440, "y": 340,
        "columns": [
            ("user_id",               "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",             "INTEGER",     0,                                  UI_FK, "NULL for super admins"),
            ("user_email",            "TEXT",        OPT_NOTNULL | OPT_UNIQUE,           0,     ""),
            ("user_password_hash",    "TEXT",        0,                                  0,     "NULL for SSO users"),
            ("user_first_name",       "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("user_last_name",        "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("user_auth_provider",    "TEXT",        0,                                  0,     ""),
            ("user_auth_provider_id", "TEXT",        0,                                  0,     ""),
            ("deleted_at",            "TIMESTAMPTZ", 0,                                  0,     ""),
            ("created_at",            "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",            "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "user_roles": {
        "x": 40, "y": 540,
        "columns": [
            ("user_role_id", "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("userid",       "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("roleid",       "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("companyid",    "INTEGER",     0,                                  UI_FK, "NULL for platform roles"),
            ("created_at",   "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",   "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "campaign_types": {
        "x": 1640, "y": 40,
        "columns": [
            ("campaign_type_id",   "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",          "INTEGER",     0,                                  UI_FK, "NULL = industry default"),
            ("industryid",         "INTEGER",     0,                                  UI_FK, ""),
            ("campaign_type_name", "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("created_at",         "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",         "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "rubric_groups": {
        "x": 1240, "y": 340,
        "columns": [
            ("rubric_group_id",           "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",                 "INTEGER",     0,                                  UI_FK, "NULL = industry template"),
            ("rubric_group_name",         "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("rubric_group_grade_target", "TEXT",        OPT_NOTNULL,                        0,     "caller | respondent"),
            ("source_industry_id",        "INTEGER",     0,                                  0,     "lineage only, not an FK"),
            ("deleted_at",                "TIMESTAMPTZ", 0,                                  0,     ""),
            ("created_at",                "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",                "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "rubric_items": {
        "x": 1640, "y": 340,
        "columns": [
            ("rubric_item_id",               "SERIAL",       OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("rubricgroupid",                "INTEGER",      OPT_NOTNULL,                        UI_FK, ""),
            ("rubric_item_name",             "TEXT",         OPT_NOTNULL,                        0,     ""),
            ("rubric_item_score_type",       "TEXT",         OPT_NOTNULL,                        0,     "out_of_10 | yes_no | yes_no_pending"),
            ("rubric_item_weight",           "NUMERIC(5,2)", OPT_NOTNULL,                        0,     ""),
            ("rubric_item_scoring_guidance", "TEXT",         0,                                  0,     ""),
            ("rubric_item_order",            "INTEGER",      OPT_NOTNULL,                        0,     ""),
            ("deleted_at",                   "TIMESTAMPTZ",  0,                                  0,     ""),
            ("created_at",                   "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
            ("updated_at",                   "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
        ],
    },
    "projects": {
        "x": 840, "y": 340,
        "columns": [
            ("project_id",         "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",          "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("project_name",       "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("campaigntypeid",     "INTEGER",     0,                                  UI_FK, ""),
            ("rubricgroupid",      "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("project_start_date", "DATE",        OPT_NOTNULL,                        0,     ""),
            ("project_end_date",   "DATE",        0,                                  0,     ""),
            ("project_status",     "TEXT",        OPT_NOTNULL,                        0,     "active | completed | archived"),
            ("deleted_at",         "TIMESTAMPTZ", 0,                                  0,     ""),
            ("created_at",         "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",         "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "project_users": {
        "x": 440, "y": 640,
        "columns": [
            ("project_user_id", "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("projectid",       "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("userid",          "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("created_at",      "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",      "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "interactions": {
        "x": 840, "y": 720,
        "columns": [
            ("interaction_id",                    "SERIAL",       OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",                         "INTEGER",      OPT_NOTNULL,                        UI_FK, ""),
            ("locationid",                        "INTEGER",      0,                                  UI_FK, ""),
            ("projectid",                         "INTEGER",      0,                                  UI_FK, ""),
            ("campaigntypeid",                    "INTEGER",      0,                                  UI_FK, ""),
            ("calleruserid",                      "INTEGER",      0,                                  UI_FK, "My Team caller"),
            ("respondentuserid",                  "INTEGER",      0,                                  UI_FK, "person who answered"),
            ("interaction_date",                  "DATE",         OPT_NOTNULL,                        0,     ""),
            ("interaction_submitted_at",          "TIMESTAMPTZ",  0,                                  0,     ""),
            ("interaction_status",                "TEXT",         OPT_NOTNULL,                        0,     "pending | transcribing | awaiting_clarification | grading | graded | no_answer"),
            ("interaction_transcript",            "TEXT",         0,                                  0,     ""),
            ("interaction_audio_url",             "TEXT",         0,                                  0,     "object storage URL"),
            ("interaction_overall_score",         "NUMERIC(5,2)", 0,                                  0,     "0-10"),
            ("interaction_original_score",        "NUMERIC(5,2)", 0,                                  0,     "frozen on first grade"),
            ("interaction_regrade_count",         "INTEGER",      OPT_NOTNULL,                        0,     ""),
            ("interaction_regraded_with_context", "BOOLEAN",      OPT_NOTNULL,                        0,     ""),
            ("interaction_reviewer_context",      "TEXT",         0,                                  0,     ""),
            ("deleted_at",                        "TIMESTAMPTZ",  0,                                  0,     ""),
            ("created_at",                        "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
            ("updated_at",                        "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
        ],
    },
    "interaction_rubric_scores": {
        "x": 1640, "y": 720,
        "columns": [
            ("interaction_rubric_score_id",          "SERIAL",       OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("interactionid",                        "INTEGER",      OPT_NOTNULL,                        UI_FK, ""),
            ("rubricitemid",                         "INTEGER",      0,                                  UI_FK, "lineage FK"),
            ("snapshot_rubric_item_name",            "TEXT",         OPT_NOTNULL,                        0,     "frozen at grade time"),
            ("snapshot_rubric_item_score_type",      "TEXT",         OPT_NOTNULL,                        0,     "frozen"),
            ("snapshot_rubric_item_weight",          "NUMERIC(5,2)", OPT_NOTNULL,                        0,     "frozen"),
            ("snapshot_rubric_item_scoring_guidance","TEXT",         0,                                  0,     "frozen"),
            ("score_value",                          "NUMERIC(5,2)", OPT_NOTNULL,                        0,     "0-10"),
            ("score_ai_explanation",                 "TEXT",         0,                                  0,     ""),
            ("created_at",                           "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
            ("updated_at",                           "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
        ],
    },
    "clarifying_questions": {
        "x": 1240, "y": 720,
        "columns": [
            ("clarifying_question_id",   "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("interactionid",            "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("question_text",            "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("question_ai_reason",       "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("question_response_format", "TEXT",        OPT_NOTNULL,                        0,     "yes_no | scale_1_10 | multiple_choice"),
            ("question_answer_value",    "TEXT",        0,                                  0,     ""),
            ("question_order",           "INTEGER",     OPT_NOTNULL,                        0,     ""),
            ("created_at",               "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",               "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "person_reports": {
        "x": 40, "y": 820,
        "columns": [
            ("person_report_id",                       "SERIAL",       OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("userid",                                 "INTEGER",      OPT_NOTNULL,                        UI_FK, ""),
            ("companyid",                              "INTEGER",      OPT_NOTNULL,                        UI_FK, ""),
            ("person_report_type",                     "TEXT",         OPT_NOTNULL,                        0,     "respondent | caller"),
            ("person_report_data",                     "JSONB",        OPT_NOTNULL,                        0,     "strengths, weaknesses, coaching, trend_data"),
            ("person_report_average_score",            "NUMERIC(5,2)", 0,                                  0,     ""),
            ("person_report_call_count",               "INTEGER",      OPT_NOTNULL,                        0,     ""),
            ("person_report_processed_interactionids", "JSONB",        OPT_NOTNULL,                        0,     "JSONB array, processing guard"),
            ("created_at",                             "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
            ("updated_at",                             "TIMESTAMPTZ",  OPT_NOTNULL,                        0,     ""),
        ],
    },
    "api_keys": {
        "x": 2040, "y": 40,
        "columns": [
            ("api_key_id",           "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",            "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("api_key_prefix",       "TEXT",        OPT_NOTNULL,                        0,     "first 8 chars plain"),
            ("api_key_hash",         "TEXT",        OPT_NOTNULL,                        0,     "bcrypt hash"),
            ("api_key_name",         "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("api_key_status",       "TEXT",        OPT_NOTNULL,                        0,     "active | revoked"),
            ("api_key_last_used_at", "TIMESTAMPTZ", 0,                                  0,     ""),
            ("api_key_revoked_at",   "TIMESTAMPTZ", 0,                                  0,     ""),
            ("created_at",           "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",           "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "api_usage": {
        "x": 2040, "y": 340,
        "columns": [
            ("api_usage_id",            "SERIAL",      OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",               "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("api_usage_service",       "TEXT",        OPT_NOTNULL,                        0,     "assemblyai | anthropic | twilio"),
            ("api_usage_period_start",  "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("api_usage_period_type",   "TEXT",        OPT_NOTNULL,                        0,     "hour | day"),
            ("api_usage_request_count", "INTEGER",     OPT_NOTNULL,                        0,     ""),
            ("created_at",              "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("updated_at",              "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
        ],
    },
    "api_call_log": {
        "x": 2040, "y": 640,
        "columns": [
            ("api_call_log_id",              "BIGSERIAL",   OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("companyid",                    "INTEGER",     OPT_NOTNULL,                        UI_FK, ""),
            ("api_call_log_service",         "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("api_call_log_called_at",       "TIMESTAMPTZ", OPT_NOTNULL,                        0,     ""),
            ("api_call_log_response_status", "TEXT",        0,                                  0,     ""),
            ("api_call_log_latency_ms",      "INTEGER",     0,                                  0,     ""),
            ("interactionid",                "INTEGER",     0,                                  UI_FK, ""),
            ("created_at",                   "TIMESTAMPTZ", OPT_NOTNULL,                        0,     "append-only"),
            ("updated_at",                   "TIMESTAMPTZ", OPT_NOTNULL,                        0,     "append-only"),
        ],
    },
    "audit_log": {
        "x": 2040, "y": 940,
        "columns": [
            ("audit_log_id",                 "BIGSERIAL",   OPT_PK | OPT_NOTNULL | OPT_AUTOINC, UI_PK, ""),
            ("actoruserid",                  "INTEGER",     0,                                  UI_FK, ""),
            ("audit_log_action_type",        "TEXT",        OPT_NOTNULL,                        0,     ""),
            ("audit_log_target_entity_type", "TEXT",        0,                                  0,     ""),
            ("audit_log_target_entity_id",   "TEXT",        0,                                  0,     "can reference any entity"),
            ("companyid",                    "INTEGER",     0,                                  UI_FK, "NULL for platform actions"),
            ("audit_log_metadata",           "JSONB",       0,                                  0,     "old/new values, IP, etc"),
            ("created_at",                   "TIMESTAMPTZ", OPT_NOTNULL,                        0,     "append-only"),
            ("updated_at",                   "TIMESTAMPTZ", OPT_NOTNULL,                        0,     "append-only"),
        ],
    },
}


# ───────────────────────────────────────────────────────────────
# Relationships: (start_table, start_col, end_table, end_col, identifying)
# start = parent (PK side), end = child (FK side)
# identifying = True when FK is part of a composite identity (rare here)
# ───────────────────────────────────────────────────────────────

RELATIONSHIPS = [
    ("industries",     "industry_id",       "companies",                "industryid",     False),
    ("companies",      "company_id",        "company_labels",           "companyid",      False),
    ("companies",      "company_id",        "locations",                "companyid",      False),
    ("companies",      "company_id",        "users",                    "companyid",      False),
    ("users",          "user_id",           "user_roles",               "userid",         False),
    ("roles",          "role_id",           "user_roles",               "roleid",         False),
    ("companies",      "company_id",        "user_roles",               "companyid",      False),
    ("companies",      "company_id",        "campaign_types",           "companyid",      False),
    ("industries",     "industry_id",       "campaign_types",           "industryid",     False),
    ("companies",      "company_id",        "rubric_groups",            "companyid",      False),
    ("rubric_groups",  "rubric_group_id",   "rubric_items",             "rubricgroupid",  False),
    ("companies",      "company_id",        "projects",                 "companyid",      False),
    ("campaign_types", "campaign_type_id",  "projects",                 "campaigntypeid", False),
    ("rubric_groups",  "rubric_group_id",   "projects",                 "rubricgroupid",  False),
    ("projects",       "project_id",        "project_users",            "projectid",      False),
    ("users",          "user_id",           "project_users",            "userid",         False),
    ("companies",      "company_id",        "interactions",             "companyid",      False),
    ("locations",      "location_id",       "interactions",             "locationid",     False),
    ("projects",       "project_id",        "interactions",             "projectid",      False),
    ("campaign_types", "campaign_type_id",  "interactions",             "campaigntypeid", False),
    ("users",          "user_id",           "interactions",             "calleruserid",   False),
    ("users",          "user_id",           "interactions",             "respondentuserid", False),
    ("interactions",   "interaction_id",    "interaction_rubric_scores","interactionid",  False),
    ("rubric_items",   "rubric_item_id",    "interaction_rubric_scores","rubricitemid",   False),
    ("interactions",   "interaction_id",    "clarifying_questions",     "interactionid",  False),
    ("users",          "user_id",           "person_reports",           "userid",         False),
    ("companies",      "company_id",        "person_reports",           "companyid",      False),
    ("companies",      "company_id",        "api_keys",                 "companyid",      False),
    ("companies",      "company_id",        "api_usage",                "companyid",      False),
    ("companies",      "company_id",        "api_call_log",             "companyid",      False),
    ("interactions",   "interaction_id",    "api_call_log",             "interactionid",  False),
    ("users",          "user_id",           "audit_log",                "actoruserid",    False),
    ("companies",      "company_id",        "audit_log",                "companyid",      False),
]


# ───────────────────────────────────────────────────────────────
# Build the .erd document
# ───────────────────────────────────────────────────────────────

def build_erd():
    table_entities = {}
    column_entities = {}
    relationship_entities = {}

    # (table_name, column_name) → column_id lookup
    col_lookup = {}
    # table_name → table_id lookup
    table_lookup = {}

    # Build tables + columns
    for tbl_name, tbl_def in TABLES.items():
        tbl_id = nid()
        table_lookup[tbl_name] = tbl_id

        column_ids = []
        for (col_name, dtype, opts, keys, comment) in tbl_def["columns"]:
            col_id = nid()
            col_lookup[(tbl_name, col_name)] = col_id
            column_ids.append(col_id)
            column_entities[col_id] = {
                "id": col_id,
                "tableId": tbl_id,
                "name": col_name,
                "comment": comment,
                "dataType": dtype,
                "default": "",
                "options": opts,
                "ui": {
                    "keys": keys,
                    "widthName": 60,
                    "widthComment": 60,
                    "widthDataType": 60,
                    "widthDefault": 60,
                },
                "meta": {"updateAt": NOW, "createAt": NOW},
            }

        table_entities[tbl_id] = {
            "id": tbl_id,
            "name": tbl_name,
            "comment": "",
            "columnIds": column_ids,
            "seqColumnIds": list(column_ids),
            "ui": {
                "x": tbl_def["x"],
                "y": tbl_def["y"],
                "zIndex": 2,
                "widthName": 60,
                "widthComment": 60,
                "color": "",
            },
            "meta": {"updateAt": NOW, "createAt": NOW},
        }

    # Build relationships
    for (start_tbl, start_col, end_tbl, end_col, identifying) in RELATIONSHIPS:
        rel_id = nid()
        start_table_id = table_lookup[start_tbl]
        end_table_id = table_lookup[end_tbl]
        start_col_id = col_lookup[(start_tbl, start_col)]
        end_col_id = col_lookup[(end_tbl, end_col)]

        relationship_entities[rel_id] = {
            "id": rel_id,
            "identification": identifying,
            # 16 = OneN (one-to-many) — standard FK cardinality
            "relationshipType": 16,
            "startRelationshipType": 2,  # dash start
            "start": {
                "tableId": start_table_id,
                "columnIds": [start_col_id],
                "x": 0,
                "y": 0,
                "direction": 1,
            },
            "end": {
                "tableId": end_table_id,
                "columnIds": [end_col_id],
                "x": 0,
                "y": 0,
                "direction": 1,
            },
            "meta": {"updateAt": NOW, "createAt": NOW},
        }

    doc = {
        "version": "3.0.0",
        "settings": {
            "width": 3000,
            "height": 2000,
            "scrollTop": 0,
            "scrollLeft": 0,
            "zoomLevel": 1,
            "show": 431,  # show name, dataType, notNull, default, comment, pk, fk, unique
            "database": 16,  # PostgreSQL
            "databaseName": "echo_audit_v2",
            "canvasType": "ERD",
            "language": 1,
            "tableNameCase": 4,
            "columnNameCase": 2,
            "bracketType": 1,
            "relationshipDataTypeSync": True,
            "relationshipOptimization": False,
            "columnOrder": [1, 2, 4, 8, 16, 32, 64],
            "maxWidthComment": -1,
            "ignoreSaveSettings": 0,
        },
        "doc": {
            "tableIds": list(table_entities.keys()),
            "relationshipIds": list(relationship_entities.keys()),
            "indexIds": [],
            "memoIds": [],
        },
        "collections": {
            "tableEntities": table_entities,
            "tableColumnEntities": column_entities,
            "relationshipEntities": relationship_entities,
            "indexEntities": {},
            "indexColumnEntities": {},
            "memoEntities": {},
        },
        "lww": {},
    }

    return doc


if __name__ == "__main__":
    doc = build_erd()
    OUTPUT.write_text(json.dumps(doc, indent=2))
    print(f"Wrote {OUTPUT}")
    print(f"  Tables:        {len(doc['doc']['tableIds'])}")
    print(f"  Columns:       {len(doc['collections']['tableColumnEntities'])}")
    print(f"  Relationships: {len(doc['doc']['relationshipIds'])}")
