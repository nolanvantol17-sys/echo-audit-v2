-- ================================================================
-- 2026-04-29 — Allow 'elevenlabs' as a voip_configs provider value
-- ================================================================
-- The original CHECK constraint enumerates a fixed allowlist. Drop and
-- recreate it with 'elevenlabs' added so we can store the Mayfair
-- AI-caller webhook config alongside the existing telephony providers.
--
-- Safe to run on prod 2026-04-29: voip_configs is currently empty
-- (verified before applying), so no row could violate the new constraint.
-- One-shot; not idempotent on its own (dropping a non-existent constraint
-- raises). Re-running is a no-op only if the constraint name was already
-- updated to the new shape.
-- ================================================================

ALTER TABLE voip_configs
    DROP CONSTRAINT chk_voip_config_provider;

ALTER TABLE voip_configs
    ADD CONSTRAINT chk_voip_config_provider CHECK (
        voip_config_provider IN (
            'ringcentral',
            'dialpad',
            'aircall',
            'zoom_phone',
            'eight_by_eight',
            'generic_webhook',
            'elevenlabs'
        )
    );
