-- ================================================================
-- 2026-04-30 — voip_call_queue attribution + provided-transcript columns
-- ================================================================
-- Multi-format ingestion: ElevenLabs supplies attribution IDs
-- (echo_audit_project_id / location_id / campaign_id / caller_user_id)
-- and a pre-transcribed transcript via webhook dynamic_variables. The
-- processor honors these when present and skips the AAI transcribe
-- step entirely. If a third transcript-providing source arrives,
-- revisit this design — these may want to be a polymorphic side
-- table rather than five ElevenLabs-shaped columns on a generic queue.
--
-- Safe to run on prod 2026-04-30: voip_call_queue holds 1 row total
-- (one ElevenLabs discovery test). All new columns are nullable;
-- existing row + future non-ElevenLabs providers continue to work
-- unchanged. One-shot; not idempotent on its own (re-running raises
-- on duplicate column).
-- ================================================================

ALTER TABLE voip_call_queue
    ADD COLUMN voip_queue_project_id INTEGER
        REFERENCES projects(project_id)   ON DELETE SET NULL,
    ADD COLUMN voip_queue_location_id INTEGER
        REFERENCES locations(location_id) ON DELETE SET NULL,
    ADD COLUMN voip_queue_campaign_id INTEGER
        REFERENCES campaigns(campaign_id) ON DELETE SET NULL,
    ADD COLUMN voip_queue_caller_user_id INTEGER
        REFERENCES users(user_id)         ON DELETE SET NULL,
    ADD COLUMN voip_queue_provided_transcript TEXT;
