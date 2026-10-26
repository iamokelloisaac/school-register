# School Register

A learning-grade school register API built with FastAPI, raw PostgreSQL, and psycopg 3. Students register, browse the program catalog, and enroll in programs. Admins manage programs and users. Sessions are opaque tokens stored server-side, with sliding + absolute expiry.

The project is deliberately scoped small so that the *plumbing* — auth, sessions, migrations, layering, testing — gets the attention, not the domain rules. What's here is what a small, well-built service looks like end to end.

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Database](#database)
- [Running the App](#running-the-app)
- [API Overview](#api-overview)
- [Testing](#testing)
- [Project Layout](#project-layout)
- [Scripts](#scripts)
- [Conventions](#conventions)
- [Security Notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Features

- **Session-based authentication.** Opaque tokens, hashed server-side, sliding + absolute expiry, revocable on demand.
- **Cookie and Bearer transports.** Browsers get an HttpOnly session cookie; API clients use `Authorization: Bearer <token>`. Same session, same permissions.
- **Role-based authorization.** `student` and `admin`. Enforced via a layered dependency (`require_admin`), applied once per admin route.
- **Program catalog.** Admins create programs; students browse and enroll. Soft-delete (archive) and hard-delete both supported.
- **Consistent error contract.** One JSON shape for every failure, from validation to 500s. Machine-readable codes.
- **Structured logging.** Human-readable in dev, JSON in production. Request IDs on every log line and every response header.
- **Hand-rolled migrations.** Numbered `.sql` files, applied via `psql`. No ORM, no migration framework.
- **Full test coverage.** API, query-layer, and service tests against a real Postgres.

---

## Architecture

### Layered design

```
┌──────────────────────────────────────────────────────────────┐
│  HTTP      api/v1/routers/*.py      parse → call → serialize │
├──────────────────────────────────────────────────────────────┤
│  Service   services/*.py            orchestration, policy    │
├──────────────────────────────────────────────────────────────┤
│  Query     db/queries/*.py          raw SQL, no policy       │
├──────────────────────────────────────────────────────────────┤
│  Driver    psycopg 3 (async)        parameterized statements │
├──────────────────────────────────────────────────────────────┤
│  Database  PostgreSQL 17+           constraints, indexes     │
└──────────────────────────────────────────────────────────────┘
```

Each layer knows only about the layers below it. Routers never write SQL. Queries never raise domain exceptions. Services never touch HTTP. The dependency graph enforces this, and the test layout mirrors it.

### Cross-cutting concerns

- `core/config.py` — the only place that reads environment variables.
- `core/security.py` — hashing (Argon2id for passwords, SHA-256 for session tokens) and token generation.
- `core/deps.py` — the auth chain: token → session → user → role.
- `core/errors.py` — domain exceptions and the HTTP translation.
- `core/logging.py` — formatter selection, request-ID context var.
- `core/middleware.py` — request-ID assignment, access logging.

### Data model

Four tables. No more.

```
users          ─┬─  id, email (citext, unique), hashed_password,
                 │   full_name, age, role, is_active, timestamps
                 │
                 ├─ sessions       id, user_id → users, token_hash (unique),
                 │                 expires_at, absolute_expires_at,
                 │                 user_agent, ip_address
                 │
                 ├─ programs       id, name (unique), code (unique),
                 │                 description, is_active,
                 │                 created_by → users, timestamps
                 │
                 └─ enrollments    (student_id, program_id) PK,
                                   enrolled_at
```

Key invariants enforced by the database:

- `users.email` is `CITEXT` — case-insensitive uniqueness.
- `sessions.token_hash` is `UNIQUE` — indexed session lookup.
- `enrollments` has a composite PK — no duplicate enrollments.
- `enrollments.student_id` and `.program_id` cascade on delete.
- `programs.created_by` is `ON DELETE SET NULL` — programs outlive their creators.

---

## Requirements

- **Python** 3.11 or newer.
- **PostgreSQL** 15 or newer. 18 is what we developed against.
- **Docker** and **Docker Compose** — for the local Postgres container. Optional if you already have Postgres running.
- A Unix-like shell (Linux, macOS, WSL). Windows native shells work with minor command translation.

---

## Quick Start

From zero to a running API in seven steps:

```bash
# 1. Clone and enter
git clone https://github/iamokelloisaac/school-register.git school-register
cd school-register

# 2. Create and activate a virtualenv
python -m venv .venv
source .venv/bin/activate

# 3. Install the package and dev dependencies
pip install -e ".[dev]"

# 4. Configure environment
cp .env.example .env
#    Edit .env if your Postgres isn't on localhost:5432 with the
#    default credentials. Otherwise the defaults work.

# 5. Start Postgres and set up both databases
docker compose up -d postgres
for f in migrations/*.sql; do
  docker compose exec -T postgres psql -U postgres -d school_register < "$f"
done
./scripts/init_test_db.sh

# 6. Seed (creates an admin and a few sample programs)
python -m scripts.seed

# 7. Run
uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs> for the interactive API documentation.

**Admin credentials** (from the seed):

```
email:    admin@school.local
password: changeme123
```

Change the password immediately in any non-local environment. See [Security Notes](#security-notes).

---

## Configuration

All configuration is read from environment variables or a `.env` file at the project root. See `.env.example` for the full list.

| Variable | Default | Purpose |
|---|---|---|
| `ENV` | `dev` | `dev` or `prod`. Drives logging format and script guards. |
| `LOG_LEVEL` | `INFO` | Root logger level. |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/school_register` | Postgres connection string. |
| `DB_POOL_MIN` | `1` | Minimum pooled connections. |
| `DB_POOL_MAX` | `10` | Maximum pooled connections. |
| `SESSION_IDLE_TTL_SECONDS` | `1800` | Sliding idle window. |
| `SESSION_ABSOLUTE_TTL_SECONDS` | `2592000` | Hard cap on session lifetime. |
| `ARGON2_TIME_COST` | `3` | Passes over memory. |
| `ARGON2_MEMORY_COST` | `65536` | KiB of RAM per hash (64 MiB). |
| `ARGON2_PARALLELISM` | `1` | Lanes. Keep at 1 unless you have a specific reason. |

**Production tuning.** Raise `ARGON2_*` costs, set `ENV=prod` (which enables `Secure` cookies and JSON logging), and point `DATABASE_URL` at a managed instance with a strong password.

---

## Database

### Migrations

Four numbered `.sql` files in `migrations/`, applied in order:

```
001_init_users.sql       citext extension, user_role enum, users table
002_sessions.sql         sessions table with two expiry columns
003_programs.sql         programs table
004_enrollments.sql      enrollments join table with composite PK
```

Apply them manually:

```bash
for f in migrations/*.sql; do
  docker compose exec -T postgres psql -U postgres -d school_register < "$f"
done
```

Every migration uses `IF NOT EXISTS` and idempotent `DO` blocks — running a migration twice is a no-op. There is no tracking table and no runner; applying in order is the entire protocol.

### Reset

To drop the schema and re-apply everything:

```bash
python -m scripts.reset_db --yes
python -m scripts.seed            # optional — restore sample data
```

The script refuses to run unless `ENV=dev`. Without `--yes`, it performs a dry run and prints what it would do.

### Test database

The test suite uses a separate database, `school_register_test`, with the same schema. Create and migrate it with:

```bash
./scripts/init_test_db.sh
```

Re-running the script is safe — it skips creation if the database exists and relies on the migrations' idempotency for the rest.

---

## Running the App

**Development (auto-reload):**

```bash
uvicorn app.main:app --reload
```

**Production (multiple workers, no reload):**

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

**Check it's alive:**

```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/health/db
```

Both should return `{"status": "ok"}`. The `/health/db` variant also runs a `SELECT 1` through the pool, so it's the more meaningful check.

---

## API Overview

All endpoints are under `/api/v1`. Full documentation with request and response schemas is at `/docs` (Swagger UI) and `/redoc` (ReDoc).

### Health

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | None | Liveness. |
| `GET` | `/health/db` | None | Liveness + DB connectivity. |

### Authentication

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/auth/register` | None | Self-register a student. |
| `POST` | `/auth/login` | None | Start a session. |
| `POST` | `/auth/logout` | Session | End the current session. |
| `POST` | `/auth/logout-all` | Session | End every session for the user. |

### Current user

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/me` | Session | Current user's profile. |
| `PATCH` | `/me` | Session | Update own profile (partial). |
| `POST` | `/me/password` | Session | Change own password. |
| `GET` | `/me/programs` | Session | Programs the user is enrolled in. |

### Programs (student-facing)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/programs` | Session | List active programs. Paginated. |
| `GET` | `/programs/{id}` | Session | Program detail. |
| `POST` | `/programs/{id}/enroll` | Session | Enroll current user. |
| `DELETE` | `/programs/{id}/enroll` | Session | Unenroll current user. |

### Admin

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/admin/students` | Admin | List all users. Paginated. |
| `GET` | `/admin/students/{id}` | Admin | Get a user by id. |
| `POST` | `/admin/students` | Admin | Register a user on their behalf. |
| `POST` | `/admin/students/{id}/password` | Admin | Reset a user's password. |
| `GET` | `/admin/programs` | Admin | List all programs (incl. archived). |
| `POST` | `/admin/programs` | Admin | Create a program. |
| `DELETE` | `/admin/programs/{id}` | Admin | Hard-delete a program. |
| `POST` | `/admin/programs/{id}/archive` | Admin | Soft-delete a program. |
| `GET` | `/admin/programs/{id}/students` | Admin | List students in a program. |

### Error shape

Every error response, without exception:

```json
{
  "error": {
    "code": "email_taken",
    "message": "An account with that email already exists.",
    "details": null
  }
}
```

The `code` field is stable and machine-readable. Clients should switch on it, not on the message.

### Authentication transports

The same session works over two transports, chosen by the client:

**Cookie (browsers).** `POST /auth/login` sets a `session_token` cookie that's `HttpOnly`, `SameSite=Lax`, and `Secure` in production. The browser sends it automatically on subsequent requests. No client code required.

**Bearer (everything else).** `POST /auth/login` returns a `token` in the JSON body. Clients send it as `Authorization: Bearer <token>`. No cookies, no CSRF surface.

### Request IDs

Every response carries an `X-Request-ID` header. If the incoming request had one, it's preserved; otherwise a new UUID is generated. The same ID appears in every log line emitted during that request — quote it in bug reports.

---

## Testing

```bash
pytest                          # everything
pytest tests/api                # API layer only
pytest tests/db                 # query layer only
pytest tests/services           # service layer only
pytest -k auth                  # files/tests matching "auth"
pytest -v                       # verbose
pytest --tb=short               # shorter tracebacks
```

Tests run against `school_register_test`, not the dev database. Isolation is per-test via transaction rollback: each test opens a transaction, runs against it, and rolls back. Fast, complete, and safe to run repeatedly.

**Prerequisites.** The test database must exist and be migrated. See [Database](#database).

**Coverage map:**

| Layer | File | What it covers |
|---|---|---|
| API | `tests/api/test_health.py` | Liveness, DB connectivity. |
| API | `tests/api/test_auth.py` | Register, login, logout, expiry, anti-enumeration. |
| API | `tests/api/test_me.py` | Profile, partial update, password change, own enrollments. |
| API | `tests/api/test_programs.py` | Catalog, detail, enroll, leave, error paths. |
| API | `tests/api/test_admin.py` | Admin CRUD, authorization, cascade and preserve semantics. |
| DB | `tests/db/test_queries.py` | Raw SQL behavior, constraints, indexes. |
| Service | `tests/services/test_auth_service.py` | Domain rules, translations, identity boundaries. |

---

## Project Layout

```
school-register/
├── app/
│   ├── main.py                    FastAPI app, lifespan, wiring
│   ├── core/
│   │   ├── config.py              Settings (pydantic-settings)
│   │   ├── security.py            Hashing, token generation
│   │   ├── deps.py                get_db, get_current_user, require_admin
│   │   ├── errors.py              Domain exceptions + handlers
│   │   ├── logging.py             Formatters, request-ID context var
│   │   └── middleware.py          Request ID + access logging
│   ├── db/
│   │   ├── pool.py                AsyncConnectionPool
│   │   └── queries/               Raw SQL, one module per table
│   ├── schemas/                   Pydantic request/response shapes
│   ├── api/v1/
│   │   ├── api.py                 Router aggregation
│   │   └── routers/               One module per resource
│   ├── services/                  Business rules + orchestration
│   └── utils/                     Pagination helpers
├── migrations/                    Numbered .sql files
├── scripts/
│   ├── seed.py                    Bootstrap admin + sample programs
│   ├── reset_db.py                Drop schema and re-migrate
│   └── init_test_db.sh            Create + migrate test DB
├── tests/
│   ├── conftest.py                Fixtures, DB isolation
│   ├── api/                       Endpoint tests
│   ├── db/                        Query tests
│   └── services/                  Service tests
├── .env.example                   Configuration template
├── docker-compose.yml             Postgres for local development
├── pyproject.toml                 Package + tool config
└── README.md                      This file
```

---

## Scripts

| Script | Purpose | Command |
|---|---|---|
| `scripts/seed.py` | Create admin + sample programs. Idempotent. | `python -m scripts.seed` |
| `scripts/reset_db.py` | Drop schema, re-apply migrations. Dev only. | `python -m scripts.reset_db --yes` |
| `scripts/init_test_db.sh` | Create and migrate the test database. Idempotent. | `./scripts/init_test_db.sh` |

All scripts use `python -m` (module invocation) so that `from app...` imports resolve. Running `python scripts/foo.py` directly would fail.

---

## Conventions

These are the rules the codebase follows. They're documented here so that changes stay consistent.

**URLs.** Plural nouns (`/programs`, not `/getPrograms`). Versioned under `/api/v1`. Verb conveyed by HTTP method.

**Naming.** `snake_case` in Python and JSON. Field names in a response match the corresponding Pydantic model's attribute names exactly.

**Timestamps.** ISO 8601 with timezone, always UTC. `2026-10-03T12:34:56Z`.

**IDs.** `BIGSERIAL` — integers, monotonic, index-friendly.

**Errors.** Every failure returns the envelope shape above. Domain exceptions are raised in services and routers, never `HTTPException`.

**Layering.** Routers call services or queries. Services call queries. Queries call nothing but psycopg. Nothing imports upward.

**Logging.** `logger = logging.getLogger(__name__)` at module level. No `print`. No logging of secrets, tokens, or full connection strings.

**Tests.** Real Postgres, transaction rollback per test, no mocks. API tests go through `httpx.AsyncClient`. Service tests call service functions directly. Query tests call query functions directly.

---

## Security Notes

The threat model for this project is "learning-grade," not "handles PII at scale." A few choices reflect that, and are worth naming for anyone who wants to lift this pattern into something more serious.

**Passwords.** Hashed with Argon2id. Parameters are configurable and should be raised in production. Passwords are never logged, never returned in any response, and never included in any query's default column list.

**Session tokens.** 256 bits of randomness from the OS CSPRNG, hashed with SHA-256 before storage. A database leak does not expose live sessions. Tokens are transportable via HttpOnly cookie (browser) or `Authorization` header (API client).

**Session expiry.** Sliding idle window (default 30 minutes) plus an absolute cap (default 30 days). Both are checked on every request. Expired sessions are deleted opportunistically on the next read.

**Session revocation.** Logout deletes one session. Logout-all deletes every session for a user. Admin password reset deletes every session for the target. Self-service password change deletes every session *except* the current one. Revocation is immediate, not eventual.

**Anti-enumeration.** Login failures for wrong password, unknown email, and inactive account all produce the same 401 with the same message and the same code. A client cannot distinguish them.

**Authorization.** Enforced by a single dependency (`require_admin`) applied per route. Admin routes live under `/admin`, and the URL prefix matches the permission boundary — a bug that accidentally exposed an admin route under a student prefix would be visible in code review.

**CSRF.** Session cookies use `SameSite=Lax`, which blocks cross-site POST requests from carrying the cookie. This is sufficient for our current surface. If we added cookie-based state-changing GET routes (we won't), we'd need a CSRF token.

**Not covered.** Rate limiting, account lockout after failed attempts, email verification, password strength beyond length bounds, MFA. Each is a real concern for a production system and each is a conscious omission here.

**Admin password.** The seed creates `admin@school.local` with `changeme123`. **Never run the seed against a database that matters.** The reset script refuses non-dev environments; the seed does not, so exercise care.

---

## Troubleshooting

**`database "school_register_test" does not exist`**

Run `./scripts/init_test_db.sh`. The test suite expects the test database to exist and be migrated before it runs.

**`permission denied while trying to connect to the Docker daemon`**

Add your user to the `docker` group, then log out and back in:

```bash
sudo usermod -aG docker $USER
# log out and back in — group membership only refreshes on login
```

`newgrp docker` refreshes the current shell but doesn't propagate to already-running processes (like an IDE). A full logout is the reliable fix.

**`pip install -e ".[dev]"` fails with "Multiple top-level packages"**

setuptools is refusing to guess which package is the real one. Ensure `pyproject.toml` contains:

```toml
[tool.setuptools.packages.find]
include = ["app*"]
exclude = ["tests*", "migrations*", "scripts*"]
```

**`psycopg.OperationalError: connection refused`**

Postgres isn't running. `docker compose up -d postgres` and verify with `docker compose ps` that the container is `Up` and `healthy`.

**Login fails with `422 Unprocessable Entity`**

The email failed Pydantic's `EmailStr` validation. Common cause: reserved TLDs like `.local`, `.test`, `.example`, `.invalid`, `.localhost` are rejected by RFC 6761. Use a normal-looking address in test data, e.g. `user@example.com`.

**`SQLSTATE 25P02: current transaction is aborted`**

A previous statement in the same transaction failed, and Postgres refuses to continue. Common in tests that intentionally trigger a constraint violation and then try another statement in the same transaction. Solution: split the two failures into two tests, or wrap the failing statement in a savepoint:

```python
async with conn.transaction():
    ...  # failing statement
```

**Logs are duplicated**

Uvicorn installs its own handlers. Our `configure_logging` detaches them and lets records propagate to the root logger. If you see duplication, check that nothing else is calling `logging.basicConfig`.

**`X-Request-ID` is missing from a response**

The middleware runs for all requests that reach the ASGI app. A missing header usually means the request short-circuited before middleware — check for a proxy or gateway in front.

---

## License

This project is provided as-is for learning. No warranty, no support. Use it however you like — as a reference, as a starting point for something larger, or as a study guide for the patterns it demonstrates.