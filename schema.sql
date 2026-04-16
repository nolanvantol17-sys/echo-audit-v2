-- ================================================================
-- Echo Audit V2 — Complete PostgreSQL Schema
-- ================================================================
-- Naming conventions (enforced without exception):
--   Every column is prefixed with its table name or abbreviation:
--     interaction_rubric_scores  →  irs_
--     clarifying_questions       →  cq_
--     api_call_log               →  acl_
--     performance_reports        →  pr_
--     user_roles                 →  ur_
--     rubric_groups              →  rg_
--     rubric_items               →  ri_
--     api_usage                  →  au_
--     api_keys                   →  ak_
--     audit_log                  →  al_
--     company_labels             →  cl_
--     audit_log_action_types     →  audit_log_action_type_   (no abbrev)
--     audit_log_target_entity_types → audit_log_target_entity_type_ (no abbrev)
--     all others                 →  full singular table name
--
--   PKs use full singular form (e.g. user_role_id, audit_log_id)
--   FKs match the target's PK exactly (e.g. user_role_id, status_id)
--   Non-key columns use the abbreviation prefix where applicable.
-- ================================================================


-- ----------------------------------------------------------------
-- Helper: auto-update {prefix}_updated_at on row modification.
-- Each table's trigger uses its own column name so the function
-- cannot be shared — per-table trigger functions are defined below.
-- ----------------------------------------------------------------


-- ================================================================
-- 0. statuses  (reference/lookup)
-- ================================================================
-- No updated_at — retire values via status_is_active = FALSE.
--
-- Helper queries:
--   SELECT * FROM statuses WHERE status_category = 'company';
--   SELECT s.status_name FROM companies c
--   JOIN statuses s ON s.status_id = c.status_id
--   WHERE c.company_id = 1;
-- ================================================================
CREATE TABLE statuses (
    status_id          INTEGER PRIMARY KEY,   -- explicit, not SERIAL
    status_name        TEXT NOT NULL,
    status_description TEXT,
    status_category    TEXT NOT NULL,
    status_is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    status_created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_statuses_name_category UNIQUE (status_name, status_category)
);

CREATE INDEX idx_statuses_category ON statuses (status_category);


