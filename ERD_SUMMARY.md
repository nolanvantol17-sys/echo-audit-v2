# Echo Audit V2 — Entity Relationship Summary

Reference document for all tables, keys, and relationships.
Use this when prompting development work against the V2 schema.

---

## Naming Conventions

**Every column is prefixed with its table name or an abbreviation.** No exceptions — including `created_at`, `updated_at`, `deleted_at`.

| Table | Abbreviation | PK column |
|-------|--------------|-----------|
| `interaction_rubric_scores` | `irs_` | `interaction_rubric_score_id` |
| `clarifying_questions` | `cq_` | `clarifying_question_id` |
| `api_call_log` | `acl_` | `api_call_log_id` |
| `performance_reports` | `pr_` | `performance_report_id` |
| `user_roles` | `ur_` | `user_role_id` |
| `rubric_groups` | `rg_` | `rubric_group_id` |
| `rubric_items` | `ri_` | `rubric_item_id` |
| `api_usage` | `au_` | `api_usage_id` |
| `api_keys` | `ak_` | `api_key_id` |
| `audit_log` | `al_` | `audit_log_id` |
| `company_labels` | `cl_` | `company_label_id` |
| all others | singular table name | `{singular}_id` |

**FK column naming:** FKs match the target's PK exactly (e.g. `user_role_id`, `status_id`, `caller_user_id`). The previous no-underscore convention (`companyid`, `userid`, `statusid`) is abandoned.

---

## Tables (23)

### 0. statuses *(lookup)*
**PK:** `status_id` (explicit int)
**FKs:** none
Centralized status taxonomy. `UNIQUE(status_name, status_category)` — names can repeat across categories. No updated_at — retire via `status_is_active = FALSE`.

### 1. audit_log_action_types *(lookup)* — NEW
**PK:** `audit_log_action_type_id` (explicit int)
**FKs:** none
Lookup for audit_log actions. Seeded: created, updated, deleted, graded, regraded, submitted, unposted.

### 2. audit_log_target_entity_types *(lookup)* — NEW
**PK:** `audit_log_target_entity_type_id` (explicit int)
**FKs:** none
Lookup for audit_log target entity types. Seeded: user, interaction, project, campaign, company, rubric_group, rubric_item, department, location.

### 3. industries
**PK:** `industry_id`
**FKs:** `status_id → statuses.status_id`

### 4. roles
**PK:** `role_id`
**FKs:** none
Static reference — super_admin (platform), admin, manager, caller, respondent.

### 5. user_roles
**PK:** `user_role_id`
**FKs:** `role_id → roles.role_id`
**FK direction reversed.** `user_id` removed. `users.user_role_id` now points at this table. Multiple users can share a row (many-to-one from users).

### 6. companies
**PK:** `company_id`
**FKs:** `industry_id → industries.industry_id`, `status_id → statuses.status_id`
`company_engagement_date` tracks client go-live. Soft-deletable via `company_deleted_at`.

### 7. company_labels
**PK:** `company_label_id`
**FKs:** `company_id → companies.company_id` (CASCADE)
Configurable UI label overrides per company.

### 8. locations
**PK:** `location_id`
**FKs:** `company_id → companies.company_id`, `status_id → statuses.status_id`

### 9. departments
**PK:** `department_id`
**FKs:** `company_id → companies.company_id` (CASCADE), `status_id → statuses.status_id`

### 10. campaigns *(renamed from campaign_types)*
**PK:** `campaign_id`
**FKs:** `location_id → locations.location_id` (CASCADE) — **sole parent FK**
`company_id` and `industry_id` removed. A campaign belongs to exactly one location.

### 11. users
**PK:** `user_id`
**FKs:** `user_role_id → user_roles.user_role_id` (SET NULL), `department_id → departments.department_id` (SET NULL), `status_id → statuses.status_id`
**`company_id` removed.** Tenant derived via `department_id → departments.company_id`. Super admins have NULL department_id and no company link.

