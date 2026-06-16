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

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import login_user

import auth
from db import get_conn, q
from helpers import safe_next_url

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


def is_microsoft_sso_configured():
    """True iff every AZURE_AD_* env var is populated. Templates use this
    via the global context processor to decide whether to render the
    "Sign in with Microsoft" button on the login page (avoids a dead
    button on instances that haven't been wired yet)."""
    return _config() is not None


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
    # Capture the post-login destination (e.g. an emailed shared dashboard
    # link) so we route the user back there after auth. Validated to a safe
    # same-site path; falls back to /app.
    session["sso_next"] = safe_next_url(request.args.get("next")) or "/app"

    app = _msal_app(cfg)
    auth_url = app.get_authorization_request_url(
        scopes=["User.Read"],
        state=state,
        redirect_uri=cfg["AZURE_AD_REDIRECT_URI"],
    )
    return redirect(auth_url)


def _login_failure(reason_for_user, log_msg=None):
    """Render the standard login template with an error banner.

    Falls through the same path a wrong-password attempt would, so the user
    lands back on a familiar surface instead of a JSON error page. Internal
    detail goes to the log; the user sees only `reason_for_user`.
    """
    if log_msg:
        logger.warning("[sso] login failure: %s", log_msg)
    return render_template("login.html", error=reason_for_user, email=""), 401


@sso_bp.route("/microsoft/callback", methods=["GET"])
def microsoft_callback():
    """Handle Microsoft's redirect back.

    Steps: validate state → exchange code for tokens → look up Echo Audit
    user by email → enforce email-domain matches a registered company →
    log them in → redirect to the original destination (or /app).

    Carlos's invite-only model: we DO NOT create new users here. If the
    Microsoft-authenticated email has no matching Echo Audit user, the
    flow rejects with "ask your admin to invite you" — admins still
    pre-provision via the normal team management UI.
    """
    cfg = _config()
    if not cfg:
        return _not_configured_response()

    # Microsoft includes ?error= when consent fails or the user cancels.
    err = request.args.get("error")
    if err:
        return _login_failure(
            "Microsoft sign-in was cancelled or failed. Please try again.",
            log_msg=f"Microsoft returned error={err} description={request.args.get('error_description', '')}",
        )

    expected_state = session.pop("sso_state", None)
    received_state = request.args.get("state")
    if not expected_state or expected_state != received_state:
        return _login_failure(
            "Sign-in session expired. Please try again.",
            log_msg="state mismatch — possible CSRF or stale session",
        )

    code = request.args.get("code")
    if not code:
        return _login_failure(
            "Microsoft sign-in was incomplete. Please try again.",
            log_msg="missing authorization code in callback",
        )

    msal_app = _msal_app(cfg)
    result = msal_app.acquire_token_by_authorization_code(
        code=code,
        scopes=["User.Read"],
        redirect_uri=cfg["AZURE_AD_REDIRECT_URI"],
    )
    if "error" in result:
        return _login_failure(
            "Couldn't complete Microsoft sign-in. Please try again or use your password.",
            log_msg=f"token exchange failed: {result.get('error')} {result.get('error_description', '')}",
        )

    id_claims = result.get("id_token_claims") or {}
    # Microsoft's preferred_username is the UPN (typically the email). Falls
    # back to email claim if preferred_username is missing.
    email = (id_claims.get("preferred_username") or id_claims.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return _login_failure(
            "Microsoft didn't return a usable email. Please contact your admin.",
            log_msg=f"id_claims missing/malformed email: {id_claims!r}",
        )

    domain = email.split("@", 1)[1]

    conn = get_conn()
    try:
        # Step 1 — confirm the email's domain belongs to a registered Echo
        # Audit company. Defense in depth + future multi-tenant routing.
        cur = conn.execute(
            q("SELECT company_id, company_name FROM companies "
              "WHERE LOWER(company_email_domain) = LOWER(?) LIMIT 1"),
            [domain],
        )
        company_row = cur.fetchone()
        if not company_row:
            return _login_failure(
                f"The email domain @{domain} isn't registered with any Echo Audit organization. "
                f"Contact your admin if you believe this is an error.",
                log_msg=f"unknown email domain: {domain}",
            )
        company_id = company_row["company_id"] if hasattr(company_row, "keys") else company_row[0]

        # Step 2 — find the Echo Audit user by email. Invite-only model: no
        # auto-creation. If the user doesn't exist, point them at their admin.
        user_row = auth._load_user_row(conn, email=email)
        if not user_row:
            return _login_failure(
                "No Echo Audit account found for this email. Ask your admin to invite you first.",
                log_msg=f"no user for email {email} (domain mapped to company {company_id})",
            )

        user = auth.User(user_row)

        # Step 3 — sanity check: the user's company must match the domain's
        # company. Catches a mis-seeded company_email_domain or a user whose
        # email was changed to an unrelated tenant's domain post-creation.
        # Super admins are exempt — they're cross-org by design and have no
        # fixed company_id (their role grants access to every tenant). For
        # them, the email-domain match is just OAuth tenant routing, not an
        # access gate.
        if not user.is_super_admin and user.company_id != company_id:
            return _login_failure(
                "Account / organization mismatch. Contact your admin.",
                log_msg=f"user {user.user_id} company={user.company_id} != domain company={company_id}",
            )

        # Step 4 — enforce account status. Suspended/inactive accounts can't
        # sign in via SSO any more than they can via password.
        if not user.is_active:
            return _login_failure(
                "Your account is inactive. Contact your admin.",
                log_msg=f"inactive user {user.user_id} attempted SSO",
            )

        # Step 5 — actually log them in. login_user issues the Flask-Login
        # session cookie; everything downstream (current_user, role gates,
        # PageRouter) treats this exactly like a password login.
        login_user(user)

        # Seed the org-context switcher with the domain-resolved company.
        # For super_admins this is the *only* signal — get_effective_company_id()
        # falls back to None without it, and every _require_company() route
        # returns 400 "No company context", leaving the dropdowns empty and
        # the grade page unusable. Mayfair domain → company_id=25 here.
        # No-op for non-super-admins (their effective company comes from
        # current_user.company_id).
        session["active_org_id"] = company_id

        # Stamp last-login timestamp. Password login already does this in
        # auth.authenticate_user, but SSO bypasses that helper entirely —
        # without this, the Team page shows "Last Login: Never" for every
        # SSO user forever. Wrapped so a stamp failure never blocks login.
        try:
            conn.execute(
                q("UPDATE users SET user_last_login_at = CURRENT_TIMESTAMP "
                  "WHERE user_id = ?"),
                (user.user_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("[sso] failed to stamp last_login_at user_id=%s",
                             user.user_id)

        logger.info("[sso] login OK user_id=%s email=%s company_id=%s",
                    user.user_id, email, company_id)

        # safe_next_url rejects open-redirect tricks (//evil.com, backslashes,
        # absolute URLs) — stronger than a bare startswith("/") check.
        next_url = safe_next_url(session.pop("sso_next", None)) or "/app"
        return redirect(next_url)
    finally:
        conn.close()
