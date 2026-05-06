"""
mayfairnet_client.py — thin client for Mayfairnet's Zapier-style REST API.

Wraps GET /api/zapier/* endpoints exposed by the PropertyMS app. Auth via
X-Api-Key header; key sourced from MAYFAIRNET_API_KEY env var so it never
lands in source control or logs.

Reachable endpoints (as of 2026-05-06):
  GET /api/zapier/health                          — no auth, liveness probe
  GET /api/zapier/rmdirectory?propertyName=<str>  — RM/CM directory lookup

This module is intentionally narrow. Use cases (matching properties to Echo
Audit locations, surfacing RM/CM contact info on a grade page, etc.) build on
top of it; the client itself stays free of business logic.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://mayfairmgt.net"
DEFAULT_TIMEOUT_SECONDS = 10


class MayfairnetError(Exception):
    """Raised on any non-2xx response or transport-level failure."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def _base_url() -> str:
    return os.getenv("MAYFAIRNET_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    key = os.getenv("MAYFAIRNET_API_KEY", "").strip()
    if not key:
        raise MayfairnetError(
            "MAYFAIRNET_API_KEY env var not set — Mayfairnet API requests "
            "will fail. Add it in Railway → echo-audit-app → Variables."
        )
    return key


def _request(path: str, params: Optional[dict] = None,
             require_auth: bool = True) -> object:
    """Execute a GET against Mayfairnet. Returns parsed JSON.

    Raises MayfairnetError on transport failure, non-2xx status, or
    unparseable JSON. Auth header is included when require_auth=True.
    """
    url = f"{_base_url()}{path}"
    headers = {"Accept": "application/json"}
    if require_auth:
        headers["X-Api-Key"] = _api_key()

    try:
        resp = requests.get(
            url, headers=headers, params=params,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("[mayfairnet] transport error path=%s err=%s", path, exc)
        raise MayfairnetError(f"Mayfairnet request failed: {exc}") from exc

    if not resp.ok:
        # Don't log response body — could carry sensitive directory data on
        # auth-required endpoints. Status + path is enough for diagnosis.
        logger.warning(
            "[mayfairnet] non-2xx path=%s status=%s", path, resp.status_code,
        )
        raise MayfairnetError(
            f"Mayfairnet returned HTTP {resp.status_code} for {path}",
            status_code=resp.status_code,
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise MayfairnetError(
            f"Mayfairnet returned non-JSON body for {path}: {exc}"
        ) from exc


def health() -> dict:
    """Hit the no-auth liveness endpoint. Returns the raw {status, timestamp}
    dict. Useful for connectivity smoke checks before issuing real lookups."""
    return _request("/api/zapier/health", require_auth=False)


def get_rm_directory(property_name: str, clear_cache: bool = False) -> list[dict]:
    """Look up RM + CM contact info for the best-matched property.

    Returns a list of dicts (Mayfairnet's API returns an array even for a
    single-property match). Each entry has PropertyName / RMName / RMEmail /
    RMPhoneNumber1 / CMName / CMEmail / CMPhoneNumber1.

    Pass clear_cache=True to force the upstream cache to refresh; default is
    False per the Postman collection.
    """
    params = {"propertyName": property_name}
    if clear_cache:
        params["clearCache"] = "true"
    result = _request("/api/zapier/rmdirectory", params=params)
    if not isinstance(result, list):
        raise MayfairnetError(
            f"Expected list from /rmdirectory, got {type(result).__name__}"
        )
    return result
