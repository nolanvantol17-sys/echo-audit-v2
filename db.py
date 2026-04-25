"""
db.py — Database abstraction for Echo Audit V2.

Supports PostgreSQL in production (via DATABASE_URL) and SQLite locally.
Schema creation is driven entirely by schema.sql — no CREATE TABLE statements
in Python. setup_db() runs schema.sql once if the schema hasn't been
initialized yet (detected by checking for the statuses table).

Preserves the V1 connection pattern:
    - IS_POSTGRES module flag
    - get_conn() returns a connection (caller must close)
    - q() helper translates SQLite-style ? placeholders and INSERT OR IGNORE
      to PostgreSQL equivalents

Note on SQLite: schema.sql uses PostgreSQL-specific features (TIMESTAMPTZ,
JSONB, SERIAL, plpgsql triggers). setup_db() will fail on SQLite. For local
development against SQLite, apply schema manually or use PostgreSQL.
"""

import os
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_POSTGRES = bool(DATABASE_URL)
SQLITE_PATH = Path(__file__).parent.resolve() / "echoaudit.db"
SCHEMA_PATH = Path(__file__).parent.resolve() / "schema.sql"


# ── Connection management ───────────────────────────────────────


class _PgConnWrapper:
    """Wraps a psycopg2 connection so conn.execute() mirrors sqlite3 behavior."""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):    self._conn.commit()
    def rollback(self):  self._conn.rollback()
    def close(self):     self._conn.close()
    def cursor(self):    return self._conn.cursor()

    @property
    def autocommit(self):        return self._conn.autocommit
    @autocommit.setter
    def autocommit(self, val):   self._conn.autocommit = val


