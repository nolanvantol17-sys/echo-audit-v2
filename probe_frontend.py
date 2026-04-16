"""
probe_frontend.py — Smoke-probe the Flask dev server end-to-end.

Hits auth pages + static assets with a real HTTP client, parses HTML for
obvious template errors, creates a test org via the super_admin API,
logs in as that org's admin, renders the dashboard, and replays every
fetch the dashboard JS makes on load. Reports what works and what's
broken — does NOT attempt to fix anything.
"""

import re
import sys
import time

import requests

BASE = "http://127.0.0.1:8080"

FINDINGS = []   # list of (level, area, route, note)


def log(level, area, route, note=""):
    FINDINGS.append((level, area, route, note))
    tag = {"OK": "✓", "WARN": "⚠", "FAIL": "✗"}.get(level, "·")
    print(f"  [{tag}] {level:4s} {area:14s} {route:50s} {note}")


def scan_html_errors(html):
    """Look for common Jinja / template misrender signatures."""
    issues = []
    if not html:
        return ["empty HTML"]
    # Unrendered Jinja tags are the loudest signal
    if "{{" in html or "{%" in html:
        # Some pages contain {{/}} JS interpolations that aren't Jinja — scan for the
        # actual tell-tale form (var names with no space/punctuation inside)
        for m in re.finditer(r"\{\{[\s\S]{0,80}?\}\}", html):
            snip = m.group(0)
            if "EA.esc" in snip or "navigator" in snip:  # JS template literal artifacts
                continue
            issues.append(f"unrendered Jinja token: {snip[:60]}")
            if len(issues) > 5: break
    # Obvious Werkzeug / Flask error page
    if "Traceback (most recent call last)" in html:
        issues.append("Werkzeug traceback on page")
    if "TemplateSyntaxError" in html or "UndefinedError" in html:
        issues.append("Jinja2 TemplateSyntaxError/UndefinedError")
    if "jinja2.exceptions" in html.lower():
        issues.append("jinja2.exceptions leaked into HTML")
    return issues


def scan_html_static_refs(html, route):
    """Return list of (url, kind) for each static-ish reference."""
    refs = []
    for m in re.finditer(r'(href|src)=["\']([^"\']+)["\']', html):
        url = m.group(2)
        if url.startswith("/static/") or url.startswith("/api/"):
            refs.append(url)
    return refs


def probe_auth_pages(s):
    print("\n── Section 1: auth pages + static assets ──")

    # Landing
    r = s.get(BASE + "/", allow_redirects=False)
    log("OK" if r.status_code in (200, 302) else "FAIL",
        "landing", "GET /",
        f"status={r.status_code} redirect→{r.headers.get('Location','—')}")
    if r.status_code == 200:
        for p in scan_html_errors(r.text):
            log("FAIL", "template", "GET /", p)

    # /login
    r = s.get(BASE + "/login")
    log("OK" if r.status_code == 200 else "FAIL",
        "login", "GET /login", f"status={r.status_code} bytes={len(r.content)}")
    if r.status_code == 200:
        for p in scan_html_errors(r.text):
            log("FAIL", "template", "GET /login", p)
        # Check that the form posts to /login and has email/password fields
        has_email = bool(re.search(r'name=["\']email["\']', r.text))
        has_pw    = bool(re.search(r'name=["\']password["\']', r.text))
        log("OK" if has_email else "FAIL", "login", "form:email field",
            "present" if has_email else "missing")
        log("OK" if has_pw else "FAIL", "login", "form:password field",
            "present" if has_pw else "missing")
        # Scan referenced assets
        for url in set(scan_html_static_refs(r.text, "/login")):
            if url.startswith("/static/"):
                r2 = s.get(BASE + url)
                log("OK" if r2.status_code == 200 else "FAIL",
                    "static", f"GET {url}", f"status={r2.status_code}")

    # /signup
    r = s.get(BASE + "/signup")
    log("OK" if r.status_code == 200 else "FAIL",
        "signup", "GET /signup", f"status={r.status_code}")
    if r.status_code == 200:
        for p in scan_html_errors(r.text):
            log("FAIL", "template", "GET /signup", p)
        required_fields = ["company_name", "email", "password",
                           "first_name", "last_name"]
        for f in required_fields:
            has = bool(re.search(rf'name=["\']({f}|org_name)["\']', r.text)) \
                  if f == "company_name" else \
                  bool(re.search(rf'name=["\']{f}["\']', r.text))
            log("OK" if has else "WARN", "signup", f"form:{f} field",
                "present" if has else "missing — not necessarily a bug")

    # /change-password — needs login, so expect a redirect on anon access
    r = s.get(BASE + "/change-password", allow_redirects=False)
    log("OK" if r.status_code in (200, 302) else "FAIL",
        "change_pw", "GET /change-password (anon)",
        f"status={r.status_code} loc={r.headers.get('Location','—')}")

    # Static assets directly
    for p in ("/static/app.css", "/static/app.js"):
        r = s.get(BASE + p)
        log("OK" if r.status_code == 200 else "FAIL",
            "static", f"GET {p}",
            f"status={r.status_code} bytes={len(r.content)} "
            f"type={r.headers.get('Content-Type','')}")


