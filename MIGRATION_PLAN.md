# Echo Audit V1 → V2 Migration Plan

## Overview

Migrating from the V1 prototype schema (flat `grades` table, text-based
everything, no proper FKs) to the V2 normalized schema (20 tables, proper
types, enforced constraints). This is a one-way migration — V1 is retired
after cutover.

---

## Phase 0 — Pre-Migration Setup

1. **Take a full pg_dump backup of the V1 production database.**
   Store it in object storage with a timestamp. This is the rollback point.

2. **Create the V2 schema in a NEW database** (or a new PostgreSQL schema
   within the same database). Never mutate V1 tables — migrate data into
   fresh V2 tables.

3. **Freeze V1 writes.** Put the app in read-only / maintenance mode
   before starting data migration. No new grades should be written during
   migration.

4. **Upload all audio.** Before migrating interaction rows, extract every
   `grades.audio_data` blob from V1, upload it to R2/S3, and build a
   mapping of `grades.id → audio_url`. This must complete before Phase 2
   because V2 stores only URLs.

   ```sql
   -- V1: extract rows with audio data
   SELECT id, audio_data, audio_mime
   FROM grades
   WHERE audio_data IS NOT NULL;
   ```

   For each row: upload `audio_data` to object storage as
   `{company_id}/{grades.id}.{extension}`, record the URL.

---

## Phase 1 — Reference & Tenant Tables (no FK dependencies)

Order matters. Insert parent rows before child rows.

### 1.1 industries

V1 has no industries table. Seed from application config.

```
Action: INSERT industry defaults (Property Management, HVAC,
        Auto Dealership, Call Center, Healthcare, etc.)
Source: Seed data — not migrated from V1
```

### 1.2 roles

V1 stores role as a text column on `users.role`.
V2 has a normalized `roles` table.

```
Action: INSERT the 5 role rows:
        (super_admin, platform), (admin, company),
        (manager, company), (caller, company),
        (respondent, company)
Source: Seed data — not migrated from V1
```

### 1.3 companies

V1: `companies (id, name, settings, created_at, is_active)`
V2: `companies (company_id, industryid, company_name, company_status, ...)`

```sql
-- Map V1 → V2
INSERT INTO v2.companies (company_id, industryid, company_name, company_status, created_at, updated_at)
SELECT
    c.id,
    (SELECT industry_id FROM v2.industries WHERE industry_name = 'Property Management'),
    -- ^ Default to Property Management; adjust per company if known
    c.name,
    CASE WHEN c.is_active = 1 THEN 'active' ELSE 'churned' END,
    COALESCE(c.created_at::timestamptz, NOW()),
    NOW()
FROM v1.companies c;
```

**Decisions:**
- `industryid`: V1 has no industry concept. Default all existing companies
  to a single industry (e.g., Property Management since Mayfair is the
  current client). Manually correct post-migration if other industries exist.
- `company_status`: Map `is_active=1 → 'active'`, `is_active=0 → 'churned'`.
- `settings` column: **Dropped.** V1 stores settings as a JSON blob on
  the companies row. V2 replaces this with the `company_labels` table
  and application-level config. Extract label values from the JSON before
  dropping.

### 1.4 company_labels

V1: Embedded in `companies.settings` JSON blob or `app_settings` table.
V2: Normalized key/value table.

```sql
-- Extract from app_settings where applicable
INSERT INTO v2.company_labels (companyid, label_key, label_value)
SELECT
    company_id,
    key,
    value
FROM v1.app_settings
WHERE key IN ('location_label', 'team_label', 'respondent_label', 'campaign_type_label');
```

**If no matching rows exist**, skip — V2 app falls back to defaults.

### 1.5 locations

V1: `properties (y_code PK, name, phone, pm_email, rm_email, vp_email, rm_phone, company_id)`
V2: `locations (location_id, companyid, location_name, location_phone, ...)`