-- ================================================================
-- 1. audit_log_action_types  (reference/lookup)
-- ================================================================
CREATE TABLE audit_log_action_types (
    audit_log_action_type_id         INTEGER PRIMARY KEY,
    audit_log_action_type_name       TEXT NOT NULL UNIQUE,
    audit_log_action_type_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ================================================================
-- 2. audit_log_target_entity_types  (reference/lookup)
-- ================================================================
CREATE TABLE audit_log_target_entity_types (
    audit_log_target_entity_type_id         INTEGER PRIMARY KEY,
    audit_log_target_entity_type_name       TEXT NOT NULL UNIQUE,
    audit_log_target_entity_type_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ================================================================
-- 3. industries
-- ================================================================
CREATE TABLE industries (
    industry_id         SERIAL PRIMARY KEY,
    industry_name       TEXT NOT NULL UNIQUE,
    status_id           INTEGER NOT NULL DEFAULT 1
                            REFERENCES statuses (status_id) ON DELETE RESTRICT,
    industry_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    industry_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_industries_status_id ON industries (status_id);

CREATE OR REPLACE FUNCTION set_industry_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.industry_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_industries_updated_at BEFORE UPDATE ON industries
    FOR EACH ROW EXECUTE FUNCTION set_industry_updated_at();


-- ================================================================
-- 4. roles
-- ================================================================
CREATE TABLE roles (
    role_id         SERIAL PRIMARY KEY,
    role_name       TEXT NOT NULL UNIQUE,
    role_scope      TEXT NOT NULL,
    role_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    role_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_roles_scope CHECK (role_scope IN ('platform', 'company'))
);

CREATE OR REPLACE FUNCTION set_role_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.role_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_roles_updated_at BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION set_role_updated_at();


-- ================================================================
-- 5. user_roles  (role assignment wrapper — users FK points here)
-- ================================================================
-- FK direction reversed: users.user_role_id points to this table.
-- Each user has exactly one user_role. Multiple users can share
-- the same user_role_id row (many-to-one from users).
-- ================================================================
CREATE TABLE user_roles (
    user_role_id  SERIAL PRIMARY KEY,
    role_id       INTEGER NOT NULL REFERENCES roles (role_id) ON DELETE RESTRICT,
    ur_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ur_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_user_roles_role_id ON user_roles (role_id);

CREATE OR REPLACE FUNCTION set_ur_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.ur_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_user_roles_updated_at BEFORE UPDATE ON user_roles
    FOR EACH ROW EXECUTE FUNCTION set_ur_updated_at();


-- ================================================================
-- 6. companies
-- ================================================================
CREATE TABLE companies (
    company_id              SERIAL PRIMARY KEY,
    industry_id             INTEGER
                                REFERENCES industries (industry_id) ON DELETE RESTRICT,
                                -- Nullable: signup flow creates a company before the
                                -- industry is picked. App prompts for industry later.
    company_name            TEXT NOT NULL,
    status_id               INTEGER NOT NULL DEFAULT 1
                                REFERENCES statuses (status_id) ON DELETE RESTRICT,
    company_engagement_date DATE,
    company_deleted_at      TIMESTAMPTZ,
    company_created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    company_updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_companies_industry_id      ON companies (industry_id);
CREATE INDEX idx_companies_status_id        ON companies (status_id);
CREATE INDEX idx_companies_engagement_date  ON companies (company_engagement_date)
    WHERE company_deleted_at IS NULL;

CREATE OR REPLACE FUNCTION set_company_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.company_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_companies_updated_at BEFORE UPDATE ON companies
    FOR EACH ROW EXECUTE FUNCTION set_company_updated_at();


-- ================================================================
-- 7. company_labels
-- ================================================================
CREATE TABLE company_labels (
    company_label_id SERIAL PRIMARY KEY,
    company_id       INTEGER NOT NULL
                         REFERENCES companies (company_id) ON DELETE CASCADE,
    cl_key           TEXT NOT NULL,
    cl_value         TEXT NOT NULL,
    cl_created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cl_updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_company_labels_key UNIQUE (company_id, cl_key)
);

CREATE INDEX idx_company_labels_company_id ON company_labels (company_id);

CREATE OR REPLACE FUNCTION set_cl_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.cl_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_company_labels_updated_at BEFORE UPDATE ON company_labels
    FOR EACH ROW EXECUTE FUNCTION set_cl_updated_at();


-- ================================================================
-- 8. locations
-- ================================================================
CREATE TABLE locations (
    location_id              SERIAL PRIMARY KEY,
    company_id               INTEGER NOT NULL
                                 REFERENCES companies (company_id) ON DELETE RESTRICT,
    location_name            TEXT NOT NULL,
    location_phone           TEXT,
    status_id                INTEGER NOT NULL DEFAULT 1
                                 REFERENCES statuses (status_id) ON DELETE RESTRICT,
    location_engagement_date DATE,
    location_deleted_at      TIMESTAMPTZ,
    location_created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    location_updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_locations_company_id        ON locations (company_id)
    WHERE location_deleted_at IS NULL;
CREATE INDEX idx_locations_status_id         ON locations (status_id);
CREATE INDEX idx_locations_engagement_date   ON locations (location_engagement_date)
    WHERE location_deleted_at IS NULL;

CREATE OR REPLACE FUNCTION set_location_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.location_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_locations_updated_at BEFORE UPDATE ON locations
    FOR EACH ROW EXECUTE FUNCTION set_location_updated_at();


-- ================================================================
-- 9. departments
-- ================================================================
CREATE TABLE departments (
    department_id         SERIAL PRIMARY KEY,
    company_id            INTEGER NOT NULL
                              REFERENCES companies (company_id) ON DELETE CASCADE,
    department_name       TEXT NOT NULL,
    status_id             INTEGER NOT NULL DEFAULT 1
                              REFERENCES statuses (status_id) ON DELETE RESTRICT,
    department_deleted_at TIMESTAMPTZ,
    department_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    department_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_departments_company_id ON departments (company_id)
    WHERE department_deleted_at IS NULL;
CREATE INDEX idx_departments_status_id  ON departments (status_id);

CREATE OR REPLACE FUNCTION set_department_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.department_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_departments_updated_at BEFORE UPDATE ON departments
    FOR EACH ROW EXECUTE FUNCTION set_department_updated_at();


-- ================================================================
-- 10. campaigns  (renamed from campaign_types)
-- ================================================================
-- Scoped to a single location. No company_id, no industry_id —
-- location_id is the only parent FK.
-- ================================================================
CREATE TABLE campaigns (
    campaign_id         SERIAL PRIMARY KEY,
    location_id         INTEGER NOT NULL
                            REFERENCES locations (location_id) ON DELETE CASCADE,
    campaign_name       TEXT NOT NULL,
    campaign_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    campaign_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_campaigns_location_id ON campaigns (location_id);

CREATE OR REPLACE FUNCTION set_campaign_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.campaign_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_campaigns_updated_at BEFORE UPDATE ON campaigns
    FOR EACH ROW EXECUTE FUNCTION set_campaign_updated_at();


-- ================================================================
-- 11. users
-- ================================================================
-- company_id removed (redundant). user_role_id FK added (direction
-- reversed from the previous junction pattern — each user has one
-- user_role).
-- ================================================================
CREATE TABLE users (
    user_id                    SERIAL PRIMARY KEY,
    user_role_id               INTEGER REFERENCES user_roles (user_role_id) ON DELETE SET NULL,
    department_id              INTEGER REFERENCES departments (department_id) ON DELETE SET NULL,
    user_email                 TEXT NOT NULL UNIQUE,
    user_password_hash         TEXT,
    user_first_name            TEXT NOT NULL,
    user_last_name             TEXT NOT NULL,
    user_auth_provider         TEXT,
    user_auth_provider_id      TEXT,
    status_id                  INTEGER NOT NULL DEFAULT 1
                                   REFERENCES statuses (status_id) ON DELETE RESTRICT,
    user_must_change_password  BOOLEAN NOT NULL DEFAULT FALSE,
    user_last_login_at         TIMESTAMPTZ,
    user_deleted_at            TIMESTAMPTZ,
    user_created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_users_user_role_id  ON users (user_role_id);
CREATE INDEX idx_users_department_id ON users (department_id)
    WHERE department_id IS NOT NULL;
CREATE INDEX idx_users_status_id     ON users (status_id);

CREATE OR REPLACE FUNCTION set_user_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.user_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_user_updated_at();


-- ================================================================
-- 12. rubric_groups
-- ================================================================
-- company_id removed. location_id is the sole parent FK (nullable
-- to preserve the industry-template pattern — templates have
-- NULL location_id and rg_source_industry_id set for lineage).
-- ================================================================
CREATE TABLE rubric_groups (
    rubric_group_id       SERIAL PRIMARY KEY,
    location_id           INTEGER REFERENCES locations (location_id) ON DELETE RESTRICT,
                              -- NULL = industry-level template
    rg_name               TEXT NOT NULL,
    rg_grade_target       TEXT NOT NULL,
    rg_source_industry_id INTEGER,   -- lineage only, NOT an FK
    status_id             INTEGER NOT NULL DEFAULT 1
                              REFERENCES statuses (status_id) ON DELETE RESTRICT,
    rg_deleted_at         TIMESTAMPTZ,
    rg_created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rg_updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_rubric_groups_grade_target
        CHECK (rg_grade_target IN ('caller', 'respondent'))
);

CREATE INDEX idx_rubric_groups_location_id ON rubric_groups (location_id)
    WHERE rg_deleted_at IS NULL;
CREATE INDEX idx_rubric_groups_status_id   ON rubric_groups (status_id);

CREATE OR REPLACE FUNCTION set_rg_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.rg_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_rubric_groups_updated_at BEFORE UPDATE ON rubric_groups
    FOR EACH ROW EXECUTE FUNCTION set_rg_updated_at();


-- ================================================================
-- 13. rubric_items
-- ================================================================
CREATE TABLE rubric_items (
    rubric_item_id       SERIAL PRIMARY KEY,
    rubric_group_id      INTEGER NOT NULL
                             REFERENCES rubric_groups (rubric_group_id) ON DELETE CASCADE,
    ri_name              TEXT NOT NULL,
    ri_score_type        TEXT NOT NULL,
    ri_weight            NUMERIC(5,2) NOT NULL DEFAULT 1.00,
    ri_scoring_guidance  TEXT,
    ri_order             INTEGER NOT NULL DEFAULT 0,
    status_id            INTEGER NOT NULL DEFAULT 1
                             REFERENCES statuses (status_id) ON DELETE RESTRICT,
    ri_deleted_at        TIMESTAMPTZ,
    ri_created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ri_updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_rubric_items_score_type
        CHECK (ri_score_type IN ('out_of_10', 'yes_no', 'yes_no_pending')),
    CONSTRAINT chk_rubric_items_weight_positive
        CHECK (ri_weight > 0)
);

CREATE INDEX idx_rubric_items_rubric_group_id ON rubric_items (rubric_group_id)
    WHERE ri_deleted_at IS NULL;
CREATE INDEX idx_rubric_items_status_id       ON rubric_items (status_id);

CREATE OR REPLACE FUNCTION set_ri_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.ri_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_rubric_items_updated_at BEFORE UPDATE ON rubric_items
    FOR EACH ROW EXECUTE FUNCTION set_ri_updated_at();


-- ================================================================
-- 14. projects
-- ================================================================
CREATE TABLE projects (
    project_id         SERIAL PRIMARY KEY,
    company_id         INTEGER NOT NULL
                           REFERENCES companies (company_id) ON DELETE RESTRICT,
    project_name       TEXT NOT NULL,
    campaign_id        INTEGER REFERENCES campaigns (campaign_id) ON DELETE SET NULL,
    rubric_group_id    INTEGER NOT NULL
                           REFERENCES rubric_groups (rubric_group_id) ON DELETE RESTRICT,
    project_start_date DATE NOT NULL,
    project_end_date   DATE,
    -- TRUE = the project spans every location in the company. In that case the
    -- project's location is chosen per-call at grade time, and the rubric
    -- group is created with location_id = NULL (shared across locations).
    project_all_locations BOOLEAN NOT NULL DEFAULT FALSE,
    status_id          INTEGER NOT NULL DEFAULT 1
                           REFERENCES statuses (status_id) ON DELETE RESTRICT,
    project_deleted_at TIMESTAMPTZ,
    project_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    project_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_projects_date_range
        CHECK (project_end_date IS NULL OR project_end_date >= project_start_date)
);

CREATE INDEX idx_projects_company_id            ON projects (company_id)
    WHERE project_deleted_at IS NULL;
CREATE INDEX idx_projects_company_id_status_id  ON projects (company_id, status_id)
    WHERE project_deleted_at IS NULL;
CREATE INDEX idx_projects_rubric_group_id       ON projects (rubric_group_id);
CREATE INDEX idx_projects_campaign_id           ON projects (campaign_id);
CREATE INDEX idx_projects_status_id             ON projects (status_id);

CREATE OR REPLACE FUNCTION set_project_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.project_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_project_updated_at();


-- ================================================================
-- 14b. respondents
-- ================================================================
-- External people detected from call transcripts (secret-shopping model).
-- Stored as free-text per company + location. A respondent is NOT a user.
-- Case-insensitive de-dup is enforced at upsert time in application code
-- (the UNIQUE constraint is case-sensitive; upsert helper lowercases the
-- match before deciding insert-vs-update).
-- ================================================================
CREATE TABLE respondents (
    respondent_id         SERIAL PRIMARY KEY,
    company_id            INTEGER NOT NULL REFERENCES companies (company_id) ON DELETE CASCADE,
    location_id           INTEGER REFERENCES locations (location_id) ON DELETE SET NULL,
    respondent_name       TEXT NOT NULL,
    respondent_call_count INTEGER NOT NULL DEFAULT 0,
    respondent_first_seen DATE,
    respondent_created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    respondent_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_respondents_name_location
        UNIQUE (company_id, location_id, respondent_name),
    CONSTRAINT chk_respondents_call_count
        CHECK (respondent_call_count >= 0)
);

CREATE INDEX idx_respondents_company_id  ON respondents (company_id);
CREATE INDEX idx_respondents_location_id ON respondents (location_id);

CREATE OR REPLACE FUNCTION set_respondent_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.respondent_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_respondents_updated_at BEFORE UPDATE ON respondents
    FOR EACH ROW EXECUTE FUNCTION set_respondent_updated_at();


-- ================================================================
-- 15. interactions
-- ================================================================
-- location_id and campaign_id both removed. Tenant/location scope
-- now derived through: interaction → project → campaign → location.
-- ================================================================
CREATE TABLE interactions (
    interaction_id                    SERIAL PRIMARY KEY,
    project_id                        INTEGER REFERENCES projects (project_id) ON DELETE SET NULL,
    caller_user_id                    INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
    respondent_user_id                INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
    respondent_id                     INTEGER REFERENCES respondents (respondent_id) ON DELETE SET NULL,
    interaction_date                  DATE NOT NULL,
    interaction_submitted_at          TIMESTAMPTZ,
    status_id                         INTEGER NOT NULL DEFAULT 45
                                          REFERENCES statuses (status_id) ON DELETE RESTRICT,
                                          -- 45 = 'pending' (interaction category)
    interaction_transcript            TEXT,
    interaction_audio_url             TEXT,
    interaction_audio_data            BYTEA,
    interaction_overall_score         NUMERIC(5,2),
    interaction_original_score        NUMERIC(5,2),
    interaction_regrade_count         INTEGER NOT NULL DEFAULT 0,
    interaction_regraded_with_context BOOLEAN NOT NULL DEFAULT FALSE,
    interaction_reviewer_context      TEXT,
    interaction_strengths             TEXT,
    interaction_weaknesses            TEXT,
    interaction_overall_assessment    TEXT,
    interaction_flags                 TEXT,
    interaction_responder_name        TEXT,
    interaction_call_start_time       TIMESTAMPTZ,
    interaction_call_end_time         TIMESTAMPTZ,
    interaction_call_duration_seconds INTEGER,
    interaction_uploaded_at           TIMESTAMPTZ,
    interaction_deleted_at            TIMESTAMPTZ,
    interaction_created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    interaction_updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_interactions_overall_score
        CHECK (interaction_overall_score IS NULL
            OR (interaction_overall_score >= 0 AND interaction_overall_score <= 10)),
    CONSTRAINT chk_interactions_original_score
        CHECK (interaction_original_score IS NULL
            OR (interaction_original_score >= 0 AND interaction_original_score <= 10)),
    CONSTRAINT chk_interactions_regrade_count
        CHECK (interaction_regrade_count >= 0)
);

CREATE INDEX idx_interactions_project_id          ON interactions (project_id)
    WHERE interaction_deleted_at IS NULL;
CREATE INDEX idx_interactions_project_id_date     ON interactions (project_id, interaction_date DESC)
    WHERE interaction_deleted_at IS NULL;
CREATE INDEX idx_interactions_status_id           ON interactions (status_id)
    WHERE interaction_deleted_at IS NULL;
CREATE INDEX idx_interactions_caller_user_id      ON interactions (caller_user_id);
CREATE INDEX idx_interactions_respondent_user_id  ON interactions (respondent_user_id);
CREATE INDEX idx_interactions_respondent_id       ON interactions (respondent_id);

CREATE OR REPLACE FUNCTION set_interaction_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.interaction_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_interactions_updated_at BEFORE UPDATE ON interactions
    FOR EACH ROW EXECUTE FUNCTION set_interaction_updated_at();


-- ================================================================
-- 16. interaction_rubric_scores
-- ================================================================
CREATE TABLE interaction_rubric_scores (
    interaction_rubric_score_id SERIAL PRIMARY KEY,
    interaction_id              INTEGER NOT NULL
                                    REFERENCES interactions (interaction_id) ON DELETE CASCADE,
    rubric_item_id              INTEGER
                                    REFERENCES rubric_items (rubric_item_id) ON DELETE SET NULL,
    -- SNAPSHOT: frozen at grade time, write-once, never updated --
    irs_snapshot_name              TEXT NOT NULL,
    irs_snapshot_score_type        TEXT NOT NULL,
    irs_snapshot_weight            NUMERIC(5,2) NOT NULL,
    irs_snapshot_scoring_guidance  TEXT,
    -- Score data: updated on regrade --
    irs_score_value                NUMERIC(5,2) NOT NULL,
    irs_score_ai_explanation       TEXT,
    irs_created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    irs_updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_irs_snapshot_score_type
        CHECK (irs_snapshot_score_type IN ('out_of_10', 'yes_no', 'yes_no_pending')),
    CONSTRAINT chk_irs_snapshot_weight
        CHECK (irs_snapshot_weight > 0),
    CONSTRAINT chk_irs_score_value
        CHECK (irs_score_value >= 0 AND irs_score_value <= 10)
);

CREATE INDEX idx_irs_interaction_id ON interaction_rubric_scores (interaction_id);
CREATE INDEX idx_irs_rubric_item_id ON interaction_rubric_scores (rubric_item_id);

CREATE OR REPLACE FUNCTION set_irs_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.irs_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_irs_updated_at BEFORE UPDATE ON interaction_rubric_scores
    FOR EACH ROW EXECUTE FUNCTION set_irs_updated_at();


-- ================================================================
-- 17. clarifying_questions
-- ================================================================
CREATE TABLE clarifying_questions (
    clarifying_question_id SERIAL PRIMARY KEY,
    interaction_id         INTEGER NOT NULL
                               REFERENCES interactions (interaction_id) ON DELETE CASCADE,
    cq_text                TEXT NOT NULL,
    cq_ai_reason           TEXT NOT NULL,
    cq_response_format     TEXT NOT NULL,
    cq_answer_value        TEXT,
    cq_order               INTEGER NOT NULL,
    cq_created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cq_updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_cq_response_format
        CHECK (cq_response_format IN ('yes_no', 'scale_1_10', 'multiple_choice'))
);

CREATE INDEX idx_cq_interaction_id ON clarifying_questions (interaction_id);

CREATE OR REPLACE FUNCTION set_cq_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.cq_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_cq_updated_at BEFORE UPDATE ON clarifying_questions
    FOR EACH ROW EXECUTE FUNCTION set_cq_updated_at();


-- ================================================================
-- 18. performance_reports
-- ================================================================
CREATE TABLE performance_reports (
    performance_report_id        SERIAL PRIMARY KEY,
    subject_user_id              INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
    respondent_id                INTEGER REFERENCES respondents (respondent_id) ON DELETE SET NULL,
    pr_data                      JSONB NOT NULL DEFAULT '{}',
    pr_average_score             NUMERIC(5,2),
    pr_call_count                INTEGER NOT NULL DEFAULT 0,
    pr_processed_interaction_ids JSONB NOT NULL DEFAULT '[]',
    pr_created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pr_updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_pr_avg_score
        CHECK (pr_average_score IS NULL
            OR (pr_average_score >= 0 AND pr_average_score <= 10)),
    CONSTRAINT chk_pr_call_count
        CHECK (pr_call_count >= 0),
    -- Exactly one of subject_user_id / respondent_id should be set per
    -- report (known-user reports vs secret-shopping respondent reports).
    CONSTRAINT chk_pr_subject_xor
        CHECK ((subject_user_id IS NOT NULL)::int + (respondent_id IS NOT NULL)::int <= 1),
    CONSTRAINT uq_performance_reports_subject   UNIQUE (subject_user_id),
    CONSTRAINT uq_performance_reports_respondent UNIQUE (respondent_id)
);

CREATE INDEX idx_performance_reports_subject_user_id ON performance_reports (subject_user_id);
CREATE INDEX idx_performance_reports_respondent_id   ON performance_reports (respondent_id);

CREATE OR REPLACE FUNCTION set_pr_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.pr_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_performance_reports_updated_at BEFORE UPDATE ON performance_reports
    FOR EACH ROW EXECUTE FUNCTION set_pr_updated_at();


-- ================================================================
-- 19. api_keys
-- ================================================================
CREATE TABLE api_keys (
    api_key_id       SERIAL PRIMARY KEY,
    company_id       INTEGER NOT NULL
                         REFERENCES companies (company_id) ON DELETE CASCADE,
    ak_prefix        TEXT NOT NULL,
    ak_hash          TEXT NOT NULL,
    ak_name          TEXT NOT NULL,
    status_id        INTEGER NOT NULL DEFAULT 1
                         REFERENCES statuses (status_id) ON DELETE RESTRICT,
    ak_last_used_at  TIMESTAMPTZ,
    ak_revoked_at    TIMESTAMPTZ,
    ak_created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ak_updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_api_keys_company_id ON api_keys (company_id);
CREATE INDEX idx_api_keys_prefix     ON api_keys (ak_prefix);
CREATE INDEX idx_api_keys_status_id  ON api_keys (status_id);

CREATE OR REPLACE FUNCTION set_ak_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.ak_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_api_keys_updated_at BEFORE UPDATE ON api_keys
    FOR EACH ROW EXECUTE FUNCTION set_ak_updated_at();


-- ================================================================
-- 20. api_usage
-- ================================================================
CREATE TABLE api_usage (
    api_usage_id      SERIAL PRIMARY KEY,
    company_id        INTEGER NOT NULL
                          REFERENCES companies (company_id) ON DELETE CASCADE,
    au_service        TEXT NOT NULL,
    au_period_start   TIMESTAMPTZ NOT NULL,
    au_period_type    TEXT NOT NULL,
    au_request_count  INTEGER NOT NULL DEFAULT 0,
    au_created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    au_updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_api_usage_period_type
        CHECK (au_period_type IN ('hour', 'day')),
    CONSTRAINT chk_api_usage_count
        CHECK (au_request_count >= 0),
    CONSTRAINT uq_api_usage UNIQUE (company_id, au_service, au_period_start, au_period_type)
);

CREATE INDEX idx_api_usage_company_id ON api_usage (company_id);

CREATE OR REPLACE FUNCTION set_au_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.au_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_api_usage_updated_at BEFORE UPDATE ON api_usage
    FOR EACH ROW EXECUTE FUNCTION set_au_updated_at();


-- ================================================================
-- 21. api_call_log  (append-only)
-- ================================================================
CREATE TABLE api_call_log (
    api_call_log_id      BIGSERIAL PRIMARY KEY,
    company_id           INTEGER NOT NULL
                             REFERENCES companies (company_id) ON DELETE CASCADE,
    acl_service          TEXT NOT NULL,
    acl_called_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acl_response_status  TEXT,
    acl_latency_ms       INTEGER,
    interaction_id       INTEGER REFERENCES interactions (interaction_id) ON DELETE SET NULL,
    acl_created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acl_updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_api_call_log_company_id_called_at
    ON api_call_log (company_id, acl_called_at);
CREATE INDEX idx_api_call_log_interaction_id
    ON api_call_log (interaction_id);

CREATE OR REPLACE FUNCTION set_acl_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.acl_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_api_call_log_updated_at BEFORE UPDATE ON api_call_log
    FOR EACH ROW EXECUTE FUNCTION set_acl_updated_at();


-- ================================================================
-- 22. audit_log  (append-only; company_id removed)
-- ================================================================
-- audit_log_action_type and audit_log_target_entity_type are now
-- integer FKs to lookup tables instead of free-text columns.
-- ================================================================
CREATE TABLE audit_log (
    audit_log_id                     BIGSERIAL PRIMARY KEY,
    actor_user_id                    INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
    audit_log_action_type_id         INTEGER NOT NULL
                                         REFERENCES audit_log_action_types (audit_log_action_type_id) ON DELETE RESTRICT,
    audit_log_target_entity_type_id  INTEGER
                                         REFERENCES audit_log_target_entity_types (audit_log_target_entity_type_id) ON DELETE RESTRICT,
    al_target_entity_id              TEXT,   -- PK value of the targeted row (any table)
    al_metadata                      JSONB,  -- old_value, new_value, ip, user_agent, etc.
    al_created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    al_updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_log_actor_user_id                   ON audit_log (actor_user_id);
CREATE INDEX idx_audit_log_action_type_id                  ON audit_log (audit_log_action_type_id);
CREATE INDEX idx_audit_log_target_entity_type_id           ON audit_log (audit_log_target_entity_type_id);
CREATE INDEX idx_audit_log_target
    ON audit_log (audit_log_target_entity_type_id, al_target_entity_id);
CREATE INDEX idx_audit_log_created_at                      ON audit_log (al_created_at);

CREATE OR REPLACE FUNCTION set_al_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.al_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_audit_log_updated_at BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION set_al_updated_at();


-- ================================================================
-- 23. voip_configs  (Phase 5 — one per company VoIP connection)
-- ================================================================
CREATE TABLE voip_configs (
    voip_config_id             SERIAL PRIMARY KEY,
    company_id                 INTEGER NOT NULL
                                   REFERENCES companies (company_id) ON DELETE CASCADE,
    voip_config_provider       TEXT NOT NULL,
    voip_config_credentials    JSONB NOT NULL DEFAULT '{}',
    voip_config_auto_grade     BOOLEAN NOT NULL DEFAULT FALSE,
    voip_config_webhook_secret TEXT,
    voip_config_is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    voip_config_created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    voip_config_updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_voip_configs_company UNIQUE (company_id),
    CONSTRAINT chk_voip_config_provider CHECK (
        voip_config_provider IN (
            'ringcentral', 'dialpad', 'aircall',
            'zoom_phone', 'eight_by_eight', 'generic_webhook'
        )
    )
);

CREATE INDEX idx_voip_configs_company_id ON voip_configs (company_id);

CREATE OR REPLACE FUNCTION set_voip_config_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.voip_config_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_voip_configs_updated_at BEFORE UPDATE ON voip_configs
    FOR EACH ROW EXECUTE FUNCTION set_voip_config_updated_at();


-- ================================================================
-- 24. voip_call_queue  (Phase 5 — incoming calls awaiting review/grade)
-- ================================================================
CREATE TABLE voip_call_queue (
    voip_queue_id                SERIAL PRIMARY KEY,
    company_id                   INTEGER NOT NULL
                                     REFERENCES companies (company_id) ON DELETE CASCADE,
    voip_queue_provider          TEXT NOT NULL,
    voip_queue_call_id           TEXT NOT NULL,
    voip_queue_recording_url     TEXT,
    voip_queue_recording_data    BYTEA,
    voip_queue_caller_number     TEXT,
    voip_queue_called_number     TEXT,
    voip_queue_call_date         DATE,
    voip_queue_duration_seconds  INTEGER,
    voip_queue_raw_payload       JSONB,
    voip_queue_status            TEXT NOT NULL DEFAULT 'pending',
    voip_queue_error             TEXT,
    voip_queue_interaction_id    INTEGER REFERENCES interactions (interaction_id) ON DELETE SET NULL,
    voip_queue_created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    voip_queue_updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_voip_queue_call UNIQUE (company_id, voip_queue_provider, voip_queue_call_id),
    CONSTRAINT chk_voip_queue_status CHECK (
        voip_queue_status IN ('pending', 'processing', 'graded', 'failed', 'skipped')
    )
);

CREATE INDEX idx_voip_call_queue_company_id ON voip_call_queue (company_id);
CREATE INDEX idx_voip_call_queue_status     ON voip_call_queue (voip_queue_status);

CREATE OR REPLACE FUNCTION set_voip_queue_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.voip_queue_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_voip_call_queue_updated_at BEFORE UPDATE ON voip_call_queue
    FOR EACH ROW EXECUTE FUNCTION set_voip_queue_updated_at();


-- ================================================================
-- 25. company_settings  (Phase 6 — per-company key/value config)
-- ================================================================
CREATE TABLE company_settings (
    company_setting_id         SERIAL PRIMARY KEY,
    company_id                 INTEGER NOT NULL
                                   REFERENCES companies (company_id) ON DELETE CASCADE,
    company_setting_key        TEXT NOT NULL,
    company_setting_value      TEXT NOT NULL,
    company_setting_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_company_settings_key UNIQUE (company_id, company_setting_key)
);

CREATE INDEX idx_company_settings_company_id ON company_settings (company_id);

CREATE OR REPLACE FUNCTION set_company_setting_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.company_setting_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_company_settings_updated_at BEFORE UPDATE ON company_settings
    FOR EACH ROW EXECUTE FUNCTION set_company_setting_updated_at();


-- ================================================================
-- 26. location_intel  (Phase 8 — per-location pre-call briefing)
-- ================================================================
-- One row per (location, company). Updated in a daemon background thread
-- after every successful grade. Stats columns are derived from the call
-- history; the AI columns (summary/strengths/weaknesses) are produced by
-- Claude on each refresh.
CREATE TABLE location_intel (
    location_intel_id       SERIAL PRIMARY KEY,
    location_id             INTEGER NOT NULL
                                REFERENCES locations (location_id) ON DELETE CASCADE,
    company_id              INTEGER NOT NULL
                                REFERENCES companies (company_id) ON DELETE CASCADE,
    li_last_call_date       DATE,
    li_last_call_time       TIMESTAMPTZ,
    li_last_call_score      NUMERIC(5,2),
    li_last_call_outcome    TEXT,
    li_total_calls          INTEGER NOT NULL DEFAULT 0,
    li_avg_score            NUMERIC(5,2),
    li_no_answer_count      INTEGER NOT NULL DEFAULT 0,
    li_summary              TEXT,
    li_strengths            TEXT,
    li_weaknesses           TEXT,
    li_last_computed_at     TIMESTAMPTZ,
    li_updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_location_intel UNIQUE (location_id, company_id)
);

CREATE INDEX idx_location_intel_location_id ON location_intel (location_id);
CREATE INDEX idx_location_intel_company_id  ON location_intel (company_id);

CREATE OR REPLACE FUNCTION set_location_intel_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.li_updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_location_intel_updated_at BEFORE UPDATE ON location_intel
    FOR EACH ROW EXECUTE FUNCTION set_location_intel_updated_at();
