-- ============================================================================
-- Migration: 2026-06-10 — rubric_groups.rg_reference_script
--
-- Adds a nullable free-text column holding a reference script that the GRADED
-- person (caller or respondent, per rg_grade_target) is expected to follow.
-- The grader injects this text into the Claude grading prompt so the AI scores
-- how closely the graded person adhered to the script. NULL/blank means "no
-- script" and the grader simply omits that block (existing behavior).
--
-- Conceptually owned by the rubric group: the grader already loads the
-- rubric_groups row when building the prompt (interactions_routes._load_rubric_group,
-- grade_jobs._load_criteria_for_project, voip/processor project SELECTs), so
-- this column rides along on queries that already run — no new joins.
--
-- HOW TO RUN
--   Run against the production Postgres database BEFORE deploying the code that
--   reads/writes the column. The PR's schema.sql update covers fresh bootstraps;
--   this migration covers the existing production database.
--
-- The statement is idempotent (IF NOT EXISTS), so re-running is safe. No
-- length CHECK so multi-paragraph scripts are unconstrained. Nullable, no
-- default — older code that doesn't select it is unaffected.
-- ============================================================================

ALTER TABLE rubric_groups
    ADD COLUMN IF NOT EXISTS rg_reference_script TEXT NULL;