```sql
INSERT INTO v2.locations (companyid, location_name, location_phone, created_at, updated_at)
SELECT
    COALESCE(p.company_id, 1),
    p.name,
    p.phone,
    NOW(),
    NOW()
FROM v1.properties p;
```

**Build a mapping table** for later use:
```sql
-- Temporary: map old y_code + name to new location_id
CREATE TEMP TABLE location_map AS
SELECT p.y_code, p.name AS old_name, p.company_id, l.location_id
FROM v1.properties p
JOIN v2.locations l ON l.location_name = p.name AND l.companyid = COALESCE(p.company_id, 1);
```

**Dropped columns:**
- `y_code`: V1-specific identifier. Not carried forward. The mapping table
  preserves it for the migration only.
- `pm_email`, `rm_email`, `vp_email`, `rm_phone`: Property manager contact
  info. **Dropped** — these are not part of the V2 schema. If needed later,
  they belong in a separate `location_contacts` table, not on locations.

---

## Phase 2 — Users, Roles, Rubrics, Campaigns

### 2.1 users

V1: `users (id, company_id, email, password_hash, first_name, last_name, role, is_active, ...)`
V2: `users (user_id, companyid, user_email, user_password_hash, user_first_name, user_last_name, ...)`

```sql
INSERT INTO v2.users (user_id, companyid, user_email, user_password_hash,
                      user_first_name, user_last_name, deleted_at, created_at, updated_at)
SELECT
    u.id,
    u.company_id,
    u.email,
    u.password_hash,
    COALESCE(NULLIF(u.first_name, ''), 'Unknown'),
    COALESCE(NULLIF(u.last_name, ''), 'User'),
    CASE WHEN u.is_active = 0 THEN NOW() ELSE NULL END,  -- soft-delete inactive users
    COALESCE(u.created_at::timestamptz, NOW()),
    NOW()
FROM v1.users u;
```

**Decisions:**
- `is_active=0` → set `deleted_at` (V2 uses soft delete, not boolean flag).
- `must_change_password`: **Dropped.** Handle in application layer if needed
  (e.g., force password reset for all migrated users on first V2 login).
- `last_login`: **Dropped.** V2 doesn't track this in the users table.
  Could be added to audit_log if needed.
- Empty `first_name`/`last_name`: V1 allowed empty defaults. V2 requires
  NOT NULL. Backfill with 'Unknown'/'User' for any empty values.

### 2.2 respondents → users

V1 has a separate `respondents` table. V2 treats respondents as users
with the respondent role.

```sql
-- Insert respondents as users (they may not have email/password)
INSERT INTO v2.users (companyid, user_email, user_first_name, user_last_name, created_at, updated_at)
SELECT
    r.company_id,
    'respondent_' || r.id || '@placeholder.echoaudit.local',
    -- ^ Placeholder email — respondents detected from transcripts
    -- don't have real email addresses. These are identifiable
    -- and can be updated later.
    r.name,
    '',  -- last_name unknown; split name if possible in app code
    COALESCE(r.created_at::timestamptz, NOW()),
    NOW()
FROM v1.respondents r;
```

**Build a mapping table:**
```sql
CREATE TEMP TABLE respondent_user_map AS
SELECT r.id AS old_respondent_id, u.user_id AS new_user_id
FROM v1.respondents r
JOIN v2.users u ON u.user_email = 'respondent_' || r.id || '@placeholder.echoaudit.local';
```

### 2.3 user_roles

V1 stores role as a text field on users. V2 normalizes to junction table.

```sql
INSERT INTO v2.user_roles (userid, roleid, companyid, created_at, updated_at)
SELECT
    u.id,
    r.role_id,
    u.company_id,
    NOW(),
    NOW()
FROM v1.users u
JOIN v2.roles r ON r.role_name = u.role;
```

Then add respondent role for migrated respondent-users:
```sql
INSERT INTO v2.user_roles (userid, roleid, companyid, created_at, updated_at)
SELECT
    rum.new_user_id,
    (SELECT role_id FROM v2.roles WHERE role_name = 'respondent'),
    v1r.company_id,
    NOW(),
    NOW()
FROM respondent_user_map rum
JOIN v1.respondents v1r ON v1r.id = rum.old_respondent_id;
```

