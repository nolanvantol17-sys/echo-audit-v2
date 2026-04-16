"""
auth.py — Authentication and session management for Echo Audit V2.

Uses Flask-Login for session handling and werkzeug for password hashing.
No ORM — raw SQL via db.get_conn() / db.q().

The User class maps V2 users/user_roles/roles/departments tables. Most
properties are eagerly joined at load time so current_user.* access
doesn't issue additional queries.
"""

import logging
from functools import wraps

from flask import abort, redirect, url_for
from flask_login import (
    LoginManager, UserMixin,
    current_user, login_user, logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_conn, q, IS_POSTGRES

logger = logging.getLogger(__name__)

# Password hashing method — pinned for cross-version werkzeug compatibility.
PASSWORD_METHOD = "pbkdf2:sha256:260000"

# Status ID for "active" (general category). Matches seed in db.py.
STATUS_ACTIVE = 1


login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = None  # app renders its own login page


# ── User class ──────────────────────────────────────────────────


_USER_SELECT = """
    SELECT
        u.user_id,
        u.user_role_id,
        u.department_id,
        u.user_email,
        u.user_password_hash,
        u.user_first_name,
        u.user_last_name,
        u.status_id,
        u.user_must_change_password,
        u.user_last_login_at,
        r.role_name,
        d.company_id AS company_id_via_department
    FROM users u
    LEFT JOIN user_roles ur ON ur.user_role_id = u.user_role_id
    LEFT JOIN roles      r  ON r.role_id       = ur.role_id
    LEFT JOIN departments d ON d.department_id = u.department_id
"""


class User(UserMixin):
    """Flask-Login user wrapper over a joined users row.

    Built from a single row containing user + role + department → company JOIN.
    All V2 column names are mapped to V1-style accessors for compatibility
    with the rest of the app (id, email, first_name, last_name, role,
    is_active, company_id, full_name, is_super_admin).
    """

    def __init__(self, row):
        # row is a dict-like (psycopg2 RealDictCursor row or sqlite3.Row)
        # — normalize key access.
        def g(k):
            try:
                return row[k]
            except (KeyError, IndexError):
                return None

        # Raw V2 columns
        self.user_id                     = g("user_id")
        self.user_role_id                = g("user_role_id")
        self.department_id               = g("department_id")
        self.user_email                  = g("user_email")
        self.user_password_hash          = g("user_password_hash")
        self.user_first_name             = g("user_first_name")
        self.user_last_name              = g("user_last_name")
        self.status_id                   = g("status_id")
        self.user_must_change_password   = bool(g("user_must_change_password"))
        self.user_last_login_at          = g("user_last_login_at")
        self.role_name                   = g("role_name")
        self._company_id_via_department  = g("company_id_via_department")

    # ── Flask-Login interface ──
    def get_id(self):
        """Flask-Login requires str ID."""
        return str(self.user_id) if self.user_id is not None else None

    @property
    def is_active(self):
        """Flask-Login respects this — returning False blocks login."""
        return self.status_id == STATUS_ACTIVE

    @property
    def is_authenticated(self):
        return True  # instance only exists for logged-in users

    @property
    def is_anonymous(self):
        return False

    # ── V1-compat accessors (preferred in view/template code) ──
    @property
    def id(self):         return self.user_id
    @property
    def email(self):      return self.user_email
    @property
    def first_name(self): return self.user_first_name
    @property
    def last_name(self):  return self.user_last_name
    @property
    def role(self):       return self.role_name

    @property
    def company_id(self):
        """Derived via department_id → departments.company_id.
        Returns None if the user has no department (e.g. super admins,
        or admins just created by signup before a department is assigned).
        """
        return self._company_id_via_department

    @property
    def full_name(self):
        parts = [p for p in (self.user_first_name, self.user_last_name) if p]
        return " ".join(parts).strip()

    @property
    def is_super_admin(self):
        return self.role_name == "super_admin"

    @property
    def must_change_password(self):
        return self.user_must_change_password

    def __repr__(self):
        return f"<User id={self.user_id} email={self.user_email!r} role={self.role_name!r}>"


def _load_user_row(conn, *, user_id=None, email=None):
    """Fetch a joined user row by user_id OR by email. Returns dict or None."""
    if user_id is not None:
        cur = conn.execute(
            q(_USER_SELECT + " WHERE u.user_id = ? AND u.user_deleted_at IS NULL"),
            (user_id,),
        )
    elif email is not None:
        cur = conn.execute(
            q(_USER_SELECT + " WHERE LOWER(u.user_email) = LOWER(?) AND u.user_deleted_at IS NULL"),
            (email,),
        )
    else:
        return None
    return cur.fetchone()


# ── Flask-Login loader ─────────────────────────────────────────


@login_manager.user_loader
def load_user(user_id):
    """Called by Flask-Login on every authenticated request."""
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        return None

    conn = get_conn()
    try:
        row = _load_user_row(conn, user_id=user_id_int)
        return User(row) if row else None
    finally:
        conn.close()


# ── Public API ─────────────────────────────────────────────────


def authenticate_user(email, password):
    """Verify credentials and stamp user_last_login_at on success.

    Returns a User instance on success, None on failure. Inactive users
    (status_id != 1) can successfully authenticate but Flask-Login will
    reject them via is_active — callers should honor user.is_active
    before calling login_user().
    """
    if not email or not password:
        return None

    conn = get_conn()
    try:
        row = _load_user_row(conn, email=email)
        if not row:
            return None
        if not row["user_password_hash"]:
            return None
        if not check_password_hash(row["user_password_hash"], password):
            return None

        # Stamp last login. Not critical — wrapped in try/except so a
        # timestamp failure never blocks login.
        try:
            conn.execute(
                q("UPDATE users SET user_last_login_at = CURRENT_TIMESTAMP WHERE user_id = ?"),
                (row["user_id"],),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("Failed to update user_last_login_at for user_id=%s", row["user_id"])

        return User(row)
    finally:
        conn.close()


def _get_or_create_user_role_for(conn, role_id):
    """Return a user_role_id wrapping the given role_id. Reuses an existing
    row if one exists, otherwise creates one. Each role typically has a
    single shared user_roles row that many users reference.
    """
    cur = conn.execute(
        q("SELECT user_role_id FROM user_roles WHERE role_id = ? LIMIT 1"),
        (role_id,),
    )
    row = cur.fetchone()
    if row:
        return row["user_role_id"] if IS_POSTGRES else row[0]

    if IS_POSTGRES:
        cur = conn.execute(
            "INSERT INTO user_roles (role_id) VALUES (%s) RETURNING user_role_id",
            (role_id,),
        )
        return cur.fetchone()["user_role_id"]
    else:
        conn.execute("INSERT INTO user_roles (role_id) VALUES (?)", (role_id,))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def email_exists(email):
    """Return True if a user with this email already exists (case-insensitive)."""
    if not email:
        return False
    conn = get_conn()
    try:
        row = conn.execute(
            q("SELECT 1 FROM users WHERE LOWER(user_email) = LOWER(?) LIMIT 1"),
            (email,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def create_user(email, password, role_name, first_name, last_name, department_id=None):
    """Create a new user with the given role. Returns the new user_id.

    Raises ValueError if email already exists or role_name is unknown.
    """
    if not email or not password or not role_name or not first_name or not last_name:
        raise ValueError("email, password, role_name, first_name, last_name are required")

    conn = get_conn()
    try:
        # Email uniqueness check (race-free via UNIQUE constraint, but
        # explicit check gives a nicer error).
        existing = conn.execute(
            q("SELECT 1 FROM users WHERE LOWER(user_email) = LOWER(?) LIMIT 1"),
            (email,),
        ).fetchone()
        if existing:
            raise ValueError(f"User with email {email!r} already exists")

        # Resolve role_name → role_id
        role_row = conn.execute(
            q("SELECT role_id FROM roles WHERE role_name = ?"),
            (role_name,),
        ).fetchone()
        if not role_row:
            raise ValueError(f"Unknown role_name: {role_name!r}")
        role_id = role_row["role_id"] if IS_POSTGRES else role_row[0]

        # Get or create a user_roles row wrapping this role
        user_role_id = _get_or_create_user_role_for(conn, role_id)

        password_hash = generate_password_hash(password, method=PASSWORD_METHOD)

        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO users (
                       user_role_id, department_id, user_email, user_password_hash,
                       user_first_name, user_last_name, status_id
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   RETURNING user_id""",
                (user_role_id, department_id, email, password_hash,
                 first_name, last_name, STATUS_ACTIVE),
            )
            user_id = cur.fetchone()["user_id"]
        else:
            conn.execute(
                """INSERT INTO users (
                       user_role_id, department_id, user_email, user_password_hash,
                       user_first_name, user_last_name, status_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_role_id, department_id, email, password_hash,
                 first_name, last_name, STATUS_ACTIVE),
            )
            user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        conn.commit()
        return user_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_company(name):
    """Create a company with status_id = 1 (active). Returns company_id."""
    if not name:
        raise ValueError("company name is required")

    conn = get_conn()
    try:
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO companies (company_name, status_id)
                   VALUES (%s, %s) RETURNING company_id""",
                (name, STATUS_ACTIVE),
            )
            company_id = cur.fetchone()["company_id"]
        else:
            conn.execute(
                "INSERT INTO companies (company_name, status_id) VALUES (?, ?)",
                (name, STATUS_ACTIVE),
            )
            company_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Seed the Phase 6 per-company settings inside the same transaction so
        # a failed seed rolls back the company row too.
        from db import seed_company_defaults
        seed_company_defaults(company_id, conn=conn)

        conn.commit()
        return company_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_department(company_id, department_name):
    """Internal helper — create a department under a company. Returns department_id.

    Used by signup to attach the admin user to a default 'Leadership'
    department so their company_id is derivable.
    """
    conn = get_conn()
    try:
        if IS_POSTGRES:
            cur = conn.execute(
                """INSERT INTO departments (company_id, department_name, status_id)
                   VALUES (%s, %s, %s) RETURNING department_id""",
                (company_id, department_name, STATUS_ACTIVE),
            )
            dept_id = cur.fetchone()["department_id"]
        else:
            conn.execute(
                "INSERT INTO departments (company_id, department_name, status_id) VALUES (?, ?, ?)",
                (company_id, department_name, STATUS_ACTIVE),
            )
            dept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return dept_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_password(user_id, new_password, clear_must_change=True):
    """Set a user's password. Optionally clears the must_change flag."""
    if not new_password:
        raise ValueError("new_password is required")

    password_hash = generate_password_hash(new_password, method=PASSWORD_METHOD)
    conn = get_conn()
    try:
        if clear_must_change:
            conn.execute(
                q("""UPDATE users
                     SET user_password_hash = ?, user_must_change_password = ?
                     WHERE user_id = ?"""),
                (password_hash, False, user_id),
            )
        else:
            conn.execute(
                q("UPDATE users SET user_password_hash = ? WHERE user_id = ?"),
                (password_hash, user_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Decorators ─────────────────────────────────────────────────


def role_required(*roles):
    """Restrict a view to users whose role is in the given set.

    Usage:
        @app.route("/admin")
        @role_required("admin", "super_admin")
        def admin_view():
            ...
    """
    allowed = {r for r in roles}

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("login"))
            if current_user.role not in allowed:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ── Flask wiring ───────────────────────────────────────────────


def init_login_manager(app):
    """Attach the LoginManager to a Flask app."""
    login_manager.init_app(app)
