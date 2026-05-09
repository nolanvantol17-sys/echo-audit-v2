"""
sso_routes.py — Microsoft SSO scaffold (Azure AD / Entra ID, OIDC via MSAL).

Status: SCAFFOLD. The full flow is not yet wired into the login UI and JIT
user provisioning is not yet implemented. The routes below establish the
shape so the rest of the work (tenant routing by email domain, role mapping,
audit logging) can be added without re-architecting.

Activation:
  Set the following env vars (typically on Railway → echo-audit-app → Variables):
      AZURE_AD_TENANT_ID     — the Microsoft tenant the app is registered in
      AZURE_AD_CLIENT_ID     — the Azure AD app registration's client (application) id
      AZURE_AD_CLIENT_SECRET — a secret generated for the app registration
      AZURE_AD_REDIRECT_URI  — must EXACTLY match the redirect URI registered
                               in Azure AD, e.g. https://app.echoaudit.com/auth/sso/microsoft/callback

  The blueprint serves 503 on every route while any of those are missing.

Azure AD app registration steps for Mayfair IT (one-time):
  1. portal.azure.com → Microsoft Entra ID → App registrations → New registration
  2. Name: "Echo Audit"
  3. Supported account types: "Accounts in this organizational directory only"
                              (single-tenant — Mayfair employees only)
  4. Redirect URI (Web): https://<your-echo-audit-host>/auth/sso/microsoft/callback
  5. After creation, copy: Directory (tenant) ID + Application (client) ID
  6. Certificates & secrets → New client secret → copy the Value (NOT the Secret ID)
  7. API permissions → add "openid", "profile", "email", "User.Read" (Microsoft Graph,
     Delegated). Click "Grant admin consent for Mayfair Management."
  8. Send the three values to the Echo Audit ops contact for env var setup.

Echo Audit-side TODO before this is production-ready:
  - Tenant routing: parse email domain from the SSO id_token, look up the
    matching `companies` row. Requires a `companies.company_email_domain`
    column (deferred until Carlos confirms multi-tenant SSO scope).
  - JIT provisioning: create a `users` row on first login with caller role
    by default. Reject if the email domain is unknown.
  - Role mapping: optional — read Azure AD app roles or group claims and
    map to Echo Audit role hierarchy (caller / manager / admin).
  - Wire a "Sign in with Microsoft" button on /login that POSTs to /start.
  - Audit log entry on every successful + failed SSO attempt (use audit_log
    blueprint's existing helper).
"""

import logging
import os
import secrets
from urllib.parse import urlencode

from flask import Blueprint, jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)

sso_bp = Blueprint("sso", __name__, url_prefix="/auth/sso")


_REQUIRED_ENV = (
    "AZURE_AD_TENANT_ID",
    "AZURE_AD_CLIENT_ID",
    "AZURE_AD_CLIENT_SECRET",
    "AZURE_AD_REDIRECT_URI",
)


def _config():
    """Return Azure AD config dict if all env vars are set, else None.

    Centralized so every route gets the same gating + error message.
    """
    cfg = {k: os.getenv(k, "").strip() for k in _REQUIRED_ENV}
    if not all(cfg.values()):
        return None
    return cfg


def _not_configured_response():
    return jsonify({
        "error": "Microsoft SSO is not configured on this instance.",
        "missing_env_vars": [k for k in _REQUIRED_ENV if not os.getenv(k, "").strip()],
        "next_steps": (
            "An admin must set the AZURE_AD_* env vars and complete the Azure AD "
            "app registration. See sso_routes.py module docstring."
        ),
    }), 503


def _msal_app(cfg):
    """Build (or return cached) MSAL ConfidentialClientApplication.

    Imported lazily so the entire app doesn't error at import time if msal
    isn't installed in some dev env.
    """
    import msal  # type: ignore
    authority = f"https://login.microsoftonline.com/{cfg['AZURE_AD_TENANT_ID']}"
    return msal.ConfidentialClientApplication(
        client_id=cfg["AZURE_AD_CLIENT_ID"],
        client_credential=cfg["AZURE_AD_CLIENT_SECRET"],
        authority=authority,
    )


# ── Routes ──────────────────────────────────────────────────────


@sso_bp.route("/microsoft/start", methods=["GET"])
def microsoft_start():
    """Kick off the OIDC auth-code flow. Generates a state token, stashes
    it in the session, and redirects the browser to Microsoft."""
    cfg = _config()
    if not cfg:
        return _not_configured_response()

    state = secrets.token_urlsafe(24)
    session["sso_state"] = state
    # Optional: capture the post-login destination so we can route the user
    # back where they intended to go. Defaults to /app.
    next_url = request.args.get("next", "/app")
    session["sso_next"] = next_url

    app = _msal_app(cfg)
    auth_url = app.get_authorization_request_url(
        scopes=["openid", "profile", "email", "User.Read"],
        state=state,
        redirect_uri=cfg["AZURE_AD_REDIRECT_URI"],
    )
    return redirect(auth_url)


@sso_bp.route("/microsoft/callback", methods=["GET"])
def microsoft_callback():
    """Handle Microsoft's redirect back. Validates state, exchanges the auth
    code for tokens, and (TODO) JIT-provisions + logs in the user.

    Returns 501 today on the user-creation step — the surrounding flow
    (state validation + token exchange) IS exercised so we'll know if Azure
    AD config breaks before the JIT logic ships.
    """
    cfg = _config()
    if not cfg:
        return _not_configured_response()

    # Microsoft includes ?error= when consent fails or the user cancels.
    err = request.args.get("error")
    if err:
        logger.warning("[sso] Microsoft returned error=%s description=%s",
                       err, request.args.get("error_description", ""))
        return jsonify({"error": "Microsoft SSO failed", "details": err}), 400

    expected_state = session.pop("sso_state", None)
    received_state = request.args.get("state")
    if not expected_state or expected_state != received_state:
        logger.warning("[sso] state mismatch — possible CSRF or stale session")
        return jsonify({"error": "Invalid SSO state"}), 400

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing authorization code"}), 400

    app = _msal_app(cfg)
    result = app.acquire_token_by_authorization_code(
        code=code,
        scopes=["openid", "profile", "email", "User.Read"],
        redirect_uri=cfg["AZURE_AD_REDIRECT_URI"],
    )
    if "error" in result:
        logger.warning("[sso] token exchange failed: %s", result.get("error"))
        return jsonify({
            "error": "Token exchange failed",
            "details": result.get("error_description", result.get("error")),
        }), 400

    id_claims = result.get("id_token_claims") or {}
    email = (id_claims.get("preferred_username") or id_claims.get("email") or "").lower()
    name  = id_claims.get("name") or ""
    if not email:
        return jsonify({"error": "Microsoft did not return an email claim"}), 400

    # ── TODO (waiting on user direction) ─────────────────────
    # 1. Look up users.user_email — if exists, login_user(existing).
    # 2. Else: parse email domain → look up companies.company_email_domain,
    #    JIT-create the user with caller role, login_user(new).
    # 3. Else: reject with "Your email domain is not registered with any
    #    Echo Audit organization."
    # 4. Audit log success / failure via audit_log blueprint.
    # 5. redirect(session.pop("sso_next", "/app"))
    return jsonify({
        "ok": False,
        "scaffold_only": True,
        "message": (
            "Microsoft SSO authenticated successfully but JIT user "
            "provisioning is not yet implemented. See sso_routes.py TODO."
        ),
        "claims": {
            "email": email,
            "name":  name,
        },
    }), 501