### 2.4 campaign_types

V1 has no campaign types taxonomy. Seed defaults per industry, then
extract distinct campaign-like labels from V1 data if any exist.

```
Action: Seed industry-default campaign types
        (Secret Shopping, Inbound Sales, Customer Service, Leasing Calls)
Source: Seed data
```

### 2.5 rubric_groups + rubric_items

V1: `rubrics (id, company_id, name, criteria JSON, context, script, version, is_active)`
V2: `rubric_groups` + `rubric_items` (normalized)

```sql
-- Rubric groups
INSERT INTO v2.rubric_groups (rubric_group_id, companyid, rubric_group_name,
                              rubric_group_grade_target, created_at, updated_at)
SELECT
    r.id,
    r.company_id,
    r.name,
    'respondent',  -- V1 default; adjust per rubric if caller-mode rubrics exist
    COALESCE(r.created_at::timestamptz, NOW()),
    NOW()
FROM v1.rubrics r;
```

```sql
-- Rubric items: parse the criteria JSON array from each V1 rubric
-- This requires a script (Python recommended) because criteria is a JSON array:
-- [{"name": "Greeting", "type": "numeric", "scale": 10, "weight": 1.0}, ...]
```

**Python migration script needed:**
```python
import json

for rubric in v1_rubrics:
    criteria = json.loads(rubric['criteria'])
    for idx, item in enumerate(criteria):
        v2_cursor.execute("""
            INSERT INTO rubric_items
                (rubricgroupid, rubric_item_name, rubric_item_score_type,
                 rubric_item_weight, rubric_item_order, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
        """, (
            rubric['id'],
            item['name'],
            'out_of_10' if item.get('type') == 'numeric' else 'yes_no',
            item.get('weight', 1.0),
            idx
        ))
```

**Dropped columns:**
- `rubrics.context`, `rubrics.script`: Free-text fields used in V1 prompts.
  V2 replaces these with per-item `rubric_item_scoring_guidance`. If context
  is valuable, it can be appended to individual item guidance during
  migration.
- `rubrics.version`: V2 uses snapshot-on-grade instead of version numbers.
- `rubrics.is_active`: V2 uses `deleted_at` for soft delete. Map
  `is_active=0 → deleted_at=NOW()`.

### 2.6 projects

V1: `campaigns (id, name, company_id, created_at)`
V2: `projects (project_id, companyid, project_name, rubricgroupid, project_start_date, ...)`

```sql
INSERT INTO v2.projects (project_id, companyid, project_name, rubricgroupid,
                         project_start_date, project_status, created_at, updated_at)
SELECT
    c.id,
    COALESCE(c.company_id, 1),
    c.name,
    -- Link to the company's active rubric (best guess — V1 campaigns
    -- don't track which rubric they used)
    (SELECT rubric_group_id FROM v2.rubric_groups
     WHERE companyid = COALESCE(c.company_id, 1)
     ORDER BY created_at DESC LIMIT 1),
    COALESCE(c.created_at::date, CURRENT_DATE),
    'active',
    COALESCE(c.created_at::timestamptz, NOW()),
    NOW()
FROM v1.campaigns c;
```

**Decisions:**
- `rubricgroupid`: V1 campaigns have no rubric link. Best effort: assign
  the company's most recent rubric group. Flag for manual review post-migration.
- `project_start_date`: Use campaign `created_at`. V1 has no date range concept.
- `campaigntypeid`: NULL — V1 has no campaign types.

---

## Phase 3 — Core Data (Interactions)

### 3.1 interactions

V1: `grades` (monolithic table with 30+ columns)
V2: `interactions` (normalized, FKs to all related tables)

This is the most complex mapping. Build it in a Python script for safety.