def get_conn():
    """Return a new database connection. Caller must close() it."""
    if IS_POSTGRES:
        import psycopg2
        import psycopg2.extras

        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if "sslmode" not in url:
            url += "?sslmode=require" if "?" not in url else "&sslmode=require"

        raw = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        raw.autocommit = False
        return _PgConnWrapper(raw)
    else:
        conn = sqlite3.connect(str(SQLITE_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


@contextmanager
def get_managed_conn():
    """Context manager form of get_conn() — auto-closes on exit."""
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def q(sql):
    """Translate SQLite-style SQL to PostgreSQL when IS_POSTGRES is set.

    Translations:
        ?              → %s
        INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
        COLLATE NOCASE → removed (use LOWER() in app-level queries)
    """
    if not IS_POSTGRES:
        return sql
    result = sql.replace("?", "%s")
    if "INSERT OR IGNORE" in result.upper():
        result = result.replace("INSERT OR IGNORE", "INSERT")
        result = result.replace("insert or ignore", "INSERT")
        result = result.rstrip().rstrip(";")
        result += " ON CONFLICT DO NOTHING"
    result = result.replace("COLLATE NOCASE", "")
    return result


# ── Schema initialization ───────────────────────────────────────


def _schema_initialized(conn):
    """Return True if schema.sql has already been applied (statuses table exists)."""
    if IS_POSTGRES:
        cur = conn.execute("SELECT to_regclass('public.statuses') AS reg")
        row = cur.fetchone()
        return row is not None and row["reg"] is not None
    else:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='statuses'")
        return cur.fetchone() is not None


def setup_db():
    """Run schema.sql to create all tables if the schema hasn't been initialized.

    Idempotent by sentinel check — re-running is a no-op after first success.
    Additive column migrations run every time (IF NOT EXISTS guards).
    """
    conn = get_conn()
    try:
        if not _schema_initialized(conn):
            if not SCHEMA_PATH.exists():
                raise RuntimeError(f"schema.sql not found at {SCHEMA_PATH}")

            schema_sql = SCHEMA_PATH.read_text()
            logger.info("Applying schema.sql (first-time setup)")
            conn.execute(schema_sql)
            conn.commit()
            logger.info("schema.sql applied successfully")
        else:
            logger.info("Schema already initialized — skipping full schema.sql")

        _apply_additive_migrations(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Additive column migrations — run every startup, idempotent. Use ADD COLUMN
# IF NOT EXISTS so re-running is a safe no-op. Keep these append-only; never
# remove a migration here. New tables / non-additive changes belong in schema.sql.
_ADDITIVE_MIGRATIONS = [
    # Phase 3: grade result + audio blob storage on interactions
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_audio_data BYTEA",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_strengths TEXT",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_weaknesses TEXT",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_overall_assessment TEXT",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_flags TEXT",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_responder_name TEXT",

    # PR 5 / Phase 2: post-signup setup wizard dismissal flag.
    # Mirrors migrations/2026_04_20_add_company_setup_dismissed_at.sql so
    # dev/CI databases pick it up automatically; production gets it from the
    # one-off migration script applied before the deploy.
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS company_setup_dismissed_at TIMESTAMPTZ",

    # Phase 5: VoIP integration tables.
    # Full CREATE TABLE IF NOT EXISTS so this also lands on DBs that already
    # ran the pre-Phase-5 schema.sql (sentinel-gated on `statuses` which pre-dates
    # these tables).
    """CREATE TABLE IF NOT EXISTS voip_configs (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_voip_configs_company_id ON voip_configs (company_id)",
    """CREATE TABLE IF NOT EXISTS voip_call_queue (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_voip_call_queue_company_id ON voip_call_queue (company_id)",
    "CREATE INDEX IF NOT EXISTS idx_voip_call_queue_status     ON voip_call_queue (voip_queue_status)",
    # Phase 6: company_settings
    """CREATE TABLE IF NOT EXISTS company_settings (
        company_setting_id         SERIAL PRIMARY KEY,
        company_id                 INTEGER NOT NULL
                                       REFERENCES companies (company_id) ON DELETE CASCADE,
        company_setting_key        TEXT NOT NULL,
        company_setting_value      TEXT NOT NULL,
        company_setting_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_company_settings_key UNIQUE (company_id, company_setting_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_company_settings_company_id ON company_settings (company_id)",
    """CREATE OR REPLACE FUNCTION set_company_setting_updated_at() RETURNS TRIGGER AS $$
       BEGIN NEW.company_setting_updated_at = NOW(); RETURN NEW; END;
       $$ LANGUAGE plpgsql""",
    """DO $$ BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_company_settings_updated_at') THEN
               CREATE TRIGGER trg_company_settings_updated_at BEFORE UPDATE ON company_settings
                   FOR EACH ROW EXECUTE FUNCTION set_company_setting_updated_at();
           END IF;
       END $$""",
    # Triggers for updated_at — CREATE OR REPLACE the function, but the trigger
    # itself needs a guard to avoid "already exists" on re-run.
    """CREATE OR REPLACE FUNCTION set_voip_config_updated_at() RETURNS TRIGGER AS $$
       BEGIN NEW.voip_config_updated_at = NOW(); RETURN NEW; END;
       $$ LANGUAGE plpgsql""",
    """DO $$ BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_voip_configs_updated_at') THEN
               CREATE TRIGGER trg_voip_configs_updated_at BEFORE UPDATE ON voip_configs
                   FOR EACH ROW EXECUTE FUNCTION set_voip_config_updated_at();
           END IF;
       END $$""",
    """CREATE OR REPLACE FUNCTION set_voip_queue_updated_at() RETURNS TRIGGER AS $$
       BEGIN NEW.voip_queue_updated_at = NOW(); RETURN NEW; END;
       $$ LANGUAGE plpgsql""",
    """DO $$ BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_voip_call_queue_updated_at') THEN
               CREATE TRIGGER trg_voip_call_queue_updated_at BEFORE UPDATE ON voip_call_queue
                   FOR EACH ROW EXECUTE FUNCTION set_voip_queue_updated_at();
           END IF;
       END $$""",

    # Phase 7: respondents — external people detected from transcripts for
    # secret-shopping grading. A respondent is NOT a user. Scoped per company
    # with optional location. Case-insensitive de-dup is enforced in app code.
    """CREATE TABLE IF NOT EXISTS respondents (
        respondent_id         SERIAL PRIMARY KEY,
        company_id            INTEGER NOT NULL
                                  REFERENCES companies (company_id) ON DELETE CASCADE,
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_respondents_company_id  ON respondents (company_id)",
    "CREATE INDEX IF NOT EXISTS idx_respondents_location_id ON respondents (location_id)",
    """CREATE OR REPLACE FUNCTION set_respondent_updated_at() RETURNS TRIGGER AS $$
       BEGIN NEW.respondent_updated_at = NOW(); RETURN NEW; END;
       $$ LANGUAGE plpgsql""",
    """DO $$ BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_respondents_updated_at') THEN
               CREATE TRIGGER trg_respondents_updated_at BEFORE UPDATE ON respondents
                   FOR EACH ROW EXECUTE FUNCTION set_respondent_updated_at();
           END IF;
       END $$""",

    # Interaction FK to respondents (nullable, ON DELETE SET NULL).
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS respondent_id INTEGER",
    """DO $$ BEGIN
           IF NOT EXISTS (
               SELECT 1 FROM pg_constraint WHERE conname = 'fk_interactions_respondent_id'
           ) THEN
               ALTER TABLE interactions
                   ADD CONSTRAINT fk_interactions_respondent_id
                   FOREIGN KEY (respondent_id) REFERENCES respondents (respondent_id)
                   ON DELETE SET NULL;
           END IF;
       END $$""",
    "CREATE INDEX IF NOT EXISTS idx_interactions_respondent_id ON interactions (respondent_id)",

    # Performance reports: add respondent_id for secret-shopping reports.
    "ALTER TABLE performance_reports ADD COLUMN IF NOT EXISTS respondent_id INTEGER",
    """DO $$ BEGIN
           IF NOT EXISTS (
               SELECT 1 FROM pg_constraint WHERE conname = 'fk_performance_reports_respondent_id'
           ) THEN
               ALTER TABLE performance_reports
                   ADD CONSTRAINT fk_performance_reports_respondent_id
                   FOREIGN KEY (respondent_id) REFERENCES respondents (respondent_id)
                   ON DELETE SET NULL;
           END IF;
       END $$""",
    """DO $$ BEGIN
           IF NOT EXISTS (
               SELECT 1 FROM pg_constraint WHERE conname = 'uq_performance_reports_respondent'
           ) THEN
               ALTER TABLE performance_reports
                   ADD CONSTRAINT uq_performance_reports_respondent UNIQUE (respondent_id);
           END IF;
       END $$""",
    "CREATE INDEX IF NOT EXISTS idx_performance_reports_respondent_id ON performance_reports (respondent_id)",

    # Phase 7: projects can span every company location — "All Locations" mode.
    # When TRUE, the project has no specific location (rubric_group.location_id
    # is NULL and grading picks the location per call).
    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS project_all_locations BOOLEAN NOT NULL DEFAULT FALSE",

    # Phase 8: call timestamps. interaction_uploaded_at is set on every grade
    # submission (server-side NOW()). The other three are populated only for
    # live recordings — uploaded files leave them NULL.
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_call_start_time       TIMESTAMPTZ",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_call_end_time         TIMESTAMPTZ",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_call_duration_seconds INTEGER",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS interaction_uploaded_at           TIMESTAMPTZ",

    # Phase 8: location_intel — per-location pre-call briefing. Updated in a
    # background thread after every successful grade. One row per (location, company).
    """CREATE TABLE IF NOT EXISTS location_intel (
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
    )""",
    "CREATE INDEX IF NOT EXISTS idx_location_intel_location_id ON location_intel (location_id)",
    "CREATE INDEX IF NOT EXISTS idx_location_intel_company_id  ON location_intel (company_id)",
    """CREATE OR REPLACE FUNCTION set_location_intel_updated_at() RETURNS TRIGGER AS $$
       BEGIN NEW.li_updated_at = NOW(); RETURN NEW; END;
       $$ LANGUAGE plpgsql""",
    """DO $$ BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_location_intel_updated_at') THEN
               CREATE TRIGGER trg_location_intel_updated_at BEFORE UPDATE ON location_intel
                   FOR EACH ROW EXECUTE FUNCTION set_location_intel_updated_at();
           END IF;
       END $$""",

    # Per-company custom vocabulary for the transcription engine. Sent as
    # keyterms_prompt on every transcription request for the owning company.
    """CREATE TABLE IF NOT EXISTS transcription_hints (
        transcription_hint_id   SERIAL PRIMARY KEY,
        company_id              INTEGER NOT NULL
                                    REFERENCES companies (company_id) ON DELETE CASCADE,
        th_term                 TEXT NOT NULL,
        status_id               INTEGER NOT NULL DEFAULT 1
                                    REFERENCES statuses (status_id) ON DELETE RESTRICT,
        th_deleted_at           TIMESTAMPTZ,
        th_created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        th_updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_transcription_hints_term UNIQUE (company_id, th_term),
        CONSTRAINT chk_th_term_length CHECK (char_length(th_term) BETWEEN 5 AND 50)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_transcription_hints_company_id "
    "ON transcription_hints (company_id) WHERE th_deleted_at IS NULL",
    """CREATE OR REPLACE FUNCTION set_th_updated_at() RETURNS TRIGGER AS $$
       BEGIN NEW.th_updated_at = NOW(); RETURN NEW; END;
       $$ LANGUAGE plpgsql""",
    """DO $$ BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_transcription_hints_updated_at') THEN
               CREATE TRIGGER trg_transcription_hints_updated_at BEFORE UPDATE ON transcription_hints
                   FOR EACH ROW EXECUTE FUNCTION set_th_updated_at();
           END IF;
       END $$""",
    """CREATE TABLE IF NOT EXISTS interaction_deletions (
        deletion_id          SERIAL PRIMARY KEY,
        interaction_id_was   INTEGER     NOT NULL,
        deleted_by_user_id   INTEGER     REFERENCES users (user_id) ON DELETE SET NULL,
        deleted_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        company_id           INTEGER     NOT NULL,
        project_id           INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_interaction_deletions_deleted_at "
    "ON interaction_deletions (deleted_at)",
    "CREATE INDEX IF NOT EXISTS idx_interaction_deletions_company_id "
    "ON interaction_deletions (company_id)",

    # location_notes — free-form post-it notes per location (Grade page).
    # Tenant scope via location_notes.location_id → locations.company_id;
    # no denormalized company_id column.
    """CREATE TABLE IF NOT EXISTS location_notes (
        location_note_id   SERIAL PRIMARY KEY,
        location_id        INTEGER NOT NULL
                               REFERENCES locations (location_id) ON DELETE CASCADE,
        ln_author_user_id  INTEGER REFERENCES users (user_id) ON DELETE SET NULL,
        ln_text            TEXT NOT NULL,
        ln_deleted_at      TIMESTAMPTZ,
        ln_created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ln_updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT chk_location_notes_text_length
            CHECK (char_length(ln_text) BETWEEN 1 AND 500),
        CONSTRAINT chk_location_notes_text_not_blank
            CHECK (btrim(ln_text) <> '')
    )""",
    "CREATE INDEX IF NOT EXISTS idx_location_notes_location_id "
    "ON location_notes (location_id) WHERE ln_deleted_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_location_notes_author_user_id "
    "ON location_notes (ln_author_user_id) WHERE ln_deleted_at IS NULL",
    """CREATE OR REPLACE FUNCTION set_ln_updated_at() RETURNS TRIGGER AS $$
       BEGIN NEW.ln_updated_at = NOW(); RETURN NEW; END;
       $$ LANGUAGE plpgsql""",
    """DO $$ BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_location_notes_updated_at') THEN
               CREATE TRIGGER trg_location_notes_updated_at BEFORE UPDATE ON location_notes
                   FOR EACH ROW EXECUTE FUNCTION set_ln_updated_at();
           END IF;
       END $$""",

    # Clarifying-questions removal: any interaction left at status 41
    # (awaiting_clarification) when the wizard was retired needs to move
    # forward so it doesn't sit in a non-existent flow. We send them to 45
    # (submitted) — visible in the UI for manual retry without losing data.
    "UPDATE interactions SET status_id = 45 WHERE status_id = 41",
]


def _apply_additive_migrations(conn):
    """Run each ALTER TABLE ADD COLUMN IF NOT EXISTS. PG-only syntax; skipped on SQLite."""
    if not IS_POSTGRES:
        return
    for sql in _ADDITIVE_MIGRATIONS:
        conn.execute(sql)
    conn.commit()


# ── Seeded reference/lookup data ────────────────────────────────


_STATUS_SEEDS = [
    # (status_id, status_name, status_description, status_category)
    (1,  'active',                 'Record is active',               'general'),
    (2,  'inactive',               'Record is inactive',             'general'),
    (10, 'suspended',              'Company account suspended',      'company'),
    (11, 'churned',                'Company has churned',            'company'),
    (20, 'pending',                'User not yet activated',         'user'),
    (30, 'completed',              'Project completed',              'project'),
    (31, 'archived',               'Project archived',               'project'),
    (40, 'transcribing',           'Audio being transcribed',        'interaction'),
    # status 41 kept for historical interactions; not produced by current code.
    (41, 'awaiting_clarification', 'Waiting for clarifying answers', 'interaction'),
    (42, 'grading',                'AI grading in progress',         'interaction'),
    (43, 'graded',                 'Interaction fully graded',       'interaction'),
    (44, 'no_answer',              'Call with no answer',            'interaction'),
    (45, 'pending',                'Submitted, not yet processing',  'interaction'),
    (50, 'revoked',                'API key revoked',                'api_key'),
]

_ACTION_TYPE_SEEDS = [
    # (audit_log_action_type_id, audit_log_action_type_name)
    (1, 'created'),
    (2, 'updated'),
    (3, 'deleted'),
    (4, 'graded'),
    (5, 'regraded'),
    (6, 'submitted'),
    (7, 'unposted'),
    (8, 'exported'),
]

_TARGET_ENTITY_TYPE_SEEDS = [
    # (audit_log_target_entity_type_id, audit_log_target_entity_type_name)
    (1, 'user'),
    (2, 'interaction'),
    (3, 'project'),
    (4, 'phone_routing'),
    (5, 'company'),
    (6, 'rubric_group'),
    (7, 'rubric_item'),
    (8, 'department'),
    (9, 'location'),
    (10, 'transcription_hint'),
    (11, 'campaign'),
]

_ROLE_SEEDS = [
    # (role_id, role_name, role_scope)
    (1, 'super_admin', 'platform'),
    (2, 'admin',       'company'),
    (3, 'manager',     'company'),
    (4, 'caller',      'company'),
]


_INDUSTRY_SEEDS = [
    # (industry_id, industry_name)
    (1,  'Property Management'),
    (2,  'Technology'),
    (3,  'Healthcare'),
    (4,  'Financial Services'),
    (5,  'Retail'),
    (6,  'Hospitality'),
    (7,  'Education'),
    (8,  'Legal'),
    (9,  'Manufacturing'),
    (10, 'Other'),
]


def seed_defaults():
    """Seed lookup tables in dependency order. Idempotent — skips existing rows.

    Order is important because FKs default to values that must exist first:
        1. statuses                       (referenced by many tables' status_id defaults)
        2. audit_log_action_types         (referenced by audit_log)
        3. audit_log_target_entity_types  (referenced by audit_log)
        4. roles                          (referenced by user_roles)
    """
    conn = get_conn()
    _ph = "%s" if IS_POSTGRES else "?"
    try:
        # 1. statuses
        for (sid, sname, sdesc, scat) in _STATUS_SEEDS:
            conn.execute(
                f"""INSERT INTO statuses (status_id, status_name, status_description, status_category)
                    SELECT {_ph}, {_ph}, {_ph}, {_ph}
                    WHERE NOT EXISTS (SELECT 1 FROM statuses WHERE status_id = {_ph})""",
                (sid, sname, sdesc, scat, sid),
            )

        # 2. audit_log_action_types
        for (aid, aname) in _ACTION_TYPE_SEEDS:
            conn.execute(
                f"""INSERT INTO audit_log_action_types
                        (audit_log_action_type_id, audit_log_action_type_name)
                    SELECT {_ph}, {_ph}
                    WHERE NOT EXISTS
                        (SELECT 1 FROM audit_log_action_types
                         WHERE audit_log_action_type_id = {_ph})""",
                (aid, aname, aid),
            )

        # 3. audit_log_target_entity_types
        for (tid, tname) in _TARGET_ENTITY_TYPE_SEEDS:
            conn.execute(
                f"""INSERT INTO audit_log_target_entity_types
                        (audit_log_target_entity_type_id, audit_log_target_entity_type_name)
                    SELECT {_ph}, {_ph}
                    WHERE NOT EXISTS
                        (SELECT 1 FROM audit_log_target_entity_types
                         WHERE audit_log_target_entity_type_id = {_ph})""",
                (tid, tname, tid),
            )

        # 4. roles
        for (rid, rname, rscope) in _ROLE_SEEDS:
            conn.execute(
                f"""INSERT INTO roles (role_id, role_name, role_scope)
                    SELECT {_ph}, {_ph}, {_ph}
                    WHERE NOT EXISTS (SELECT 1 FROM roles WHERE role_id = {_ph})""",
                (rid, rname, rscope, rid),
            )

        # 5. industries
        for (iid, iname) in _INDUSTRY_SEEDS:
            conn.execute(
                f"""INSERT INTO industries (industry_id, industry_name)
                    SELECT {_ph}, {_ph}
                    WHERE NOT EXISTS (SELECT 1 FROM industries WHERE industry_id = {_ph})""",
                (iid, iname, iid),
            )

        conn.commit()
        logger.info("seed_defaults completed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Per-company defaults (Phase 6) ──────────────────────────────


# Whitelist of allowed company-setting keys. Routes that write settings MUST
# reject any key not in this list (401 bad request). New settings land here.
COMPANY_SETTING_KEYS = (
    "location_label",
    "location_list_label",
    "caller_label",
    "respondent_label",
    "phone_routing_label",
    "project_label",
    "show_transcript",
)


_COMPANY_SETTING_DEFAULTS = {
    "location_label":      "Location",
    "location_list_label": "Locations",
    "caller_label":        "Caller",
    "respondent_label":    "Respondent",
    "phone_routing_label": "Phone Routing",
    "project_label":       "Project",
    "show_transcript":     "true",
}


def seed_company_defaults(company_id, conn=None):
    """Insert the default company_settings rows for a newly created company.

    Idempotent via the uq_company_settings_key UNIQUE constraint — re-seeding
    an existing company is a no-op. If `conn` is passed in the caller's
    transaction is re-used so the seed rolls back together with the company
    row on failure.
    """
    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        for key, value in _COMPANY_SETTING_DEFAULTS.items():
            if IS_POSTGRES:
                conn.execute(
                    """INSERT INTO company_settings
                           (company_id, company_setting_key, company_setting_value)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (company_id, company_setting_key) DO NOTHING""",
                    (company_id, key, value),
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO company_settings
                           (company_id, company_setting_key, company_setting_value)
                       VALUES (?, ?, ?)""",
                    (company_id, key, value),
                )
        if own_conn:
            conn.commit()
    except Exception:
        if own_conn:
            conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


# ── Flask integration helper ────────────────────────────────────


def init_app(app):
    """Wire setup_db() + seed_defaults() into a Flask app at startup."""
    with app.app_context():
        setup_db()
        seed_defaults()
        logger.info("Echo Audit V2 database ready (postgres=%s)", IS_POSTGRES)