def create_org_via_api(super_session):
    print("\n── Section 2: POST /api/platform/orgs as super_admin ──")
    # Build a test org. Use a stable name so we can clean it up later.
    company_name = "Frontend Test Co"
    email = "fe_admin@echoaudit.test"
    r = super_session.post(
        BASE + "/api/platform/orgs",
        json={
            "company_name":     company_name,
            "industry_id":      1,
            "admin_email":      email,
            "admin_first_name": "Frontend",
            "admin_last_name":  "Admin",
            "admin_password":   "FeTest99!",
        },
    )
    body = r.json() if "json" in r.headers.get("Content-Type","") else {}
    ok = r.status_code in (200, 201)
    log("OK" if ok else "FAIL",
        "platform", "POST /api/platform/orgs",
        f"status={r.status_code} company_id={body.get('company_id')} "
        f"user_id={body.get('user_id')} has_temp_pw={bool(body.get('temp_password'))}")
    return (body.get("company_id"), email, body.get("temp_password") or "FeTest99!",
            r.status_code, body)


def login_session(email, password):
    s = requests.Session()
    r = s.post(BASE + "/login", data={"email": email, "password": password},
               allow_redirects=False)
    return s, r


def probe_dashboard(s):
    print("\n── Section 4: dashboard render + fetch replay ──")

    # /app
    r = s.get(BASE + "/app", allow_redirects=False)
    log("OK" if r.status_code == 200 else "FAIL",
        "dashboard", "GET /app",
        f"status={r.status_code} bytes={len(r.content)}")
    html = r.text if r.status_code == 200 else ""
    if html:
        for p in scan_html_errors(html):
            log("FAIL", "template", "GET /app", p)
        # Confirm nav items render for admin (expect sidebar brand + dashboard link)
        for hint, desc in [
            ("sidebar-brand",      "sidebar brand block"),
            ("nav-link",           "sidebar nav-link class"),
            ('data-react-root',    "(should NOT be present — we aren't using React)"),
            ('id="dashboard',      "dashboard root id"),
        ]:
            present = hint in html
            if hint.startswith("data-react"):
                # reverse-check — we expect this NOT to appear
                log("OK" if not present else "WARN", "dashboard",
                    f"html contains {hint!r}",
                    "absent (good)" if not present else "present — unexpected")
            else:
                log("OK" if present else "WARN", "dashboard",
                    f"html contains {hint!r}",
                    "present" if present else "missing")

    # Replay the fetches the dashboard JS issues on load.
    # Read index.html to be sure we're not missing any.
    endpoints = [
        "/api/me",
        "/api/dashboard",
        "/api/dashboard/chart",
        "/api/projects",
        "/api/rubric-groups",
    ]
    # Also pick up any /api/... reference in the dashboard HTML itself.
    for m in re.finditer(r'/api/[a-zA-Z0-9_\-/\?\=\.]+', html or ""):
        u = m.group(0).rstrip(".,;")
        # strip trailing quotes/parens
        u = re.sub(r'["\'\)].*$', '', u)
        if u not in endpoints:
            endpoints.append(u)

    print("  replaying dashboard fetches:")
    for ep in endpoints:
        r = s.get(BASE + ep)
        ct = r.headers.get("Content-Type","")
        if r.status_code == 200:
            log("OK", "api", f"GET {ep}", f"status=200 type={ct.split(';')[0]}")
        else:
            # Try to extract server-side error message
            body = ""
            try: body = str(r.json())[:160]
            except Exception: body = r.text[:160]
            log("FAIL", "api", f"GET {ep}",
                f"status={r.status_code} type={ct.split(';')[0]} body={body}")


def main():
    s_anon = requests.Session()
    probe_auth_pages(s_anon)

    # Log in as super_admin
    super_session, r_super = login_session(
        "superadmin@echoaudit.test", "SuperSecret9!")
    log("OK" if r_super.status_code == 302 else "FAIL",
        "login", "POST /login super_admin",
        f"status={r_super.status_code} loc={r_super.headers.get('Location','—')}")

    cid, fe_email, fe_pw, create_status, create_body = create_org_via_api(super_session)
    if create_status not in (200, 201):
        print(f"\nCannot proceed — org create returned {create_status}:")
        print(f"  {create_body}")
        return

    # Log out super, log in as the newly created FE admin
    print("\n── Section 3: log in as the newly created admin ──")
    # Release super session — super_admin cookies could shadow
    super_session.close()
    fe_s, r_fe = login_session(fe_email, fe_pw)
    log("OK" if r_fe.status_code == 302 else "FAIL",
        "login", "POST /login fe_admin",
        f"status={r_fe.status_code} loc={r_fe.headers.get('Location','—')} "
        f"email={fe_email}")

    # First login triggers force-change-password redirect
    loc = (r_fe.headers.get("Location") or "")
    if "change-password" in loc:
        log("OK", "login", "POST /login fe_admin",
            "forced to /change-password on first login — expected")
        # Change the password so we can get to the dashboard
        r_cp = fe_s.post(BASE + "/change-password", data={
            "current_password": fe_pw,
            "new_password":     "FeTest100!",
            "confirm_password": "FeTest100!",
        }, allow_redirects=False)
        log("OK" if r_cp.status_code in (200, 302) else "FAIL",
            "change_pw", "POST /change-password",
            f"status={r_cp.status_code} loc={r_cp.headers.get('Location','—')}")

    probe_dashboard(fe_s)

    # Summary
    print("\n══ Summary ══")
    ok    = sum(1 for l, *_ in FINDINGS if l == "OK")
    warn  = sum(1 for l, *_ in FINDINGS if l == "WARN")
    fail  = sum(1 for l, *_ in FINDINGS if l == "FAIL")
    print(f"  OK={ok}  WARN={warn}  FAIL={fail}")
    if fail:
        print("\n  FAILS:")
        for lvl, area, route, note in FINDINGS:
            if lvl == "FAIL":
                print(f"    · {area:12s} {route:40s} {note}")
    if warn:
        print("\n  WARNS:")
        for lvl, area, route, note in FINDINGS:
            if lvl == "WARN":
                print(f"    · {area:12s} {route:40s} {note}")


if __name__ == "__main__":
    main()