```sql
INSERT INTO v2.interactions (
    interaction_id, companyid, locationid, projectid,
    calleruserid, respondentuserid,
    interaction_date, interaction_submitted_at, interaction_status,
    interaction_transcript, interaction_audio_url,
    interaction_overall_score, interaction_original_score,
    interaction_regrade_count, interaction_regraded_with_context,
    interaction_reviewer_context,
    created_at, updated_at
)
SELECT
    g.id,
    COALESCE(g.company_id, 1),

    -- locationid: map from property_name → locations
    (SELECT lm.location_id FROM location_map lm
     WHERE lm.old_name = g.property_name
       AND lm.company_id = COALESCE(g.company_id, 1)
     LIMIT 1),

    -- projectid: map from campaign_name → projects
    (SELECT p.project_id FROM v2.projects p
     WHERE p.project_name = g.campaign_name
       AND p.companyid = COALESCE(g.company_id, 1)
     LIMIT 1),

    -- calleruserid: graded_by_user_id or lookup by caller_name
    g.graded_by_user_id,

    -- respondentuserid: map from respondent_id → new user
    (SELECT rum.new_user_id FROM respondent_user_map rum
     WHERE rum.old_respondent_id = g.respondent_id),

    COALESCE(g.call_date::date, g.date::date, CURRENT_DATE),
    g.submitted_at,

    CASE
        WHEN g.call_outcome = 'no_answer' THEN 'no_answer'
        WHEN g.total_score IS NOT NULL THEN 'graded'
        ELSE 'pending'
    END,

    NULLIF(g.transcript, ''),
    -- audio_url: from the pre-built audio upload mapping
    (SELECT audio_url FROM audio_upload_map WHERE grade_id = g.id),

    g.total_score,
    g.original_score,
    COALESCE(g.regrade_count, 0),
    COALESCE(g.regraded_with_context::boolean, FALSE),
    NULLIF(g.reviewer_context, ''),
    NOW(),
    NOW()
FROM v1.grades g;
```

### 3.2 interaction_rubric_scores

V1: `grades.scores` is a JSON string containing per-item scores.
V1: `grades.explanations` is a JSON string containing AI explanations.
V2: One row per rubric item per interaction with snapshot columns.

**Python migration script needed:**
```python
import json

for grade in v1_grades:
    if not grade['scores']:
        continue

    scores = json.loads(grade['scores'])
    explanations = json.loads(grade.get('explanations') or '{}')

    # Get the rubric items that were active for this company at grade time
    # Since V1 has no snapshot, we use the current rubric items as best-effort
    rubric_items = get_rubric_items_for_company(grade['company_id'])

    for item_name, score_val in scores.items():
        matching_item = find_item_by_name(rubric_items, item_name)
        v2_cursor.execute("""
            INSERT INTO interaction_rubric_scores (
                interactionid, rubricitemid,
                snapshot_rubric_item_name, snapshot_rubric_item_score_type,
                snapshot_rubric_item_weight, snapshot_rubric_item_scoring_guidance,
                score_value, score_ai_explanation,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        """, (
            grade['id'],
            matching_item['rubric_item_id'] if matching_item else None,
            item_name,
            matching_item['rubric_item_score_type'] if matching_item else 'out_of_10',
            matching_item['rubric_item_weight'] if matching_item else 1.0,
            matching_item.get('rubric_item_scoring_guidance'),
            float(score_val) if score_val is not None else 0,
            explanations.get(item_name)
        ))
```

**Important caveat:** V1 has no rubric snapshot. Historical scores are
reconstructed from the current rubric state. This is lossy if rubrics
were edited between grades. Document this as a known data quality gap
for pre-migration grades.

### 3.3 clarifying_questions

V1: `grades.clarifying_responses` is a JSON string.
V2: Normalized `clarifying_questions` table.

