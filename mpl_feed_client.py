"""
mpl_feed_client.py — read-only client for Mayfair's MPL bulk feed
("Exile Island"): the proper property + user directory that replaces the
old fuzzy /api/properties/managers name-search.

Deliberately SEPARATE from mayfairnet_client.py: this is a different
server and a different key. Credentials come from MPL_API_BASE /
MPL_API_KEY (X-Api-Key header) so they never land in source or logs.

Endpoints (validated 2026-05-19, read-only, plain JSON arrays, NO
pagination — single full dump each):

  GET /api/properties/property-directory  → all properties. Keys:
      PropertyId(int), YardiCode(str, unique join key), ShortName,
      LongName, FullAddress, PhoneNumber, and per-role stable-user-ID
      CSV lists: AllAssignedUserIds, PropertyManagerUserIds,
      RegionalMaintenanceUserIds, ComplianceUserIds, OnsiteUserIds.

  GET /api/properties/active-users        → all users. Keys:
      ID(int, unique join key), FirstName, LastName, Email, TeamsPhone.

This module is intentionally narrow: fetch + shape-validate, no DB, no
business logic. A bad pull must never reach the sync — every failure
mode (unreachable, non-200, non-JSON, not-a-list, empty) raises
MPLFeedError so the caller aborts and leaves the live tables untouched.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30  # full-dump pulls; generous vs. mayfairnet's 10

_PROPERTY_DIRECTORY_PATH = "/api/properties/property-directory"
_ACTIVE_USERS_PATH = "/api/properties/active-users"


class MPLFeedError(Exception):
    """Raised on any condition that makes the pull unsafe to sync from:
    transport failure, non-2xx, non-JSON, wrong shape, or empty payload.
    The sync treats this as 'abort, touch nothing'."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _base_url() -> str:
    base = os.getenv("MPL_API_BASE", "").strip()
    if not base:
        raise MPLFeedError(
            "MPL_API_BASE env var not set — the MPL feed is unreachable. "
            "It lives in the local .env today; add MPL_API_BASE / "
            "MPL_API_KEY to Railway before the recurring job can run."
        )
    return base.rstrip("/")


def _api_key() -> str:
    key = os.getenv("MPL_API_KEY", "").strip()
    if not key:
        raise MPLFeedError(
            "MPL_API_KEY env var not set — the MPL feed is unreachable. "
            "It lives in the local .env today; add MPL_API_BASE / "
            "MPL_API_KEY to Railway before the recurring job can run."
        )
    return key


def _get_list(path: str, what: str) -> list[dict]:
    """GET a feed endpoint and return its JSON array.

    Hard-fails (raises MPLFeedError) on transport error, non-2xx,
    non-JSON, a non-list body, or an empty list. An empty feed is
    treated as a failure on purpose: a daily full dump that comes back
    empty is almost certainly an upstream outage, and syncing from it
    would inactivate every property/user. Never sync a bad pull.
    """
    url = f"{_base_url()}{path}"
    try:
        resp = requests.get(
            url,
            headers={"Accept": "application/json", "X-Api-Key": _api_key()},
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("[mpl_feed] transport error path=%s err=%s", path, exc)
        raise MPLFeedError(f"MPL feed request failed for {what}: {exc}") from exc

    if not resp.ok:
        # Never log the body — directory data is sensitive. Status is enough.
        logger.warning(
            "[mpl_feed] non-2xx path=%s status=%s", path, resp.status_code,
        )
        raise MPLFeedError(
            f"MPL feed returned HTTP {resp.status_code} for {what}",
            status_code=resp.status_code,
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise MPLFeedError(
            f"MPL feed returned non-JSON body for {what}: {exc}"
        ) from exc

    if not isinstance(body, list):
        raise MPLFeedError(
            f"MPL feed {what} expected a JSON array, got "
            f"{type(body).__name__}"
        )
    if not body:
        raise MPLFeedError(
            f"MPL feed {what} returned an empty list — treating as an "
            f"upstream outage and aborting (a bad pull must never wipe "
            f"the live tables)."
        )
    return body


def fetch_property_directory() -> list[dict]:
    """All properties from the MPL feed. Raises MPLFeedError if the pull
    is unsafe to sync from (see _get_list)."""
    return _get_list(_PROPERTY_DIRECTORY_PATH, "property-directory")


def fetch_active_users() -> list[dict]:
    """All users from the MPL feed. Raises MPLFeedError if the pull is
    unsafe to sync from (see _get_list)."""
    return _get_list(_ACTIVE_USERS_PATH, "active-users")
