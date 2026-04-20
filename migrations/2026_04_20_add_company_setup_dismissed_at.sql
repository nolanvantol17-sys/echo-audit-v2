-- ============================================================================
-- Migration: 2026-04-20 — companies.company_setup_dismissed_at
--
-- Adds a nullable timestamp recording when the admin dismissed the post-signup
-- setup wizard. NULL means "never dismissed"; a non-NULL value (combined with
-- the data-presence checks in the /app/setup handler) suppresses the wizard
-- on subsequent logins.
--
-- HOW TO RUN
--   Run this against the production Postgres database BEFORE deploying the
--   code that reads/writes the column. The PR's schema.sql update covers new
--   bootstraps; this migration covers the existing production database that
--   was created from the prior schema.
--
-- The statement is idempotent (IF NOT EXISTS), so re-running is safe.
-- ============================================================================

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS company_setup_dismissed_at TIMESTAMPTZ NULL;