```python
import json

for grade in v1_grades:
    responses = json.loads(grade.get('clarifying_responses') or '{}')
    if not responses:
        continue

    for idx, (question, answer) in enumerate(responses.items()):
        v2_cursor.execute("""
            INSERT INTO clarifying_questions (
                interactionid, question_text, question_ai_reason,
                question_response_format, question_answer_value,
                question_order, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        """, (
            grade['id'],
            question,
            'Migrated from V1 — original AI reason not preserved',
            'multiple_choice',  -- V1 didn't track format; default
            str(answer) if answer is not None else None,
            idx
        ))
```

**Known lossy fields:**
- `question_ai_reason`: V1 didn't store this. Backfill with a migration marker.
- `question_response_format`: V1 didn't track format. Default to `multiple_choice`.

---

## Phase 4 — Derived & Supporting Data

### 4.1 person_reports

V1: `person_reports (id, company_id, person_identifier, location_name, grade_target, report_strengths, report_weaknesses, report_coaching, average_score, call_count, last_updated, processed_grade_ids)`

V2: `person_reports (person_report_id, userid, companyid, person_report_type, person_report_data JSONB, ...)`

```python
for report in v1_person_reports:
    # Find the user_id for this person_identifier
    # person_identifier in V1 is the person's name (text match)
    user_id = lookup_user_by_name(report['person_identifier'], report['company_id'])
    if not user_id:
        continue  # skip orphaned reports

    report_data = {
        "strengths": report['report_strengths'],
        "weaknesses": report['report_weaknesses'],
        "coaching_recommendations": report['report_coaching'],
        "trend_data": {}  # V1 had no trend data
    }

    # Map processed_grade_ids → processed_interactionids
    # Grade IDs map 1:1 to interaction IDs
    processed_ids = json.loads(report.get('processed_grade_ids') or '[]')

    v2_cursor.execute("""
        INSERT INTO person_reports (
            userid, companyid, person_report_type, person_report_data,
            person_report_average_score, person_report_call_count,
            person_report_processed_interactionids,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
    """, (
        user_id,
        report['company_id'],
        report['grade_target'],
        json.dumps(report_data),
        report['average_score'],
        report['call_count'],
        json.dumps(processed_ids)
    ))
```

### 4.2 api_usage

V1: `api_usage (id, company_id, api_name, request_count, window_start, window_type)`
V2: `api_usage (api_usage_id, companyid, api_usage_service, api_usage_period_start, api_usage_period_type, api_usage_request_count)`

```sql
INSERT INTO v2.api_usage (companyid, api_usage_service, api_usage_period_start,
                          api_usage_period_type, api_usage_request_count,
                          created_at, updated_at)
SELECT
    a.company_id,
    a.api_name,
    a.window_start::timestamptz,
    a.window_type,
    a.request_count,
    NOW(),
    NOW()
FROM v1.api_usage a;
```

### 4.3 project_users

V1 has no campaign-user assignments. Skip — populate going forward only.

### 4.4 audit_log, api_call_log, api_keys

These tables are new in V2. No V1 data to migrate. Start empty.

---

## Phase 5 — Sequence Reset & Validation

After all data is inserted, reset the SERIAL sequences so new inserts
don't collide with migrated IDs:

```sql
SELECT setval('companies_company_id_seq',  (SELECT COALESCE(MAX(company_id), 0)  FROM companies));
SELECT setval('users_user_id_seq',         (SELECT COALESCE(MAX(user_id), 0)     FROM users));
SELECT setval('locations_location_id_seq', (SELECT COALESCE(MAX(location_id), 0) FROM locations));
SELECT setval('interactions_interaction_id_seq', (SELECT COALESCE(MAX(interaction_id), 0) FROM interactions));
SELECT setval('projects_project_id_seq',   (SELECT COALESCE(MAX(project_id), 0)  FROM projects));
SELECT setval('rubric_groups_rubric_group_id_seq', (SELECT COALESCE(MAX(rubric_group_id), 0) FROM rubric_groups));
SELECT setval('rubric_items_rubric_item_id_seq', (SELECT COALESCE(MAX(rubric_item_id), 0) FROM rubric_items));
-- Repeat for all tables where we preserved V1 IDs
```

