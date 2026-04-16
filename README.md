# Echo Audit

AI-graded call quality assurance for distributed teams. Upload (or auto-ingest via VoIP webhook) a sales/support call recording, get back a transcript, a per-criterion scorecard against your custom rubric, and a pre-call brief next time someone dials the same location.

Multi-tenant SaaS — companies → departments → users → projects → calls — with role-based access (super_admin / admin / manager / agent), append-only audit logging, and per-tenant rate limiting on every external AI call.

## Stack

- **Backend:** Flask (app factory + blueprints), Flask-Login, raw SQL via `psycopg2` / `sqlite3` (no ORM)
- **DB:** PostgreSQL in prod (Railway), SQLite local fallback. Single schema source of truth in [schema.sql](schema.sql).
- **Frontend:** Server-rendered Jinja2 + vanilla JS (`window.EA` helpers in [static/app.js](static/app.js))
- **AI:** Anthropic Claude (grading, clarifying questions, rubric generation, per-location briefs), AssemblyAI (transcription)
- **VoIP:** Pluggable provider abstraction in [voip/](voip/) — webhook ingestion, HMAC-verified, credentials encrypted at rest with Fernet
- **Auth:** pbkdf2:sha256:260000 password hashing, super-admin impersonation via session

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python -c "from app import create_app; create_app()"   # bootstraps schema on first boot
flask --app app run --debug
```

App boots at http://localhost:5000. With `DATABASE_URL` unset, it falls back to a local SQLite file (`echoaudit.db`).

Schema bootstrap is in [db.py](db.py) — runs `schema.sql` on first boot if the `statuses` table is missing. There's no migrations framework; schema changes are made by editing `schema.sql` and applying the diff manually.

## Deploy notes

- **Railway:** push to `main`, Railway auto-deploys. All env vars from [.env.example](.env.example) must be set in the Railway dashboard.
- **Process:** `gunicorn 'app:create_app()' --bind 0.0.0.0:$PORT`
- **DB:** the existing Railway Postgres plugin provides `DATABASE_URL` automatically.

## Repo layout

| Area | Files |
|---|---|
| App entry | [app.py](app.py) |
| DB layer | [db.py](db.py), [schema.sql](schema.sql) |
| Auth | [auth.py](auth.py), [helpers.py](helpers.py) |
| Grading pipeline | [grader.py](grader.py) (stateless), [interactions_routes.py](interactions_routes.py) |
| Rubrics | [rubrics_routes.py](rubrics_routes.py), [rubric_ai_routes.py](rubric_ai_routes.py), [rubric_templates.py](rubric_templates.py) |
| Per-location intel | [intel.py](intel.py) |
| VoIP ingestion | [voip/](voip/), [voip_routes.py](voip_routes.py) |
| Reporting | [dashboard_routes.py](dashboard_routes.py), [performance_reports.py](performance_reports.py), [export_routes.py](export_routes.py) |
| Admin | [platform_admin_routes.py](platform_admin_routes.py), [account_routes.py](account_routes.py), [settings_routes.py](settings_routes.py), [labels_routes.py](labels_routes.py) |
| Audit | [audit_log.py](audit_log.py), [audit_log_routes.py](audit_log_routes.py) |

Detailed schema reference: [ERD_SUMMARY.md](ERD_SUMMARY.md).

## Conventions

- Every column is prefixed with its table abbreviation (`irs_`, `cq_`, `acl_`, `pr_`, `ur_`, `rg_`, `ri_`, `au_`, `ak_`, `al_`, `cl_`, `li_`). Don't violate this.
- Tenant scope is **derived** (users → departments → companies; projects via campaigns/rubric_groups → locations). There is no `company_id` on `users`, `interactions`, `rubric_groups`, or `audit_log`. Always filter through the join path.
- Soft delete: `{prefix}_deleted_at IS NULL` on every list query. Never `DELETE FROM`.
- `grader.py` is **intentionally stateless** — no DB calls, no env reads beyond module init. Keep it that way.
- `_row_to_dict(row)` normalizes psycopg2 `RealDictRow` and sqlite3 `Row` into a plain dict. Use it everywhere row data crosses a function boundary.
- Status IDs are stable integers seeded in `schema.sql`. The codebase hard-codes them (e.g. `_STATUS_GRADED = 43`). If you change the seed, hunt down every constant.
