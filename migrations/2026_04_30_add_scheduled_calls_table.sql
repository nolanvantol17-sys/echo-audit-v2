-- Arc C — D1: scheduled_calls audit table for outbound AI shop scheduling.
-- Tenant scope is DERIVED via sc_location_id → locations.company_id; no
-- sc_company_id column. Stored statuses are TERMINAL only; intermediate
-- display states (webhook_received, processing, timeout) derive at poll
-- time from the join with voip_call_queue + interactions.

CREATE TABLE scheduled_calls (
    sc_id                   BIGSERIAL PRIMARY KEY,
    sc_location_id          INTEGER NOT NULL
                                REFERENCES locations (location_id) ON DELETE RESTRICT,
    sc_project_id           INTEGER NOT NULL
                                REFERENCES projects  (project_id)  ON DELETE RESTRICT,
    sc_campaign_id          INTEGER
                                REFERENCES campaigns (campaign_id) ON DELETE SET NULL,
    sc_caller_user_id       INTEGER NOT NULL
                                REFERENCES users     (user_id)     ON DELETE RESTRICT,
    sc_requested_by_user_id INTEGER NOT NULL
                                REFERENCES users     (user_id)     ON DELETE RESTRICT,
    sc_conversation_id      TEXT,
    sc_phone_number         TEXT NOT NULL,
    sc_status               TEXT NOT NULL DEFAULT 'initiated',
    sc_status_message       TEXT,
    sc_ai_caller_response   JSONB,
    sc_requested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sc_completed_at         TIMESTAMPTZ,

    CONSTRAINT chk_sc_status CHECK (
        sc_status IN ('initiated', 'graded', 'no_answer', 'failed')
    )
);

CREATE INDEX idx_scheduled_calls_location_id     ON scheduled_calls (sc_location_id);
CREATE INDEX idx_scheduled_calls_conversation_id ON scheduled_calls (sc_conversation_id);
CREATE INDEX idx_scheduled_calls_requested_at    ON scheduled_calls (sc_requested_at DESC);

-- Audit log lookup seeds — idempotent.
INSERT INTO audit_log_action_types (audit_log_action_type_id, audit_log_action_type_name)
VALUES (9, 'scheduled_ai_shop')
ON CONFLICT (audit_log_action_type_id) DO NOTHING;

INSERT INTO audit_log_target_entity_types (audit_log_target_entity_type_id, audit_log_target_entity_type_name)
VALUES (12, 'scheduled_call')
ON CONFLICT (audit_log_target_entity_type_id) DO NOTHING;