### 12. rubric_groups
**PK:** `rubric_group_id`
**FKs:** `location_id → locations.location_id` (RESTRICT, nullable for templates), `status_id → statuses.status_id`
`company_id` removed. `location_id` is sole parent FK. Industry templates have NULL `location_id` and non-NULL `rg_source_industry_id` for lineage.

### 13. rubric_items
**PK:** `rubric_item_id`
**FKs:** `rubric_group_id → rubric_groups.rubric_group_id` (CASCADE), `status_id → statuses.status_id`

### 14. projects
**PK:** `project_id`
**FKs:** `company_id → companies.company_id`, `campaign_id → campaigns.campaign_id` (SET NULL), `rubric_group_id → rubric_groups.rubric_group_id`, `status_id → statuses.status_id`
Replaces V1 "campaign" concept. Strict 1:1 with one rubric group.

### 15. interactions
**PK:** `interaction_id`
**FKs:** `project_id → projects.project_id` (SET NULL), `caller_user_id → users.user_id` (SET NULL), `respondent_user_id → users.user_id` (SET NULL), `status_id → statuses.status_id` (default 45 = pending)
**`location_id` and `campaign_id` both removed.** Location/tenant scope derived via: `interaction → project → campaign → location`.

### 16. interaction_rubric_scores
**PK:** `interaction_rubric_score_id`
**FKs:** `interaction_id → interactions.interaction_id` (CASCADE), `rubric_item_id → rubric_items.rubric_item_id` (SET NULL, lineage only)
Snapshot columns (`irs_snapshot_*`) frozen at grade time. Score columns updated on regrade.

### 17. clarifying_questions
**PK:** `clarifying_question_id`
**FKs:** `interaction_id → interactions.interaction_id` (CASCADE)

### 18. performance_reports
**PK:** `performance_report_id`
**FKs:** `subject_user_id → users.user_id` (SET NULL, nullable)
One row per evaluated user. Subject type (caller vs respondent) derived from the user's role at query time.

### 19. api_keys
**PK:** `api_key_id`
**FKs:** `company_id → companies.company_id` (CASCADE), `status_id → statuses.status_id`

### 20. api_usage
**PK:** `api_usage_id`
**FKs:** `company_id → companies.company_id` (CASCADE)

### 21. api_call_log *(append-only)*
**PK:** `api_call_log_id` (BIGSERIAL)
**FKs:** `company_id → companies.company_id` (CASCADE), `interaction_id → interactions.interaction_id` (SET NULL)

### 22. audit_log *(append-only)*
**PK:** `audit_log_id` (BIGSERIAL)
**FKs:** `actor_user_id → users.user_id` (SET NULL), `audit_log_action_type_id → audit_log_action_types` (RESTRICT), `audit_log_target_entity_type_id → audit_log_target_entity_types` (RESTRICT)
**`company_id` removed.** `audit_log_action_type` and `audit_log_target_entity_type` converted from free text to integer FKs.

---

## Key Architectural Notes

- **Tenant scope derivation chains**: With `company_id` removed from users, interactions, rubric_groups, and audit_log, most tenant lookups now require joins:
  - **user → company**: `users.department_id → departments.company_id` (nullable; users without a department have no company)
  - **interaction → company**: `interactions.project_id → projects.company_id`
  - **interaction → location**: `interactions.project_id → projects.campaign_id → campaigns.location_id`
  - **rubric_group → company**: `rubric_groups.location_id → locations.company_id` (templates have NULL)
  - **audit_log → company**: `audit_log.actor_user_id → users.department_id → departments.company_id`
- **Snapshot-on-grade**: `interaction_rubric_scores.irs_snapshot_*` columns frozen at grade time.
- **Audio storage**: Object storage (R2/S3). DB stores URL only.
- **Score scale**: 0-10 everywhere.
- **Soft deletes**: `{prefix}_deleted_at` on core entities.
- **Append-only tables**: `api_call_log`, `audit_log` use BIGSERIAL.
- **Status & audit-log lookups**: `statuses`, `audit_log_action_types`, `audit_log_target_entity_types` are integer-FK lookup tables. Seeded with explicit IDs.