### Validation Queries

Run these after migration to verify data integrity:

```sql
-- Row counts: V1 grades should match V2 interactions
SELECT 'v1_grades' AS source, COUNT(*) FROM v1.grades
UNION ALL
SELECT 'v2_interactions', COUNT(*) FROM v2.interactions;

-- Every interaction has a valid companyid
SELECT COUNT(*) FROM v2.interactions i
WHERE NOT EXISTS (SELECT 1 FROM v2.companies c WHERE c.company_id = i.companyid);
-- Expected: 0

-- Every user_role points to a valid user and role
SELECT COUNT(*) FROM v2.user_roles ur
WHERE NOT EXISTS (SELECT 1 FROM v2.users u WHERE u.user_id = ur.userid);
-- Expected: 0

-- Every interaction_rubric_score points to a valid interaction
SELECT COUNT(*) FROM v2.interaction_rubric_scores irs
WHERE NOT EXISTS (SELECT 1 FROM v2.interactions i WHERE i.interaction_id = irs.interactionid);
-- Expected: 0

-- Score range validation
SELECT COUNT(*) FROM v2.interactions WHERE interaction_overall_score > 10 OR interaction_overall_score < 0;
-- Expected: 0

SELECT COUNT(*) FROM v2.interaction_rubric_scores WHERE score_value > 10 OR score_value < 0;
-- Expected: 0
```

---

## V1 Tables — Disposition Summary

| V1 Table | V2 Equivalent | Action |
|----------|--------------|--------|
| grades | interactions + interaction_rubric_scores + clarifying_questions | Split into 3 normalized tables |
| agents | users (with caller/respondent role) | Merged into users |
| companies | companies | Migrated with field renames |
| properties | locations | Migrated; y_code and contact emails dropped |
| phone_numbers | *dropped* | Not part of V2 scope; Twilio integration redesigned |
| twilio_config | *dropped* | Credentials move to environment variables |
| app_settings | company_labels (partial) | Label-type settings migrated; rest handled in app config |
| rubrics | rubric_groups + rubric_items | Normalized from JSON blob to rows |
| graph_presets | *dropped* | V2 rebuilds dashboard/graphing from scratch |
| campaigns | projects | Renamed and extended with date range, status, rubric link |
| users | users + user_roles | Role extracted to junction table |
| person_reports | person_reports | Restructured: text fields → JSONB sections |
| api_usage | api_usage | Direct mapping with field renames |
| respondents | users (with respondent role) | Merged into users table |

---

## Rollback Strategy

1. **Before migration**: Full `pg_dump` of V1 stored in object storage.
2. **During migration**: V2 is built in a separate database/schema.
   V1 is untouched and still functional.
3. **Cutover**: Application config switches `DATABASE_URL` to V2.
   V1 database remains available as read-only fallback.
4. **If rollback needed**: Revert `DATABASE_URL` to V1, deploy V1 code.
   No data loss because V1 was never modified.
5. **Post-validation window**: Keep V1 database for 30 days after
   successful cutover. Drop after confirming no issues.

---

## Migration Execution Order (Summary)

```
0. Backup V1 → freeze writes → upload audio to object storage
1. Seed: industries, roles
2. Migrate: companies → company_labels → locations
3. Migrate: users (from V1 users + respondents) → user_roles
4. Seed: campaign_types
5. Migrate: rubric_groups + rubric_items (from V1 rubrics JSON)
6. Migrate: projects (from V1 campaigns)
7. Migrate: interactions (from V1 grades)
8. Migrate: interaction_rubric_scores (from V1 grades.scores JSON)
9. Migrate: clarifying_questions (from V1 grades.clarifying_responses JSON)
10. Migrate: person_reports
11. Migrate: api_usage
12. Reset sequences → run validation queries
13. Cutover: switch DATABASE_URL → verify → monitor
```

Total estimated data volume: Small. Mayfair (primary client) likely has
<50K grades, <200 users, <120 properties. Migration should complete
in minutes, not hours.
