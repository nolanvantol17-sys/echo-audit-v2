"""
audit_log.py — write_audit_log() helper used across Phases 2/3/4.

Write-only; the read route lives in audit_log_routes.py. Keeping the writer
in its own module avoids a circular import between api_routes / interactions
/ rubrics / dashboard modules that all need to log, and a future audit read
blueprint.

Action type IDs and target entity type IDs match the seed in db.py:

    ACTIONS:   1=created, 2=updated, 3=deleted, 4=graded, 5=regraded,
               6=submitted, 7=unposted
    TARGETS:   1=user, 2=interaction, 3=project, 4=campaign, 5=company,
               6=rubric_group, 7=rubric_item, 8=department, 9=location,
               10=transcription_hint
"""

import json
import logging

from db import IS_POSTGRES, get_conn, q

logger = logging.getLogger(__name__)

# Action type IDs
ACTION_CREATED    = 1
ACTION_UPDATED    = 2
ACTION_DELETED    = 3
ACTION_GRADED     = 4
ACTION_REGRADED   = 5
ACTION_SUBMITTED  = 6
ACTION_UNPOSTED   = 7

# Target entity type IDs
ENTITY_USER         = 1
ENTITY_INTERACTION  = 2
ENTITY_PROJECT      = 3
ENTITY_CAMPAIGN     = 4
ENTITY_COMPANY      = 5
ENTITY_RUBRIC_GROUP = 6
ENTITY_RUBRIC_ITEM  = 7
ENTITY_DEPARTMENT   = 8
ENTITY_LOCATION     = 9
ENTITY_TRANSCRIPTION_HINT = 10


def write_audit_log(actor_user_id, action_type_id, target_entity_type_id=None,
                    target_entity_id=None, metadata=None, conn=None):
    """Append a row to audit_log. Never raises — failure to audit must not
    break the user-facing request.

    Parameters
    ----------
    actor_user_id : int or None
        The user taking the action. None is allowed (system actions).
    action_type_id : int
        Required. See ACTION_* constants.
    target_entity_type_id : int or None
        See ENTITY_* constants.
    target_entity_id : str, int, or None
        PK of the affected row. Stored as TEXT so any table's PK fits.
    metadata : dict or None
        Arbitrary before/after payload. Serialized to JSONB.
    conn : optional existing connection
        Pass an in-flight connection to piggyback on the caller's transaction
        so the audit row rolls back with the mutation on error. If omitted,
        a fresh connection is opened and committed independently.
    """
    try:
        metadata_json = json.dumps(metadata) if metadata is not None else None
        target_id_str = str(target_entity_id) if target_entity_id is not None else None

        own_conn = conn is None
        if own_conn:
            conn = get_conn()

        try:
            if IS_POSTGRES:
                conn.execute(
                    """INSERT INTO audit_log (
                           actor_user_id, audit_log_action_type_id,
                           audit_log_target_entity_type_id, al_target_entity_id,
                           al_metadata
                       ) VALUES (%s, %s, %s, %s, %s::jsonb)""",
                    (actor_user_id, action_type_id, target_entity_type_id,
                     target_id_str, metadata_json),
                )
            else:
                conn.execute(
                    q("""INSERT INTO audit_log (
                             actor_user_id, audit_log_action_type_id,
                             audit_log_target_entity_type_id, al_target_entity_id,
                             al_metadata
                         ) VALUES (?, ?, ?, ?, ?)"""),
                    (actor_user_id, action_type_id, target_entity_type_id,
                     target_id_str, metadata_json),
                )
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
                conn.close()
    except Exception:
        # Audit failure must never propagate.
        logger.exception("write_audit_log failed (actor=%s action=%s entity=%s id=%s)",
                         actor_user_id, action_type_id, target_entity_type_id,
                         target_entity_id)
