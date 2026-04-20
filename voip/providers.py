"""
voip/providers.py — Provider abstraction layer.

Each provider class knows how to:
    - verify_signature(payload_bytes, headers, secret) — authenticate the webhook
    - parse_webhook(payload_dict, headers) -> VoIPCallEvent | None — normalize

Webhook formats for each provider were built from publicly available
documentation snapshots. The exact header names, body shapes, and signature
algorithms MUST be validated against the current live provider documentation
before going to production — see the assumption notes below and at the top
of each provider class.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ── Normalized event shape ──────────────────────────────────────


@dataclass
class VoIPCallEvent:
    """Provider-agnostic, normalized representation of a completed call.

    Only what Echo Audit needs downstream: the queue row + the processor.
    Raw provider payload is preserved in `raw_payload` for debugging and
    auditing.
    """
    provider:         str
    call_id:          str
    recording_url:    Optional[str]
    caller_number:    Optional[str]
    called_number:    Optional[str]
    call_date:        date
    duration_seconds: Optional[int]
    raw_payload:      dict = field(default_factory=dict)


# ── Helpers ────────────────────────────────────────────────────


def _safe_get(d: dict, *path, default=None):
    """Walk a dotted path safely through a dict of dicts/lists."""
    cur: Any = d
    for key in path:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                cur = cur[key]
            except (IndexError, TypeError):
                return default
        else:
            return default
    return cur if cur is not None else default


def _parse_date(value, default=None) -> date:
    """Accept ISO date/datetime strings or datetime objects. Returns a date."""
    if not value:
        return default or date.today()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value)
    # Common ISO-8601 shapes: "2026-04-14", "2026-04-14T18:22:00Z", "2026-04-14T18:22:00+00:00"
    for trial in (s, s[:10]):
        try:
            return datetime.fromisoformat(trial.replace("Z", "+00:00")).date()
        except (ValueError, AttributeError):
            pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return default or date.today()


def _parse_int(value, default=None) -> Optional[int]:
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _constant_time_equal(a: str, b: str) -> bool:
    """Timing-attack-resistant string comparison."""
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _hmac_sha256_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# ── Base class ────────────────────────────────────────────────


class VoIPProvider:
    """Base class. Subclasses MUST override both methods."""

    name: str = ""

    def verify_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        raise NotImplementedError

    def parse_webhook(self, payload: dict, headers: dict) -> Optional[VoIPCallEvent]:
        raise NotImplementedError

    # Header lookup helper — providers send headers in mixed case. Normalize.
    @staticmethod
    def _header(headers: dict, *candidates, default=None):
        if not headers:
            return default
        lowered = {k.lower(): v for k, v in headers.items()}
        for c in candidates:
            val = lowered.get(c.lower())
            if val is not None:
                return val
        return default


# ── Provider: RingCentral ──────────────────────────────────────
#
# ASSUMPTION NOTES (verify against live RingCentral docs before prod):
# - RingCentral webhooks follow a subscription-lifecycle handshake: the first
#   POST carries a "Validation-Token" header that must be echoed back on the
#   200 response. This provider does NOT return the echo; the webhook route
#   layer handles echoing when it sees the header.
# - Completed-call events arrive under body.event = "/restapi/.../telephony/sessions"
#   with body.body containing the call details. Recording URL shape is
#   /restapi/v1.0/account/.../recording/<id>/content.
# - Authentication of the webhook body in RingCentral is typically implicit
#   (HTTPS + subscription token). We treat the validation token as our
#   shared-secret-ish signal: if the stored webhook_validation_token matches
#   what the client pastes into their RC dashboard, we accept.


class RingCentralProvider(VoIPProvider):
    name = "ringcentral"

    def verify_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        # RingCentral's validation-token mechanism: either the first-time
        # handshake (echo the Validation-Token header) or a configured
        # Verification-Token header that matches the stored secret.
        token = self._header(headers, "Verification-Token", "X-Verification-Token",
                             "Validation-Token")
        if not token or not secret:
            return False
        return _constant_time_equal(token, secret)

    def parse_webhook(self, payload: dict, headers: dict) -> Optional[VoIPCallEvent]:
        if not payload:
            return None

        body = payload.get("body") or payload  # body may be nested or flat

        # Telephony sessions API: party list inside body
        parties = _safe_get(body, "parties") or []
        recordings = _safe_get(body, "recordings") or []
        if not recordings:
            # No recording means nothing to grade — skip gracefully.
            return None

        recording = recordings[0]
        recording_url = recording.get("contentUri") or recording.get("uri")
        call_id = (
            _safe_get(body, "sessionId")
            or _safe_get(body, "telephonySessionId")
            or recording.get("id")
            or payload.get("uuid")
        )
        if not call_id:
            return None

        from_party = next((p for p in parties if p.get("direction") == "Inbound"), None) \
            or (parties[0] if parties else {})
        to_party = next((p for p in parties if p.get("direction") == "Outbound"), None) \
            or (parties[-1] if parties else {})

        caller_number = _safe_get(from_party, "from", "phoneNumber")
        called_number = _safe_get(to_party, "to", "phoneNumber")

        call_date = _parse_date(
            _safe_get(body, "startTime")
            or _safe_get(body, "creationTime")
        )
        duration = _parse_int(_safe_get(body, "duration"))

        return VoIPCallEvent(
            provider="ringcentral",
            call_id=str(call_id),
            recording_url=recording_url,
            caller_number=caller_number,
            called_number=called_number,
            call_date=call_date,
            duration_seconds=duration,
            raw_payload=payload,
        )


# ── Provider: Dialpad ─────────────────────────────────────────
#
# ASSUMPTIONS:
# - Dialpad sends an `X-Dialpad-Signature` header containing the HMAC-SHA256
#   of the raw request body using the webhook secret. Verify this against
#   the current Dialpad webhook doc — newer Dialpad accounts may use JWT
#   instead, which would require token decoding instead of HMAC.
# - Completed-call payload shape is flat with `call_id`, `recording_url`
#   (sometimes `recording_details[0].url`), `from_number`, `to_number`.


class DialpadProvider(VoIPProvider):
    name = "dialpad"

    def verify_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        provided = self._header(headers, "X-Dialpad-Signature", "Dialpad-Signature")
        if not provided or not secret:
            return False
        expected = _hmac_sha256_hex(secret, payload)
        return _constant_time_equal(expected, provided.strip())

    def parse_webhook(self, payload: dict, headers: dict) -> Optional[VoIPCallEvent]:
        if not payload:
            return None

        # Dialpad "call_event" hook has a flat shape
        call_id = payload.get("call_id") or payload.get("id")
        if not call_id:
            return None

        recording_url = (
            payload.get("recording_url")
            or _safe_get(payload, "recording_details", 0, "url")
            or _safe_get(payload, "recording", "url")
        )
        caller_number = payload.get("from_number") or _safe_get(payload, "contact", "phone")
        called_number = payload.get("to_number")
        call_date = _parse_date(
            payload.get("date_started")
            or payload.get("date_ended")
            or payload.get("call_date")
        )
        duration = _parse_int(payload.get("duration") or payload.get("duration_seconds"))

        return VoIPCallEvent(
            provider="dialpad",
            call_id=str(call_id),
            recording_url=recording_url,
            caller_number=caller_number,
            called_number=called_number,
            call_date=call_date,
            duration_seconds=duration,
            raw_payload=payload,
        )


# ── Provider: Aircall ─────────────────────────────────────────
#
# ASSUMPTIONS:
# - Aircall webhooks sign the body with HMAC-SHA256 using the webhook token
#   from the client's Aircall dashboard and place the result in the
#   `X-Aircall-Signature` header. Signature is sometimes prefixed with
#   "sha256=" — this provider tolerates both.
# - Payload shape: {"event": "call.ended", "data": {...}} with `data.id`,
#   `data.recording` (URL string), `data.direction`, `data.raw_digits`,
#   `data.number.digits` (the called DID).


class AircallProvider(VoIPProvider):
    name = "aircall"

    def verify_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        provided = self._header(headers, "X-Aircall-Signature", "Aircall-Signature")
        if not provided or not secret:
            return False
        provided = provided.strip()
        if provided.startswith("sha256="):
            provided = provided[len("sha256="):]
        expected = _hmac_sha256_hex(secret, payload)
        return _constant_time_equal(expected, provided)

    def parse_webhook(self, payload: dict, headers: dict) -> Optional[VoIPCallEvent]:
        if not payload:
            return None
        data = payload.get("data") or payload
        call_id = data.get("id") or data.get("call_id")
        if not call_id:
            return None

        recording_url = data.get("recording") or data.get("recording_url")
        # direction: 'inbound' / 'outbound'. `raw_digits` is the counterparty;
        # `number.digits` is the Aircall-side DID.
        direction = data.get("direction", "").lower()
        counterparty = data.get("raw_digits")
        aircall_number = _safe_get(data, "number", "digits")

        if direction == "inbound":
            caller_number, called_number = counterparty, aircall_number
        else:
            caller_number, called_number = aircall_number, counterparty

        call_date = _parse_date(
            data.get("started_at")
            or data.get("ended_at")
            or data.get("date")
        )
        duration = _parse_int(data.get("duration"))

        return VoIPCallEvent(
            provider="aircall",
            call_id=str(call_id),
            recording_url=recording_url,
            caller_number=caller_number,
            called_number=called_number,
            call_date=call_date,
            duration_seconds=duration,
            raw_payload=payload,
        )


# ── Provider: Zoom Phone ──────────────────────────────────────
#
# ASSUMPTIONS:
# - Zoom requires URL validation via "endpoint.url_validation" events —
#   the webhook layer handles that before signature verification.
# - Per-event signing uses HMAC-SHA256 over "v0:{timestamp}:{body}" with
#   the secret token, header `x-zm-signature: v0=<hex>`, timestamp in
#   header `x-zm-request-timestamp`. A 5-minute freshness window is
#   recommended but not enforced here; add that if replay protection is
#   needed.
# - Recording URL comes from phone.recording_completed events as
#   payload.object.recordings[0].download_url, which is authenticated and
#   requires an OAuth access token built from account/client creds.


class ZoomPhoneProvider(VoIPProvider):
    name = "zoom_phone"

    def verify_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        provided = self._header(headers, "x-zm-signature")
        timestamp = self._header(headers, "x-zm-request-timestamp")
        if not provided or not timestamp or not secret:
            return False
        base = f"v0:{timestamp}:".encode("utf-8") + payload
        expected = "v0=" + hmac.new(secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
        return _constant_time_equal(expected, provided.strip())

    def parse_webhook(self, payload: dict, headers: dict) -> Optional[VoIPCallEvent]:
        if not payload:
            return None
        obj = _safe_get(payload, "payload", "object") or payload.get("object") or payload

        call_id = obj.get("call_id") or obj.get("id")
        if not call_id:
            return None

        recordings = obj.get("recordings") or []
        recording_url = None
        if recordings:
            recording_url = recordings[0].get("download_url") or recordings[0].get("url")

        # Caller/callee: Zoom uses `callee`/`caller` objects. Direction varies.
        caller_number = (
            _safe_get(obj, "caller", "phone_number")
            or obj.get("caller_number")
            or obj.get("from")
        )
        called_number = (
            _safe_get(obj, "callee", "phone_number")
            or obj.get("callee_number")
            or obj.get("to")
        )
        call_date = _parse_date(obj.get("date_time") or obj.get("start_time"))
        duration = _parse_int(obj.get("duration") or obj.get("duration_in_seconds"))

        return VoIPCallEvent(
            provider="zoom_phone",
            call_id=str(call_id),
            recording_url=recording_url,
            caller_number=caller_number,
            called_number=called_number,
            call_date=call_date,
            duration_seconds=duration,
            raw_payload=payload,
        )


# ── Provider: 8x8 ─────────────────────────────────────────────
#
# ASSUMPTIONS:
# - 8x8's Contact Center Data API posts call-completed events with fields
#   like callId, callerNumber, calledNumber, recordingUrl, and signs the
#   body via HMAC-SHA256 in the `X-8x8-Signature` header.
# - 8x8 tenancy varies heavily — the authoritative doc differs between
#   CPaaS and Contact Center products. Treat this as a generic HMAC
#   implementation and confirm the exact header name + payload shape per
#   customer onboarding.


class EightByEightProvider(VoIPProvider):
    name = "eight_by_eight"

    def verify_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        provided = self._header(headers, "X-8x8-Signature", "X-8X8-Signature",
                                "X-EightByEight-Signature")
        if not provided or not secret:
            return False
        provided = provided.strip()
        if provided.startswith("sha256="):
            provided = provided[len("sha256="):]
        expected = _hmac_sha256_hex(secret, payload)
        return _constant_time_equal(expected, provided)

    def parse_webhook(self, payload: dict, headers: dict) -> Optional[VoIPCallEvent]:
        if not payload:
            return None
        data = payload.get("data") or payload
        call_id = data.get("callId") or data.get("call_id") or data.get("id")
        if not call_id:
            return None

        recording_url = (
            data.get("recordingUrl")
            or data.get("recording_url")
            or _safe_get(data, "recording", "url")
        )
        caller_number = data.get("callerNumber") or data.get("caller_number")
        called_number = data.get("calledNumber") or data.get("called_number")
        call_date = _parse_date(
            data.get("startTime") or data.get("start_time") or data.get("callDate")
        )
        duration = _parse_int(data.get("duration") or data.get("durationSeconds"))

        return VoIPCallEvent(
            provider="eight_by_eight",
            call_id=str(call_id),
            recording_url=recording_url,
            caller_number=caller_number,
            called_number=called_number,
            call_date=call_date,
            duration_seconds=duration,
            raw_payload=payload,
        )


# ── Provider: generic webhook ─────────────────────────────────
#
# Echo Audit's own normalized payload shape, for any provider not listed
# above. Clients Zapier-or-CSV their way into this format. Signature is a
# simple HMAC-SHA256 over the raw body using the stored webhook_secret,
# passed in the `X-Echo-Signature` header.


class GenericWebhookProvider(VoIPProvider):
    name = "generic_webhook"

    def verify_signature(self, payload: bytes, headers: dict, secret: str) -> bool:
        provided = self._header(headers, "X-Echo-Signature", "X-Signature")
        if not provided or not secret:
            return False
        provided = provided.strip()
        if provided.startswith("sha256="):
            provided = provided[len("sha256="):]
        expected = _hmac_sha256_hex(secret, payload)
        return _constant_time_equal(expected, provided)

    def parse_webhook(self, payload: dict, headers: dict) -> Optional[VoIPCallEvent]:
        if not payload:
            return None
        call_id = payload.get("call_id")
        if not call_id:
            return None
        return VoIPCallEvent(
            provider="generic_webhook",
            call_id=str(call_id),
            recording_url=payload.get("recording_url"),
            caller_number=payload.get("caller_number"),
            called_number=payload.get("called_number"),
            call_date=_parse_date(payload.get("call_date")),
            duration_seconds=_parse_int(payload.get("duration_seconds")),
            raw_payload=payload,
        )


# ── Registry ──────────────────────────────────────────────────


PROVIDERS = {
    "ringcentral":     RingCentralProvider(),
    "dialpad":         DialpadProvider(),
    "aircall":         AircallProvider(),
    "zoom_phone":      ZoomPhoneProvider(),
    "eight_by_eight":  EightByEightProvider(),
    "generic_webhook": GenericWebhookProvider(),
}


# Per-provider credential field manifest — consumed by the management
# route GET /api/voip/providers so the UI can render setup forms without
# embedding a second copy of this list on the frontend.
PROVIDER_INFO = [
    {
        "key":  "ringcentral",
        "name": "RingCentral",
        "credentials_fields": ["client_id", "client_secret", "server_url",
                               "webhook_validation_token"],
        "test_supported": False,
        "docs_url": "https://developers.ringcentral.com/api-reference",
    },
    {
        "key":  "dialpad",
        "name": "Dialpad",
        "credentials_fields": ["api_key", "webhook_secret"],
        "test_supported": True,
        "docs_url": "https://developers.dialpad.com/reference/",
    },
    {
        "key":  "aircall",
        "name": "Aircall",
        "credentials_fields": ["api_id", "api_token", "webhook_secret"],
        "test_supported": True,
        "docs_url": "https://developer.aircall.io/api-references/",
    },
    {
        "key":  "zoom_phone",
        "name": "Zoom Phone",
        "credentials_fields": ["account_id", "client_id", "client_secret",
                               "webhook_secret_token"],
        "test_supported": False,
        "docs_url": "https://developers.zoom.us/docs/api/phone/",
    },
    {
        "key":  "eight_by_eight",
        "name": "8x8",
        "credentials_fields": ["api_key", "webhook_secret"],
        "test_supported": False,
        "docs_url": "https://developer.8x8.com/",
    },
    {
        "key":  "generic_webhook",
        "name": "Generic Webhook",
        "credentials_fields": ["webhook_secret"],
        "test_supported": False,
        "docs_url": None,
    },
]


# The subset of credential fields used as the HMAC secret / verification
# token in verify_signature(). Used by the webhook route to pick the
# correct secret without hardcoding provider-specific logic outside of
# this module.
PROVIDER_WEBHOOK_SECRET_FIELD = {
    "ringcentral":     "webhook_validation_token",
    "dialpad":         "webhook_secret",
    "aircall":         "webhook_secret",
    "zoom_phone":      "webhook_secret_token",
    "eight_by_eight":  "webhook_secret",
    "generic_webhook": "webhook_secret",
}
