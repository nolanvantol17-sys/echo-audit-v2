"""
app.py — Echo Audit V2 Flask application entry point.

Phase 1: foundation only. Auth, session, signup, /app placeholder, /api/me.
No business logic, no domain routes — just the scaffolding the rest of
the app will hang off of.
"""

import logging
import os
import secrets
from pathlib import Path

from flask import (
    Flask, flash, jsonify, redirect, render_template, request, session, url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash

import db
import auth
from api_routes import api_bp
from audit_log_routes import audit_log_bp
from location_notes_routes import location_notes_bp
from dashboard_routes import dashboard_bp
from grade_jobs_routes import grade_jobs_bp
from interactions_routes import interactions_bp
from performance_reports import reports_bp
from platform_routes import platform_bp
from rubric_ai_routes import rubric_ai_bp
from rubrics_routes import rubrics_bp
from voip_routes import voip_bp
# Phase 6 modules
from account_routes import account_bp
from export_routes import export_bp
from bulk_export_routes import bulk_export_bp
from labels_routes import labels_bp
from platform_admin_routes import platform_admin_bp
from settings_routes import settings_bp
from scheduled_calls_routes import scheduled_calls_bp
from active_jobs_routes import active_jobs_bp
# Re-exported from helpers so "from app import get_effective_company_id" works
# for any callers that expect the helper to live on the main app module.
# check_rate_limit and increment_usage live in helpers to avoid a circular
# import with interactions_routes, but callers can still import them here.
from helpers import (  # noqa: F401
    check_rate_limit,
    get_effective_company_id,
    increment_usage,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# Load the logo once at startup so templates can embed it inline. Optional —
# absence is OK; the templates fall back to a text-only header.
try:
    with open(Path(__file__).parent / "static" / "logo_base64.txt", "r") as _lf:
        LOGO_BASE64 = _lf.read().strip()
except Exception:
    LOGO_BASE64 = ""


# Product branding. Can be overridden by env vars for white-label deployments.
CLIENT_NAME      = os.environ.get("CLIENT_NAME",      "Echo Audit")
CLIENT_FULL_NAME = os.environ.get("CLIENT_FULL_NAME", "Echo Audit")


# Static-asset cache-buster. Railway bumps file mtimes on redeploy, so the
# version string turns over automatically; templates append it as ?v=<mtime>
# alongside a long Cache-Control on /static/* so browsers skip the re-download
# on every sidebar navigation.
_STATIC_VERSION_CACHE = {}


def _static_version(filename):
    if filename in _STATIC_VERSION_CACHE:
        return _STATIC_VERSION_CACHE[filename]
    try:
        v = str(int((Path(__file__).parent / "static" / filename).stat().st_mtime))
    except OSError:
        v = "0"
    _STATIC_VERSION_CACHE[filename] = v
    return v


# Opt-in helper for routes that participate in the HTMX page router. When the
# client sends HX-Request, base.html skips its chrome and renders just
# extra_css + content + extra_js so the response can be swapped into
# <main id="app-main">. Routes still using plain render_template are
# unaffected — _fragment defaults to falsy and the full shell renders.
def render_page(template_name, **ctx):
    ctx["_fragment"] = bool(request.headers.get("HX-Request"))
    return render_template(template_name, **ctx)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    # Initialize database schema + seed defaults on startup
    db.init_app(app)

    # Wire up Flask-Login
    auth.init_login_manager(app)

    register_routes(app)
    _register_context_processors(app)

    # Phase 2 API routes
    app.register_blueprint(api_bp)

    # Phase 3 grading-flow routes
    app.register_blueprint(interactions_bp)

    # Phase 9: async grading queue (split-pane workflow)
    app.register_blueprint(grade_jobs_bp)

    # Phase 4 routes: rubrics, dashboard, performance reports,
    # platform usage (super_admin), audit log reader.
    app.register_blueprint(rubrics_bp)
    app.register_blueprint(rubric_ai_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(platform_bp)
    app.register_blueprint(audit_log_bp)
    app.register_blueprint(location_notes_bp)

    # Phase 5: VoIP integration (webhook + config + queue)
    app.register_blueprint(voip_bp)

    # Arc C: scheduled_calls (outbound AI shop scheduling — bridges to
    # the external AI caller service, parallel to voip_bp's inbound bridge).
    app.register_blueprint(scheduled_calls_bp)

    # Persistent multi-job dock — unified feed UNION'ing grade_jobs +
    # scheduled_calls into one shape for the base.html dock UI.
    app.register_blueprint(active_jobs_bp)

    # Phase 6: settings, account, labels, export, super-admin platform
    app.register_blueprint(settings_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(labels_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(bulk_export_bp)
    app.register_blueprint(platform_admin_bp)

    # JSON error handlers for /api/ paths (HTML paths get default handling)
    _register_error_handlers(app)

    # Flask-Login: return 401 JSON for unauthenticated API calls instead of
    # redirecting to /login (which would confuse API clients).
    @auth.login_manager.unauthorized_handler
    def _unauthorized():
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login"))

    # Long-cache /static/* — templates pair this with a ?v=<mtime> cache-buster
    # so redeploys invalidate the old asset. Without this, every sidebar nav
    # re-downloads the shell (~94 KB) and the refresh icon spins on each click.
    app.add_template_global(_static_version, name="static_v")

    @app.after_request
    def _set_static_cache_headers(resp):
        if request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    return app


def _register_context_processors(app):
    """Expose branding + current-user info to every rendered template.

    Values here are available implicitly in all Jinja2 contexts, so routes
    that call render_template() don't need to pass them explicitly. Mirrors
    the V1 inject_client_config() pattern.
    """
    @app.context_processor
    def inject_client_config():
        ctx = {
            "client_name":         CLIENT_NAME,
            "client_full_name":    CLIENT_FULL_NAME,
            "logo_base64":         LOGO_BASE64,
            "user_role":           None,
            "user_name":           None,
            "user_email":          None,
            "user_id":             None,
            "active_org_id":       None,
            "active_org_name":     None,
            "all_orgs":            None,
            "location_label":      "Location",
            "property_list_label": "Locations",
            "keyterms_prompt_max_terms": 200,
            "keyterm_min_length":  5,
            "keyterm_max_length":  50,
            "active_jobs_initial": [],
        }
        try:
            import grader as _grader
            ctx["keyterms_prompt_max_terms"] = _grader.KEYTERMS_PROMPT_MAX_TERMS
            ctx["keyterm_min_length"] = _grader.KEYTERM_MIN_LENGTH
            ctx["keyterm_max_length"] = _grader.KEYTERM_MAX_LENGTH
        except Exception:
            pass
        if not current_user.is_authenticated:
            return ctx

        ctx["user_role"]  = current_user.role
        ctx["user_name"]  = current_user.full_name
        ctx["user_email"] = current_user.email
        ctx["user_id"]    = current_user.user_id

        active_cid = get_effective_company_id()
        ctx["active_org_id"] = active_cid

        # Dock first-paint payload — server-side initial state for the
        # persistent multi-job dock so it renders populated on first paint
        # instead of flashing empty before the first /api/active-jobs poll.
        # Best-effort: a query failure here must not 500 the page render.
        # Skipped on /app/grade where the dock is suppressed in favor of
        # the in-page #gj-pane (see is_grade gate in base.html).
        if request.path != "/app/grade":
            try:
                from active_jobs_routes import get_active_jobs_for_user
                ctx["active_jobs_initial"] = get_active_jobs_for_user(
                    active_cid, current_user.user_id,
                )
            except Exception:
                logger.warning("active_jobs context processor failed", exc_info=True)

        try:
            conn = db.get_conn()
            try:
                if active_cid is not None:
                    cur = conn.execute(
                        db.q("SELECT company_name FROM companies WHERE company_id = ?"),
                        (active_cid,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        try:
                            ctx["active_org_name"] = row["company_name"]
                        except (KeyError, TypeError, IndexError):
                            ctx["active_org_name"] = row[0]

                    # Pull label settings so templates can localize UI text
                    cur = conn.execute(
                        db.q("""SELECT company_setting_key, company_setting_value
                                FROM company_settings
                                WHERE company_id = ?"""),
                        (active_cid,),
                    )
                    for r in cur.fetchall():
                        try:
                            k, v = r["company_setting_key"], r["company_setting_value"]
                        except (KeyError, TypeError, IndexError):
                            k, v = r[0], r[1]
                        if k == "location_label":
                            ctx["location_label"] = v
                        elif k == "location_list_label":
                            ctx["property_list_label"] = v

                # Super-admin org picker data
                if current_user.is_super_admin:
                    cur = conn.execute(db.q(
                        """SELECT company_id, company_name FROM companies
                           WHERE company_deleted_at IS NULL
                           ORDER BY company_name"""
                    ))
                    orgs = []
                    for r in cur.fetchall():
                        try:
                            orgs.append({"company_id": r["company_id"],
                                         "company_name": r["company_name"]})
                        except (KeyError, TypeError, IndexError):
                            orgs.append({"company_id": r[0], "company_name": r[1]})
                    ctx["all_orgs"] = orgs
            finally:
                conn.close()
        except Exception:
            # Context processors must never block a render
            pass
        return ctx


def _register_error_handlers(app):
    @app.errorhandler(403)
    def _403(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Forbidden"}), 403
        return e, 403

    @app.errorhandler(404)
    def _404(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return e, 404

    @app.errorhandler(405)
    def _405(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "Method not allowed"}), 405
        return e, 405


# ── Helpers ─────────────────────────────────────────────────────


def _setup_wizard_should_skip(company_id):
    """Return True when /app/setup should redirect to the dashboard.

    Skip when the company has any locations OR rubric_groups OR projects, OR
    when the admin has previously dismissed the wizard. Returns True (skip)
    on any DB error so a glitch in the suppression check never traps a real
    user inside the wizard.
    """
    if company_id is None:
        return True
    try:
        conn = db.get_conn()
        try:
            cur = conn.execute(
                db.q("SELECT company_setup_dismissed_at FROM companies "
                     "WHERE company_id = ?"),
                (company_id,),
            )
            row = cur.fetchone()
            dismissed = None
            if row is not None:
                try:
                    dismissed = row["company_setup_dismissed_at"]
                except (KeyError, TypeError, IndexError):
                    dismissed = row[0]
            if dismissed is not None:
                return True

            # Rubric groups don't carry company_id directly — they're scoped
            # via locations. All-locations rubric groups (location_id NULL)
            # are always created alongside a project, so the projects check
            # covers that path.
            for sql in (
                "SELECT 1 FROM locations WHERE company_id = ? "
                "AND location_deleted_at IS NULL LIMIT 1",
                "SELECT 1 FROM projects WHERE company_id = ? "
                "AND project_deleted_at IS NULL LIMIT 1",
                "SELECT 1 FROM rubric_groups rg "
                "JOIN locations l ON l.location_id = rg.location_id "
                "WHERE l.company_id = ? AND rg.rg_deleted_at IS NULL "
                "AND l.location_deleted_at IS NULL LIMIT 1",
            ):
                cur = conn.execute(db.q(sql), (company_id,))
                if cur.fetchone() is not None:
                    return True
            return False
        finally:
            conn.close()
    except Exception:
        logger.exception("Setup-wizard suppression check failed")
        return True


def _mark_setup_dismissed(company_id):
    conn = db.get_conn()
    try:
        if db.IS_POSTGRES:
            conn.execute(
                "UPDATE companies SET company_setup_dismissed_at = NOW() "
                "WHERE company_id = %s AND company_setup_dismissed_at IS NULL",
                (company_id,),
            )
        else:
            conn.execute(
                "UPDATE companies "
                "SET company_setup_dismissed_at = CURRENT_TIMESTAMP "
                "WHERE company_id = ? AND company_setup_dismissed_at IS NULL",
                (company_id,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Routes ──────────────────────────────────────────────────────


def register_routes(app):

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("app_home"))
        return render_template("login.html")

    # ── Login ──
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("app_home"))

        error = None
        email = ""
        if request.method == "POST":
            email = (request.form.get("email") or "").strip()
            password = request.form.get("password") or ""
            remember = bool(request.form.get("remember"))
            user = auth.authenticate_user(email, password)

            if user is None:
                error = "Invalid email or password."
            elif not user.is_active:
                error = "This account is inactive."
            else:
                login_user(user, remember=remember)
                if user.must_change_password:
                    return redirect(url_for("change_password"))
                return redirect(url_for("app_home"))

        return render_template("login.html", error=error, email=email)

    # ── Signup ──
    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if current_user.is_authenticated:
            return redirect(url_for("app_home"))

        error = None
        company_name = email = first_name = last_name = ""
        if request.method == "POST":
            form = request.form
            # Accept either `company_name` (V2-native) or `org_name` (V1 template)
            company_name     = (form.get("company_name") or form.get("org_name") or "").strip()
            email            = (form.get("email") or "").strip()
            password         = form.get("password") or ""
            confirm_password = form.get("confirm_password") or ""
            first_name       = (form.get("first_name") or "").strip()
            last_name        = (form.get("last_name") or "").strip()

            if not (company_name and email and password and first_name and last_name):
                error = "All fields are required."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif confirm_password and confirm_password != password:
                error = "Passwords do not match."
            else:
                try:
                    # Check email uniqueness BEFORE creating the company so
                    # a failed signup doesn't leave an orphaned company row.
                    if auth.email_exists(email):
                        raise ValueError(f"User with email {email!r} already exists")

                    # Create company → default department → admin user.
                    # The default department is created so the admin's
                    # company_id is derivable via department_id → departments.
                    company_id = auth.create_company(company_name)
                    dept_id = auth.create_department(company_id, "Leadership")
                    user_id = auth.create_user(
                        email=email,
                        password=password,
                        role_name="admin",
                        first_name=first_name,
                        last_name=last_name,
                        department_id=dept_id,
                    )
                    # Log the new admin in
                    user = auth.load_user(user_id)
                    if user is None:
                        error = "Account created but could not log in. Please try logging in."
                    else:
                        login_user(user)
                        # First-time admins land in the setup wizard. The
                        # wizard self-suppresses (redirects to /app) when
                        # data already exists or has been dismissed, so this
                        # is safe even if a fresh signup somehow inherits
                        # state (e.g. the admin retried after a partial run).
                        return redirect(url_for("setup_wizard"))
                except ValueError as e:
                    error = str(e)
                except Exception:
                    logger.exception("Signup failed")
                    error = "Signup failed. Please try again."

        return render_template(
            "signup.html",
            error=error,
            company_name=company_name,
            org_name=company_name,  # alias so V1-style templates work too
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

    # ── Forgot password ──
    # Static informational page only — there is no email-based reset flow yet.
    # The page tells users to contact their org admin (admins are listed when
    # an email is provided, otherwise generic guidance is shown).
    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if current_user.is_authenticated:
            return redirect(url_for("change_password"))

        submitted = False
        admins = []
        email = (request.form.get("email") if request.method == "POST" else "") or ""
        email = email.strip()
        if request.method == "POST":
            submitted = True
            if email:
                try:
                    conn = db.get_conn()
                    try:
                        # Resolve the company for the entered email, then list
                        # active admins of that company so the user knows whom
                        # to contact. Lookup is best-effort and does NOT reveal
                        # whether the email itself exists (we always render the
                        # same "If your account exists…" message below).
                        cur = conn.execute(
                            db.q("""SELECT d.company_id
                                    FROM users u
                                    JOIN departments d ON d.department_id = u.department_id
                                    WHERE LOWER(u.user_email) = LOWER(?)
                                    LIMIT 1"""),
                            (email,),
                        )
                        row = cur.fetchone()
                        company_id = None
                        if row is not None:
                            try:
                                company_id = row["company_id"]
                            except (KeyError, TypeError, IndexError):
                                company_id = row[0]
                        if company_id is not None:
                            cur = conn.execute(
                                db.q("""SELECT u.user_email, u.user_first_name, u.user_last_name
                                        FROM users u
                                        JOIN user_roles ur ON ur.user_role_id = u.user_role_id
                                        JOIN roles      r  ON r.role_id       = ur.role_id
                                        JOIN departments d ON d.department_id = u.department_id
                                        WHERE d.company_id = ?
                                          AND r.role_name IN ('admin','super_admin')
                                          AND u.status_id = 1
                                        ORDER BY u.user_email
                                        LIMIT 5"""),
                                (company_id,),
                            )
                            for r in cur.fetchall():
                                try:
                                    admins.append({
                                        "email": r["user_email"],
                                        "name":  " ".join(
                                            s for s in (r["user_first_name"], r["user_last_name"]) if s
                                        ).strip() or None,
                                    })
                                except (KeyError, TypeError, IndexError):
                                    admins.append({
                                        "email": r[0],
                                        "name":  " ".join(s for s in (r[1], r[2]) if s).strip() or None,
                                    })
                    finally:
                        conn.close()
                except Exception:
                    logger.exception("Forgot-password admin lookup failed")
                    # Swallow — the page still renders the generic message.
        return render_template(
            "forgot_password.html",
            email=email,
            submitted=submitted,
            admins=admins,
        )

    # ── Change password ──
    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def change_password():
        error = None
        success = None
        forced = bool(current_user.must_change_password)

        # Settings → Account is the canonical change-password surface. This page
        # is now reserved for the forced-rotation flow (admin reset, expired
        # temp password). A non-forced GET means the user navigated here from
        # a stale link or bookmark — bounce them to the unified UI.
        if request.method == "GET" and not forced:
            return redirect("/app/settings#account")

        if request.method == "POST":
            current_password = request.form.get("current_password") or ""
            new_password     = request.form.get("new_password") or ""
            confirm_password = request.form.get("confirm_password") or ""

            # Verify current password against the stored hash. Skipped when the
            # user was force-redirected here after an admin reset AND the
            # must_change flag is still set, because a just-issued temp password
            # is still theoretically the "current" password but we don't want
            # to treat an unknown temp as a hard block. Either way we still
            # require SOMETHING in the field so a logged-in attacker can't
            # silently change the password.
            if not current_password:
                error = "Current password is required."
            elif len(new_password) < 8:
                error = "Password must be at least 8 characters."
            elif new_password != confirm_password:
                error = "Passwords do not match."
            else:
                # Re-fetch the hash from the DB — current_user has it on the
                # session object but re-reading avoids stale-hash edge cases.
                conn = db.get_conn()
                try:
                    cur = conn.execute(
                        db.q("SELECT user_password_hash FROM users WHERE user_id = ?"),
                        (current_user.user_id,),
                    )
                    row = cur.fetchone()
                finally:
                    conn.close()
                stored_hash = None
                if row is not None:
                    try:
                        stored_hash = row["user_password_hash"]
                    except (KeyError, TypeError, IndexError):
                        stored_hash = row[0]

                if not stored_hash or not check_password_hash(stored_hash, current_password):
                    error = "Current password is incorrect."
                else:
                    try:
                        auth.update_password(
                            user_id=current_user.user_id,
                            new_password=new_password,
                            clear_must_change=True,
                        )
                        success = "Password updated."
                        # Refresh the session user so the cleared must_change
                        # flag is reflected before the next redirect.
                        refreshed = auth.load_user(current_user.user_id)
                        if refreshed is not None:
                            login_user(refreshed)
                        if forced:
                            return redirect(url_for("app_home"))
                    except Exception:
                        logger.exception("Password update failed")
                        error = "Password update failed."

        return render_template(
            "change_password.html",
            error=error,
            success=success,
            forced=forced,
        )

    # ── Logout ──
    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        logout_user()
        return redirect(url_for("login"))

    # ── Authenticated landing ──
    @app.route("/app")
    @login_required
    def app_home():
        return redirect(url_for("projects_page"))

    # ── Post-signup setup wizard ──
    # Walks a brand-new admin through creating one location, one rubric, and
    # one project so the empty product has something to grade against. The
    # wizard itself reuses the existing JSON APIs; this route just renders
    # the shell and gates entry.
    @app.route("/app/setup")
    @login_required
    def setup_wizard():
        # Non-admins have nothing to do here — back to the dashboard.
        if current_user.role not in ("admin", "super_admin"):
            return redirect(url_for("app_home"))
        if _setup_wizard_should_skip(get_effective_company_id()):
            return redirect(url_for("app_home"))
        return render_template("setup.html")

    @app.route("/app/setup/skip", methods=["POST"])
    @login_required
    def setup_wizard_skip():
        if current_user.role not in ("admin", "super_admin"):
            return jsonify({"error": "Forbidden"}), 403
        cid = get_effective_company_id()
        if cid is None:
            return jsonify({"error": "No active company"}), 400
        _mark_setup_dismissed(cid)
        return jsonify({"ok": True})

    # ── FE stub routes — each renders a placeholder that extends base.html.
    # Data loads client-side via fetch(); the routes only render the shell.

    @app.route("/app/projects")
    @login_required
    def projects_page():
        return render_template("projects.html")

    @app.route("/app/projects/<int:project_id>")
    @login_required
    def project_hub_page(project_id):
        return render_page("project_hub.html", project_id=project_id)

    @app.route("/app/grade")
    @login_required
    def grade_page():
        return render_template("grade.html")

    @app.route("/app/history")
    @login_required
    def history_page():
        return render_template("history.html")

    @app.route("/app/history/<int:interaction_id>")
    @login_required
    def interaction_detail_page(interaction_id):
        return render_page("interaction_detail.html", interaction_id=interaction_id)

    @app.route("/app/reports")
    @login_required
    def reports_page():
        return render_page("reports.html")

    @app.route("/app/team")
    @login_required
    @auth.role_required("manager", "admin", "super_admin")
    def team_page():
        return render_template("team.html")

    @app.route("/app/settings")
    @login_required
    @auth.role_required("admin", "super_admin")
    def settings_page():
        return render_template("settings.html")

    @app.route("/app/locations")
    @login_required
    @auth.role_required("admin", "super_admin")
    def locations_page():
        return render_template("locations.html")

    @app.route("/app/locations/<int:location_id>")
    @login_required
    @auth.role_required("admin", "super_admin")
    def location_detail_page(location_id):
        return render_page("location_detail.html", location_id=location_id)

    @app.route("/app/voip")
    @login_required
    @auth.role_required("admin", "super_admin")
    def voip_page():
        return render_template("voip.html")

    @app.route("/app/platform")
    @login_required
    @auth.role_required("super_admin")
    def platform_page():
        return render_template("platform.html")

    # ── Current-user JSON ──
    @app.route("/api/me")
    @login_required
    def api_me():
        impersonating_id = session.get("impersonating_user_id")
        impersonated_user_name = None
        impersonated_org_name  = None
        if impersonating_id:
            try:
                conn = db.get_conn()
                try:
                    cur = conn.execute(
                        db.q("""SELECT u.user_first_name, u.user_last_name,
                                       c.company_name
                                FROM users u
                                JOIN departments d ON d.department_id = u.department_id
                                JOIN companies  c ON c.company_id     = d.company_id
                                WHERE u.user_id = ?"""),
                        (impersonating_id,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        try:
                            fn = row["user_first_name"]; ln = row["user_last_name"]
                            cn = row["company_name"]
                        except (KeyError, TypeError, IndexError):
                            fn, ln, cn = row[0], row[1], row[2]
                        impersonated_user_name = (
                            " ".join([s for s in (fn, ln) if s]) or None
                        )
                        impersonated_org_name = cn
                finally:
                    conn.close()
            except Exception:
                logger.exception("Failed to resolve impersonation context for /api/me")

        return jsonify({
            "id":                     current_user.id,
            "user_id":                current_user.id,
            "email":                  current_user.email,
            "first_name":             current_user.first_name,
            "last_name":              current_user.last_name,
            "role":                   current_user.role,
            "company_id":             current_user.company_id,
            "impersonating":          bool(impersonating_id),
            "impersonated_user_id":   impersonating_id,
            "impersonated_user_name": impersonated_user_name,
            "impersonated_org_name":  impersonated_org_name,
        })


# ── Module-level app object (for gunicorn / flask run) ─────────


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))
