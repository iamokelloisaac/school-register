#!/usr/bin/env bash
# =============================================================================
# School Register — full commit history in dependency order
# =============================================================================
#
# Paste this entire file into a terminal, or save it as `commit-all.sh` and
# run `bash commit-all.sh` from the project root.
#
# Assumes:
#   * You are on branch `main`.
#   * All files listed below already exist in the working tree with their
#     final content.
#   * The working tree is otherwise clean (no other staged changes).
#
# Each commit is one file. Messages follow Conventional Commits.
# =============================================================================

set -e

# -----------------------------------------------------------------------------
# Tier 0 — Package scaffolding (12 commits)
# -----------------------------------------------------------------------------

git add app/__init__.py
git commit -F- <<'EOF'
chore(pkg): add app package marker

Empty `__init__.py` to make `app/` an importable Python package. Has
no content of its own; exists so that `import app` works and so that
submodules like `app.core`, `app.db`, and `app.api` can be imported.
EOF

git add app/core/__init__.py
git commit -F- <<'EOF'
chore(pkg): add core package marker

Empty `__init__.py` for the `app.core` package, which will hold
cross-cutting concerns (config, security, errors, deps, logging,
middleware).
EOF

git add app/db/__init__.py
git commit -F- <<'EOF'
chore(pkg): add db package marker

Empty `__init__.py` for the `app.db` package. Will contain the
connection pool and the raw-SQL query modules.
EOF

git add app/db/queries/__init__.py
git commit -F- <<'EOF'
chore(pkg): add queries package marker

Empty `__init__.py` for the `app.db.queries` package, which will hold
one raw-SQL module per database table.
EOF

git add app/schemas/__init__.py
git commit -F- <<'EOF'
chore(pkg): add schemas package marker

Empty `__init__.py` for the `app.schemas` package, which will hold the
Pydantic request and response models.
EOF

git add app/api/__init__.py
git commit -F- <<'EOF'
chore(pkg): add api package marker

Empty `__init__.py` for the `app.api` package. Nesting here is
deliberate: the API is versioned, so `app.api` is the parent of
`app.api.v1`.
EOF

git add app/api/v1/__init__.py
git commit -F- <<'EOF'
chore(pkg): add api.v1 package marker

Empty `__init__.py` for the versioned API package. Versioning at the
package level (rather than in strings only) means a future v2 can live
alongside v1 without collisions.
EOF

git add app/api/v1/routers/__init__.py
git commit -F- <<'EOF'
chore(pkg): add routers package marker

Empty `__init__.py` for the `app.api.v1.routers` package, which will
hold one router module per resource (health, auth, me, programs,
admin).
EOF

git add app/services/__init__.py
git commit -F- <<'EOF'
chore(pkg): add services package marker

Empty `__init__.py` for the `app.services` package, which will hold
the business-rule and orchestration layer between routers and queries.
EOF

git add app/utils/__init__.py
git commit -F- <<'EOF'
chore(pkg): add utils package marker

Empty `__init__.py` for the `app.utils` package. Will hold small,
generic helpers that don't fit any single domain.
EOF

git add scripts/__init__.py
git commit -F- <<'EOF'
chore(pkg): add scripts package marker

Empty `__init__.py` so that `python -m scripts.seed` and similar
module invocations work. Running scripts as modules (rather than
paths) is what makes `from app...` imports resolve from the project
root.
EOF

git add tests/__init__.py
git commit -F- <<'EOF'
chore(pkg): add tests package marker

Empty `__init__.py` for the `tests` package. Makes test modules
discoverable and importable under a stable package path.
EOF

# -----------------------------------------------------------------------------
# Tier 1 — Schema (4 commits)
# -----------------------------------------------------------------------------

git add migrations/001_init_users.sql
git commit -F- <<'EOF'
feat(db): add users table migration

Introduces the `users` table and the `user_role` enum, plus the
CITEXT extension for case-insensitive email uniqueness.

Three choices worth noting for anyone reading this later:

  * CITEXT on `email` rather than TEXT — email is an identifier, and
    `Alice@x.com` and `alice@x.com` must not be two accounts.
  * BIGSERIAL ids — monotonic, index-friendly, human-readable in logs
    and URLs.
  * A CHECK constraint on `age` — the DB is the last line of defense
    even if application validation is bypassed.

The `DO $$ ... $$` block around CREATE TYPE exists because Postgres
has no `CREATE TYPE IF NOT EXISTS`. Every migration in this project is
idempotent so that re-applying is a no-op.
EOF

git add migrations/002_sessions.sql
git commit -F- <<'EOF'
feat(db): add sessions table migration

Opaque session tokens, stored server-side. The raw token is never
persisted — only its SHA-256 digest — so a database leak does not hand
an attacker live sessions.

Two expiry columns:

  * `expires_at` — sliding idle window, bumped on every request.
  * `absolute_expires_at` — hard cap, never moves.

Both are checked on every authenticated request. `user_id` cascades on
delete so orphaned sessions can't exist. Three indexes support the hot
paths: lookup by token, revocation by user, and cleanup by expiry.
EOF

git add migrations/003_programs.sql
git commit -F- <<'EOF'
feat(db): add programs table migration

The catalog of programs students can enroll in. Two unique columns —
`name` (human-readable) and `code` (short, stable) — because programs
are referenced by both in practice.

`created_by` uses ON DELETE SET NULL, not CASCADE. This is the design
decision worth understanding: sessions cascade because they belong to
a user, but programs belong to the school and outlive their creator.
Deleting an admin must not delete their programs.

`is_active` provides soft delete. Hard delete and archive are both
supported; the admin endpoints pick.
EOF

git add migrations/004_enrollments.sql
git commit -F- <<'EOF'
feat(db): add enrollments table migration

The join table linking students to programs. Composite primary key on
`(student_id, program_id)` — no surrogate id, because the pair *is*
the identity and duplicates must be impossible.

Both foreign keys cascade on delete. That's the correct semantic for a
join table: an enrollment only makes sense if both sides exist.

Two indexes on the FK columns. The composite PK covers lookups by
student; the second index reverses the priority for lookups by
program. This is a real lesson about composite keys — column order
determines which queries the PK index can serve.
EOF

# -----------------------------------------------------------------------------
# Tier 2 — Config and error vocabulary (2 commits)
# -----------------------------------------------------------------------------

git add app/core/config.py
git commit -F- <<'EOF'
feat(config): add settings loader

The single place in the codebase that reads environment variables.
Every other module imports `settings` from here, so "where does
DATABASE_URL come from?" has exactly one answer.

Uses pydantic-settings for three things:

  * Type coercion and validation, so a malformed value fails at
    startup, not at first use.
  * Sensible defaults declared in Python, so a fresh clone runs
    without a `.env`.
  * `.env` file support for local development.

The `Settings` object is cached with `lru_cache` — read once, at first
import, then reused. That's why test setup must set environment
variables before importing anything that touches settings.
EOF

git add app/core/errors.py
git commit -F- <<'EOF'
feat(errors): add domain exceptions and HTTP handlers

The shared vocabulary for failure. Every layer that needs to signal
"something went wrong" raises one of the domain exceptions defined
here; the handlers at the bottom translate them into a single JSON
shape.

The split matters. Domain exceptions (`NotFound`, `Forbidden`,
`Conflict`, `Unauthorized`) describe *what went wrong* in the
application's language and know nothing about HTTP. The handlers
decide the status code. Services raise; handlers translate; routers
never see a status code.

The error JSON shape is part of the public API contract:

    {"error": {"code": "...", "message": "...", "details": ...}}

`code` is stable and machine-readable. Clients switch on it, not on
the message. Four handlers are registered:

  * AppError — our domain exceptions, mapped to status codes via a
    single lookup table.
  * StarletteHTTPException — framework-raised 404s, 405s, and so on,
    normalized into our shape.
  * RequestValidationError — Pydantic's 422s, with `loc` flattened
    into a dotted field path.
  * Exception — the catch-all. Logs the traceback server-side and
    returns a generic message. Never leaks internals to the client.
EOF

# -----------------------------------------------------------------------------
# Tier 3 — Core primitives (3 commits)
# -----------------------------------------------------------------------------

git add app/core/security.py
git commit -F- <<'EOF'
feat(security): add password and token primitives

Two hashing concerns, deliberately separated because they are not the
same problem:

  * Passwords — low-entropy, human-chosen. Argon2id, slow and
    memory-hard, so offline guessing is expensive.
  * Session tokens — high-entropy, server-generated. SHA-256,
    deterministic and fast, because the security comes from the
    token's randomness, not from the cost of hashing it.

Using Argon2 for session tokens would be wrong on two counts: it would
add ~50ms to every authenticated request, and because Argon2 salts
each hash, the lookup could not use an indexed equality match. SHA-256
makes `WHERE token_hash = $1` possible.

Functions are pure — strings in, strings or bools out. No I/O, no
async, no FastAPI. That purity is why they can be tested exhaustively
and cheaply.
EOF

git add app/core/logging.py
git commit -F- <<'EOF'
feat(logging): add structured logging configuration

Called once from `main.py`'s lifespan. After that, every module uses
`logging.getLogger(__name__)` and never reconfigures.

Two formatters:

  * Dev — human-readable lines, UTC timestamps, request ID in brackets.
  * JSON — one object per line, for log aggregators.

Selection is by `settings.ENV`. Production gets JSON; everything else
gets the human-readable form.

A `ContextVar` carries the request ID across async tasks. The
middleware sets it at request start; the formatter reads it when
emitting a log record. That's what lets every log line from one
request be correlated without threading the ID through function
arguments.

Uvicorn's own handlers are detached so its logs flow through our
formatter too — otherwise you'd see two formats in the same output
stream.
EOF

git add app/db/pool.py
git commit -F- <<'EOF'
feat(db): add async connection pool

A single `AsyncConnectionPool` opened on app startup and closed on
shutdown. `row_factory=dict_row` means every query returns dicts
rather than tuples, which makes the query layer's contract consistent
and readable.

Two rules the pool enforces implicitly, worth naming:

  * Never hold a connection across a request boundary. The pool
    reuses connections; using one after it's been returned is a bug
    that shows up as "my query ran against the wrong request's data."
  * Never grab two connections in one request. Pass the connection
    down.

Pool sizing comes from settings. `min_size` keeps a few warm
connections ready; `max_size` caps concurrency at the database layer.
EOF

# -----------------------------------------------------------------------------
# Tier 4 — Pydantic schemas (5 commits)
# -----------------------------------------------------------------------------

git add app/schemas/common.py
git commit -F- <<'EOF'
feat(schemas): add shared envelope models

Two shapes that every endpoint reuses:

  * `ErrorResponse` — the outer wrapper for every failure. Always
    present on error, always absent on success.
  * `Page[T]` — the pagination envelope. A list endpoint never returns
    a bare JSON array; it returns an object with `items`, `total`,
    `limit`, and `offset`. That way new fields can be added later
    without breaking clients, and the shape is self-describing.

`Pagination` is a Pydantic model used as a FastAPI dependency, so
`limit` and `offset` are parsed and bounded by the schema, not written
inline on every route. Bounds: `limit` 1..100, `offset` >= 0.
EOF

git add app/schemas/user.py
git commit -F- <<'EOF'
feat(schemas): add user request and response models

`UserRead` is the public shape of a user. Note what is *absent*:
`hashed_password`. Because the field simply doesn't exist on the
schema, it's impossible to accidentally serialize the hash — the
safety comes from the shape, not from remembering to exclude it.

`UserRole` is a `str`-based Enum. The `str` inheritance matters: it
lets the enum accept `"student"` on input, serialize as `"student"` on
output, and still give the codebase typo-safe comparisons and
type-checker support.

`UserUpdate` is the PATCH shape — every field optional, and only the
fields present in the request are updated. `age`, `email`, `role`, and
`is_active` are deliberately not updatable through this schema; those
are privileged operations.
EOF

git add app/schemas/auth.py
git commit -F- <<'EOF'
feat(schemas): add authentication request and response models

Three request shapes and two response shapes for the auth flow.

`RegisterRequest` has no `role` field. That is deliberate: every
self-registered account is a student. Admin promotion is a separate,
admin-only operation, so the client cannot choose its own privilege
level — not at the schema level, and not at the service level either.

`LoginRequest` uses plain `str` for the email, not `EmailStr`, and the
docstring explains why: a malformed email should fail identically to
an unknown one, so an attacker can't distinguish "not an email" from
"no such account" by comparing response codes.

Password bounds (8..128) are intentionally mild on the lower end
(NIST-style: length over character-class complexity) and cap the upper
end to bound the work an unauthenticated client can force.

`LoginResponse` returns both a `token` and the user. The token serves
Bearer clients; the login route also sets a cookie for browsers. Same
session, two transports.
EOF

git add app/schemas/program.py
git commit -F- <<'EOF'
feat(schemas): add program request and response models

`ProgramRead` is the public shape of a program, matching the columns
selected in the query layer's `_PROGRAM_COLUMNS` string.

`ProgramCreate` is what an admin sends when creating one. It has no
`is_active` field — new programs are always active; archiving is a
separate operation. It also has no `created_by` field — that's set
from the authenticated admin's session, not from the request body, so
it cannot be spoofed.

There is no `ProgramUpdate`. Programs are created and deleted as whole
entities; edit is a feature we haven't decided to support, and adding
a schema without an endpoint that uses it would be speculative.
EOF

git add app/schemas/enrollment.py
git commit -F- <<'EOF'
feat(schemas): add enrollment response models

Two shapes, two purposes:

  * `EnrollmentRead` — the raw join row. Useful for confirming a link
    and reading the enrollment timestamp.
  * `EnrolledProgram` — a *flattened* view: the program's details plus
    the enrollment timestamp. This is what `/me/programs` actually
    returns, because the UI needs the program's name and code, not
    just its id.

The two coexist on purpose. The raw shape is honest about the table;
the joined shape is what clients need. Neither is a subset of the
other, and forcing one endpoint to return the other's shape would
either leak internal fields or force an extra query.

`EnrolledProgram` deliberately omits `created_by`, `updated_at`, and
`is_active` — those are not relevant to a student viewing their own
enrollments.
EOF

# -----------------------------------------------------------------------------
# Tier 5 — Middleware (1 commit)
# -----------------------------------------------------------------------------

git add app/core/middleware.py
git commit -F- <<'EOF'
feat(middleware): add request ID and access logging

Two cross-cutting concerns that apply to *every* request:

  * Request ID — read from the incoming `X-Request-ID` header if
    present (propagating an upstream ID), otherwise generated as a
    fresh UUID4. Stored in the same ContextVar the logging formatter
    reads, and echoed back in the response's `X-Request-ID` header.
  * Access logging — one line per request with method, path, status,
    and duration.

The ID is set *before* the route handler runs, so every log record
emitted downstream — by services, queries, anything — carries it
automatically. A client can quote the ID from the response header, and
a support engineer can grep the logs for it.

Status-based log level: 4xx logs at INFO (client errors are normal
traffic), 5xx logs at ERROR (server errors are ours). This is what
makes error-rate metrics meaningful.

Exceptions are logged and re-raised, never swallowed.
EOF

# -----------------------------------------------------------------------------
# Tier 6 — Query layer (4 commits)
# -----------------------------------------------------------------------------

git add app/db/queries/users.py
git commit -F- <<'EOF'
feat(db): add user queries

Raw SQL for the `users` table. Six functions: create, look up by email
(with hash), look up by id (without hash), list with pagination,
update password, set active.

Three conventions worth reading:

  * Column names are spelled out in `_USER_COLUMNS`. No `SELECT *`.
    Explicit projections mean schema changes are opt-in and there's
    one place to change if the shape of a "user" changes.
  * `hashed_password` is excluded from the default read path. Only
    `get_user_by_email` returns it, because only the login flow needs
    it. Defense in depth — even if a response schema forgot to strip
    the hash, the query never fetched it.
  * No exception handling. `UniqueViolation` from a duplicate email
    bubbles up untouched. The service layer decides what it means.

`RETURNING` on inserts avoids a second round trip. `ORDER BY id` on
list queries gives a stable pagination order.
EOF

git add app/db/queries/sessions.py
git commit -F- <<'EOF'
feat(db): add session queries

Raw SQL for the `sessions` table. Six functions covering the full
session lifecycle: create, look up by token hash, touch (sliding
expiry), delete one, delete all for a user, delete all-but-one for a
user, and clean up expired rows.

The lookup by token hash is the hot path — it runs on every
authenticated request — so it stays minimal: one indexed equality
match, no joins, no expiry filter. Expiry is checked by the caller
(`deps.get_current_session`), which preserves the distinction between
"no such session" and "expired session" for logging purposes.

`delete_other_sessions_for_user` exists as a separate function from
`delete_all_sessions_for_user` because password change and suspension
have different semantics: change preserves the current session,
suspension kills every session. Different names prevent the wrong one
being called by accident.
EOF

git add app/db/queries/programs.py
git commit -F- <<'EOF'
feat(db): add program queries

Raw SQL for the `programs` table. Create, get by id, get by code, list
with pagination and an `active_only` filter, hard delete, and soft
delete (deactivate).

`active_only` is a parameter, not a hardcoded WHERE clause. The public
catalog passes `True`; admin listings pass `False`. Same query, one
filter, no drift between the two.

The page query and the count query share the same WHERE clause via a
local variable. If they diverged, `total` would include rows the page
query excluded, and clients' pagination math would be wrong.

`delete_program` returns a boolean (did we remove a row?) rather than
being void, because the service layer needs to distinguish "deleted"
from "already gone" to produce 204 vs 404.
EOF

git add app/db/queries/enrollments.py
git commit -F- <<'EOF'
feat(db): add enrollment queries

Raw SQL for the `enrollments` join table plus the two joined views the
API needs.

Link management: `enroll` (insert, lets UniqueViolation bubble),
`leave` (delete, returns a bool), `is_enrolled` (fast EXISTS check).

Joined reads:
  * `list_enrolled_programs` — for `/me/programs`. Joins enrollments
    to programs, flattened, ordered by enrolled_at descending.
  * `list_students_in_program` — the reverse, for admin rosters.

`list_enrolled_programs` deliberately does *not* filter by
`programs.is_active`. A student's enrollment history is theirs; if a
program gets archived, the enrollment stays visible in their list. The
public catalog filters; the student's list does not.

All identifiers are qualified (`programs.name`, not `name`) so the
join's provenance is readable at a glance.
EOF

# -----------------------------------------------------------------------------
# Tier 7 — Utils (1 commit)
# -----------------------------------------------------------------------------

git add app/utils/pagination.py
git commit -F- <<'EOF'
feat(utils): add pagination helpers

Two small functions that every list endpoint reuses:

  * `clamp_pagination` — bounds limit and offset for internal callers
    (scripts, tests) that bypass FastAPI's query-param validation. The
    schema enforces bounds for API clients; this enforces them for
    everyone else.
  * `build_page` — wraps items, total, and the parsed Pagination into
    the `Page[T]` envelope.

`build_page` echoes back the *requested* limit and offset, not any
transformed value. If the client asked for `?limit=25`, the response
says `"limit": 25`, and the client's pagination math stays correct.

Bounds are declared as module constants, mirroring the schema's. They
must agree; changing one means changing the other.
EOF

# -----------------------------------------------------------------------------
# Tier 8 — Dependencies (1 commit)
# -----------------------------------------------------------------------------

git add app/core/deps.py
git commit -F- <<'EOF'
feat(deps): add the auth dependency chain

The wiring layer between requests and everything else. FastAPI
dependencies compose: `require_admin` depends on `get_current_user`,
which depends on `get_current_session`, which depends on `get_db`.
FastAPI resolves the whole chain, in order, per request.

Four dependencies and their jobs:

  * `get_db` — yields a connection from the pool for the request's
    lifetime. Because psycopg 3's connection context manager commits
    on clean exit and rolls back on exception, this is *also* where
    the transaction boundary lives. Services don't manage
    transactions; only this one dependency decides.
  * `_extract_token` — pulls the token from `Authorization: Bearer` or
    the `session_token` cookie. Header wins when both are present.
  * `get_current_session` — hashes the token, looks up the row, checks
    both expiries, and bumps the sliding window. Expired sessions are
    deleted opportunistically.
  * `get_current_user` — loads the user behind the session and rejects
    inactive accounts with the same generic 401 as an invalid session,
    so a suspended user can't be distinguished from a nonexistent one.
  * `require_admin` — the authorization half. Returns the user so that
    a route needing admin gets it in one dependency, not two.

Four `Annotated` type aliases at the bottom (`DbConn`, `CurrentUser`,
`CurrentSession`, `AdminUser`) are what every route signature uses.
Reading a router tells you exactly what it needs from the parameter
types.
EOF

# -----------------------------------------------------------------------------
# Tier 9 — Services (3 commits)
# -----------------------------------------------------------------------------

git add app/services/auth_service.py
git commit -F- <<'EOF'
feat(services): add auth service

Orchestration for register, login, logout, and logout-all. This is
where low-level errors are translated into domain terms.

`register` hashes the password, inserts the user, and translates
`UniqueViolation` into `Conflict("email_taken")`. It doesn't pre-check
"does this email exist?" — that would be a race condition. The DB's
unique index is the only reliable arbiter, so we catch the constraint
instead.

`login` fetches the user by email (which includes the hash), verifies
the password, and creates a session. Every failure — no user, wrong
password, inactive account — raises the *same* `Unauthorized` with the
same code and message. That's the anti-enumeration guarantee: a client
cannot distinguish "no account" from "wrong password."

The password hash is fetched for verification and stripped before
return. The caller never sees it.

Every auth event (success and failure) is logged at INFO with the
relevant identifiers. Never the password, never the raw token.
EOF

git add app/services/enrollment_service.py
git commit -F- <<'EOF'
feat(services): add enrollment service

Enroll and leave, plus the business rules that don't belong in the
query layer.

Rules made here (each documented in the function's docstring):

  * A student can enroll in any *active* program.
  * Enrolling twice is a `Conflict` — the client asked "am I in?" and
    the honest answer is "you already were."
  * Enrolling in an archived program is a `Conflict`. Archiving is the
    admin's way of saying "stop new students."
  * Leaving is always allowed.
  * Leaving a program you weren't in is a `NotFound`. Stricter than
    idempotent delete; the client targeted something that isn't there.

`enroll` checks the program exists and is active before inserting, but
*not* to prevent a race — the FK handles that. It checks because
"archived" and "nonexistent" need different error codes, and there's
no DB constraint for "don't allow enrollment into inactive programs."
The check is for the archived case.

`list_my_programs` is a pass-through. That's deliberate: routes always
go through the service, even for trivial reads, so a future rule
change has one obvious home.
EOF

git add app/services/program_service.py
git commit -F- <<'EOF'
feat(services): add program service

A small service that exists for one reason: translating
`UniqueViolation` on program creation into a specific domain error.

`programs` has two unique constraints — one on `name`, one on `code`.
An insert can violate either, and the client deserves to know which.
The service inspects `exc.diag.constraint_name` (Postgres populates
this) and maps:

  * `programs_code_key` → `Conflict("program_code_taken")`
  * `programs_name_key` → `Conflict("program_name_taken")`
  * anything else      → `Conflict("program_duplicate")` (fallback)

Without this translation, a duplicate program would surface as a raw
`UniqueViolation` and the error handler would return a 500. That's the
bug this function prevents.

The file is short. It will grow if we add edit, unarchive, or
bulk-import operations. For now, one function.
EOF

# -----------------------------------------------------------------------------
# Tier 10 — Routers (5 commits)
# -----------------------------------------------------------------------------

git add app/api/v1/routers/health.py
git commit -F- <<'EOF'
feat(api): add health endpoints

Two endpoints that prove the app is running:

  * `GET /health` — liveness. Returns `{"status": "ok"}` without
    touching the database.
  * `GET /health/db` — runs `SELECT 1` through the pool. This is the
    meaningful one: it proves the pool is open and Postgres is
    reachable, not just that the process is up.

Both are unauthenticated. Health checks that require auth are useless
to load balancers and container orchestrators.

Uses the `DbConn` alias from `core/deps.py` — the same dependency
every other route uses for a connection.
EOF

git add app/api/v1/routers/auth.py
git commit -F- <<'EOF'
feat(api): add auth endpoints

Register, login, logout, and logout-all. The first router that
exercises the whole stack: schemas → service → queries → Postgres, with
security and error handling threaded through.

`POST /auth/login` does two things on success:

  * Returns the raw token in the JSON body, for API clients that use
    `Authorization: Bearer`.
  * Sets an `HttpOnly`, `SameSite=Lax` cookie with the same token, for
    browsers. `Secure` is enabled only when `ENV != "dev"`, because
    local dev runs on plain http and browsers reject Secure cookies
    over http.

Same session in the database, two transports. The client picks.

`POST /auth/logout` requires authentication and returns 204. Logging
out twice fails the second time — a known consequence of requiring
auth on this route, documented in the docstring.

`POST /auth/logout-all` revokes every session for the user, including
the current one. This is the operation that makes sessions worth more
than JWTs.

The session cookie name is a module constant (`SESSION_COOKIE_NAME`)
because it's coupled by string to a parameter in `deps._extract_token`.
Renaming one means renaming the other.
EOF

git add app/api/v1/routers/me.py
git commit -F- <<'EOF'
feat(api): add current-user endpoints

Four endpoints, all operating on the authenticated user's own data:
`GET /me`, `PATCH /me`, `POST /me/password`, `GET /me/programs`.

There is no path parameter for "which user" anywhere in this file. The
target is always `user["id"]`, so a bug in authorization cannot leak
across users — there's nothing to leak *to*.

The PATCH handler demonstrates partial-update semantics. It builds the
UPDATE dynamically from only the fields present in the request body
(`exclude_unset=True`), against a hardcoded allowlist of updatable
columns. Column names come from the allowlist, not from user input;
values travel through parameterized placeholders.

`POST /me/password` requires the current password, verifies it, stores
the new hash, and revokes every *other* session for the user. The
current session survives — logging out of the tab you just changed
your password in would be hostile.

The password change body (`PasswordChangeRequest`) is defined inline
in this file, next to its only consumer. If a second consumer ever
appears, it moves to `schemas/`.
EOF

git add app/api/v1/routers/programs.py
git commit -F- <<'EOF'
feat(api): add program catalog endpoints

The student-facing program routes: list, detail, enroll, leave.

`GET /programs` filters to active programs only and returns a
paginated envelope. `GET /programs/{id}` returns archived programs
too — the catalog hides them, but a detail fetch doesn't. Two
different audiences, two different rules, both correct.

Enroll and leave operate on `user["id"]`, always. There is no path
parameter to enroll someone else. If admin-initiated enrollment is
ever wanted, it belongs in a separate `/admin/...` route with its own
authorization check, not as a modification of this one.

The identity rule is written into the docstrings because it's the kind
of thing a future contributor might reasonably ask about, and the
answer is a design decision, not an oversight.

Enrollment errors come straight from the service layer as domain
exceptions: 404 for missing program, 409 for already-enrolled or
archived, 401 for no session. The router raises nothing.
EOF

git add app/api/v1/routers/admin.py
git commit -F- <<'EOF'
feat(api): add admin endpoints

Nine admin-only routes: user listing and detail, admin-side student
creation, password reset, program listing (including archived),
program creation, hard delete, archive, and program roster.

Every route carries `AdminUser` in its signature. That dependency
(`require_admin`) runs the full auth chain and checks the role; a
non-admin gets 403 before the handler body executes. The URL prefix
(`/admin`) matches the permission boundary — scanning the codebase,
"is this admin-only?" is answered by "does the path start with
/admin?"

Notes on specific routes:

  * `GET /admin/students` returns *all* users, not just students, so
    the admin sees other admins too.
  * `POST /admin/students` reuses `auth_service.register`, so the
    "create a user" operation has one implementation, not two.
  * `POST /admin/students/{id}/password` revokes every session for the
    target user. Without this, an admin reset wouldn't actually take
    effect until the target's existing sessions expired.
  * `DELETE /admin/programs/{id}` cascades to enrollments — destructive,
    and documented as such.
  * `POST /admin/programs/{id}/archive` is the soft-delete counterpart.
    Enrollments survive.
  * `GET /admin/programs/{id}/students` returns a bare list of dicts.
    No schema yet; if this endpoint earns its keep, it gets a proper
    one.

The `create_program` handler calls `program_service.create_program`,
which translates unique-constraint violations into a specific 409.
EOF

# -----------------------------------------------------------------------------
# Tier 11 — Aggregation and entry point (2 commits)
# -----------------------------------------------------------------------------

git add app/api/v1/api.py
git commit -F- <<'EOF'
feat(api): aggregate v1 routers

Mounts every router under a single `APIRouter`. `main.py` includes this
one router with the `/api/v1` prefix.

Ordering note in the docstring: FastAPI matches routes in registration
order, so specific routes must precede parameterized ones when their
paths could overlap. We sidestepped the classic `/programs/mine` vs
`/programs/{id}` collision by putting "my programs" under
`/me/programs`, but the rule still applies within individual routers.

All imports use the `app.` prefix. Bare imports like `from api.v1...`
would only work if `app/` itself were on `sys.path`, which it isn't —
that's a bug we hit once and don't want to hit again.
EOF

git add app/main.py
git commit -F- <<'EOF'
feat(app): add application entry point

The file that ties everything together. Creates the FastAPI instance,
registers middleware and exception handlers, and mounts the API
router.

The lifespan context manager defines startup and shutdown order:

  1. Configure logging — everything after it wants our log format.
  2. Open the connection pool — the first request shouldn't pay that
     cost.
  3. Yield — the app runs.

Shutdown is the reverse: close the pool. The `try/finally` guarantees
it runs even if the app crashes mid-request.

Middleware is added before exception handlers. Only one middleware
for now (request ID and access logging), but the order will matter
when we add more — request logging goes outermost so it sees every
request, including ones later middleware rejects.

Uses `lifespan` rather than the deprecated `@app.on_event` decorators.
Same behavior, cleaner structure, and teardown runs even if startup
raises.

The root `/` endpoint returns `{"app": ..., "env": ...}` — a trivial
"which environment am I pointed at?" check.
EOF

# -----------------------------------------------------------------------------
# Tier 12 — Scripts (3 commits)
# -----------------------------------------------------------------------------

git add scripts/seed.py
git commit -F- <<'EOF'
chore(scripts): add database seed script

Bootstrap for a fresh database. Creates one admin user and a handful
of sample programs. Idempotent — re-running is a no-op.

The script exists because of a chicken-and-egg problem: the
registration endpoint always creates `role="student"` (deliberately,
so clients can't self-promote), which means admin accounts cannot be
created through the API at all. A script inserting directly is the
honest way to break the deadlock.

Usage: `python -m scripts.seed` from the project root. The `-m` form
matters — running `python scripts/seed.py` directly would fail on the
`from app...` imports, because `sys.path[0]` would be `scripts/`
instead of the project root.

The admin's password is hardcoded for convenience and obviously bad
(`changeme123`), with a docstring note explaining that this is
deliberately not a production pattern. The full connection URL is
never logged — only host and database name.

Exits non-zero on failure, so a CI job or Makefile target sees the
failure rather than a false success.
EOF

git add scripts/reset_db.py
git commit -F- <<'EOF'
chore(scripts): add database reset script

Drops every table, type, and extension we own, then re-applies all
migrations in order. The result is a schema identical to a fresh
`psql` run.

Two safety guardrails:

  * Refuses to run when `ENV != "dev"`. The check is on the setting,
    not on the database name, so a production deployment with
    `ENV=prod` is rejected even if pointed at the right database.
  * Requires `--yes` to actually perform the reset. Without it, prints
    what it would do and exits 0. The safe path is the default; typing
    the flag is a small ceremony against muscle-memory mistakes.

The migrations directory is located *before* the drop, not after. If
the migration files were missing, we'd otherwise leave the database
empty with nothing to rebuild it. Order matters when one step
destroys data — verify the things you'll need *after* a destructive
step, before it runs.

Drop statements run individually (with `CASCADE` on tables,
`IF EXISTS` everywhere) so a failure is easy to locate, and so the
script is safe to run against a partially-migrated database.
EOF

git add scripts/init_test_db.sh
git commit -F- <<'EOF'
chore(scripts): add test database bootstrap

Creates the test database (if missing) and applies all migrations to
it. Idempotent and safe to run repeatedly.

The test suite uses a separate database — `school_register_test` — so
tests never touch dev data. This script is the one-time setup: create
the database, then run every `.sql` file from `migrations/` against
it.

Uses `set -euo pipefail` — the shell's strict mode. `-e` exits on any
failed command, `-u` errors on undefined variables, `-o pipefail`
makes a pipeline fail if any stage fails, not just the last. This is
the correct default for any script that matters.

`docker exec -i` (not `-T`) keeps stdin open so the migration files
can be piped in. `psql -t` (lowercase) suppresses headers on the
"does this database exist?" check, so the grep can match cleanly.

Container name, database name, and Postgres user are overridable via
environment variables — the defaults work for the local setup, the
overrides matter for CI.
EOF

# -----------------------------------------------------------------------------
# Tier 13 — Tests (8 commits)
# -----------------------------------------------------------------------------

git add tests/conftest.py
git commit -F- <<'EOF'
test: add test fixtures and database isolation

Shared fixtures for the whole test suite. This is the file every test
depends on, so it comes first in the test tier.

Two design decisions:

  1. **Tests use a real Postgres, not mocks.** Mocking the DB would
     mean writing SQL-shaped mocks that drift from reality, and the
     whole point of choosing raw SQL was to test the SQL. A real
     database catches constraint violations, NULL handling, and
     transaction semantics that mocks can't.

  2. **Isolation via transaction rollback, not schema recreation.**
     Each test opens a connection, begins a transaction, runs against
     it, and rolls back at teardown. The database is untouched between
     tests, but every test sees a fresh state because its writes are
     discarded.

The tricky part: FastAPI's app pulls connections from a pool, but the
test's connection is separate. The fix is to override the app's
`get_db` dependency to yield the *test's* connection. Both the test
and the app then share one transaction, one connection, one view of
the data. `conftest.py`'s `app` fixture wires that override and clears
it on teardown.

Environment setup (`DATABASE_URL`) sits *above* every `from app...`
import. That's the one place where Python's import order matters,
because `Settings` is cached at first import.

Provides: `db_conn`, `app`, `client`, `create_user`, `create_program`,
and `authenticated_client`. The `create_*` fixtures are *factories* —
callables that produce as many users or programs as a test needs,
rather than eagerly creating one of each.
EOF

git add tests/api/test_health.py
git commit -F- <<'EOF'
test(api): add health endpoint tests

Three tests that prove the app boots and can reach the database. This
is the smallest complete slice: config loads, the pool opens, a
dependency yields a connection, a raw query runs.

  * `test_root` — the app responds and settings loaded.
  * `test_health` — a route with no DB dependency works.
  * `test_health_db` — a route with a DB dependency runs `SELECT 1`
    through the pool and returns `{"status": "ok", "db": "ok"}`.

The third is the one that matters: it exercises the whole chain from
request to response, including the pool, the dependency, psycopg, and
Postgres. If it passes, the foundation is working.
EOF

git add tests/api/test_auth.py
git commit -F- <<'EOF'
test(api): add authentication endpoint tests

Eighteen tests covering register, login, logout, and logout-all, plus
the failure modes that matter.

The interesting ones:

  * `test_register_success` asserts the response body does not contain
    `hashed_password` in any form. Security assertion, not just a
    shape check.
  * `test_login_wrong_password` verifies wrong-password and
    unknown-email return byte-identical responses. This is the
    anti-enumeration guarantee at the HTTP layer.
  * `test_login_sets_bearer_usable_token` builds a *fresh* client with
    no cookies and sends the token as a Bearer header. If the session
    only worked via cookies, this catches it.
  * `test_logout_revokes_session` walks the full lifecycle: login,
    verify `/me` works, logout, verify `/me` fails.
  * `test_logout_all_revokes_every_session` inserts a second session
    directly, then confirms both are gone from the DB after
    logout-all.
  * `test_expired_session_rejected` and
    `test_absolute_expiry_rejected` push each expiry column into the
    past and verify the rejection. The absolute-expiry test leaves the
    idle window in the future, so a bug that only checked one expiry
    would still fail it.
  * `test_register_validation_errors` is parametrized over five bad
    inputs — one per field.

All test emails use `@example.com`, never `.local` or `.test` — those
TLDs are reserved by RFC 6761 and Pydantic's `EmailStr` rejects them.
EOF

git add tests/api/test_me.py
git commit -F- <<'EOF'
test(api): add current-user endpoint tests

Fifteen tests for the `/me/*` routes. The interesting parts are the
PATCH and password-change semantics.

PATCH tests:

  * `test_patch_me_updates_single_field` — the target field changed.
  * `test_patch_me_does_not_touch_other_fields` — reads the row
    *directly from the DB* to verify every other column is unchanged.
    A buggy implementation that overwrote everything with defaults
    would still return a 200 with the right `full_name`; only the DB
    check catches it.
  * `test_patch_me_with_empty_body_is_noop` — captures `updated_at`
    before and after, and asserts equality. If the endpoint issued any
    UPDATE (even a no-op one), the timestamp would move.

Password-change tests:

  * `test_change_password_success` — verifies the new hash works and
    the old one doesn't.
  * `test_change_password_wrong_current` — after a failed change, the
    old password still logs in. Confirms the rejection happens before
    the update.
  * `test_change_password_revokes_other_sessions` — creates a second
    session directly, changes the password, and verifies exactly one
    session survives. If the implementation revoked all sessions
    including current, the count would be zero; if it revoked none,
    two.

Enrollment tests use `@example.com` throughout, and the
identity-boundary test builds two `AsyncClient` instances because one
client can't hold two cookie jars.
EOF

git add tests/api/test_programs.py
git commit -F- <<'EOF'
test(api): add program catalog and enrollment tests

Eighteen tests for the student-facing program routes. This is where
the enrollment rules live.

Notable tests:

  * `test_list_programs_returns_active_only` asserts `total == 1` when
    one of two programs is archived. This is the subtle check that the
    count query uses the same WHERE clause as the page query. If they
    diverged, the client's pagination math would be wrong.
  * `test_list_programs_pagination_bounds` is parametrized over four
    invalid query strings — below min, above max, negative, non-int.
    All must produce 422.
  * `test_enroll_archived_program_returns_409` distinguishes
    `program_inactive` from `already_enrolled`. Same status, different
    codes.
  * `test_enroll_targets_current_user` verifies the caller is the
    target, not someone else.
  * `test_leave_only_affects_current_user` creates two enrollments and
    verifies Alice leaving doesn't remove Bob's. A bug that deleted by
    `program_id` alone would wipe every enrollment in the program;
    this test catches it.

Every test uses `@example.com` for emails. The `student_ids` set
assertions use equality (`== {expected_id}`), not membership, so
"exactly this one" is verified, not just "this one is present."
EOF

git add tests/api/test_admin.py
git commit -F- <<'EOF'
test(api): add admin endpoint tests

Twenty-seven tests, one for each admin route's happy path and one for
a student getting 403. Both directions matter: a test that "admin can
do X" doesn't prove "student cannot." If the `require_admin`
dependency were accidentally removed from a route, only the forbidden
tests would catch it.

Uses a module-level `_login_as` helper that builds a fresh client with
its own cookie jar. Two clients are needed per test (one admin, one
student) because a single `httpx.AsyncClient` can only hold one
session.

Notable tests:

  * `test_delete_program_cascades_enrollments` verifies the FK's
    `ON DELETE CASCADE` actually fires. If a migration ever changed
    it to `RESTRICT`, this would fail and force a decision.
  * `test_archive_preserves_enrollments` is the deliberate contrast —
    same setup as the cascade test, opposite outcome. Reading the two
    side by side is the lesson.
  * `test_list_program_students_program_not_found` vs
    `test_list_program_students_empty` pin a distinction: an empty
    program returns `[]`, a nonexistent one returns 404.
  * `test_create_program_duplicate_code` and
    `_duplicate_name` verify the service layer's constraint-name
    matching produces different error codes for the two unique
    constraints. If the translation broke, we'd get 500.
  * `test_reset_password_revokes_target_sessions` — the target user's
    existing sessions must all be gone after an admin password reset.
EOF

git add tests/db/test_queries.py
git commit -F- <<'EOF'
test(db): add query layer tests

Twenty-four tests that call query functions directly, with a real
connection. No HTTP, no services — just SQL against the schema.

These catch things endpoint tests can't. A few examples:

  * `test_create_user_returns_full_row` asserts `hashed_password` is
    *not* among the returned keys. If someone added it to
    `_USER_COLUMNS` for convenience, no API test would notice (schemas
    strip it), but this would fail. The query layer's read path
    deliberately excludes the hash.
  * `test_get_user_by_email_includes_hash` is the counterpart — the
    login flow needs it, and the asymmetry is deliberate.
  * `test_create_user_email_case_insensitive` proves CITEXT is
    working. Without the extension, the second insert would succeed
    and the test would fail on "no exception raised."
  * `test_create_user_age_below_minimum_raises` and
    `_above_maximum_raises` are two separate tests, not one. Postgres
    aborts the transaction after a constraint violation, so a second
    failing statement in the same test would fail with
    `InFailedSqlTransaction` before reaching the constraint. One
    failing insert per test.
  * `test_same_student_different_programs` is the composite PK's
    permissiveness — the counterpart to `test_enroll_twice_raises`.
    Together they pin the exact rule.
  * `test_delete_expired_sessions` uses three sessions with different
    expiry scenarios, verifying the OR condition in the cleanup query.
  * `test_list_enrolled_programs` asserts the joined shape *excludes*
    `created_by` and `updated_at` — the SELECT projection is what the
    schema expects.
EOF

git add tests/services/test_auth_service.py
git commit -F- <<'EOF'
test(services): add auth service tests

Sixteen tests calling service functions directly, asserting on domain
exceptions and return shapes rather than HTTP.

The layers here have logic that HTTP tests can't easily observe:

  * `test_register_creates_user_with_hashed_password` — reads the row
    with the hash and verifies the plaintext was never stored. The
    check catches a catastrophic bug that a status-code assertion
    would miss.
  * `test_register_always_creates_student` — verifies the role
    hardcoding. The service's signature has no `role` parameter; the
    test pins the current behavior so a future signature change has to
    be deliberate.
  * `test_login_creates_session_row` — verifies the round-trip:
    `hash(returned_token) == stored_token_hash`. If the service ever
    stored a truncation or a different hash, the current request would
    pass but the next one would fail.
  * `test_login_failures_are_indistinguishable` — compares both `code`
    and `message` from the two failure modes. Stronger than checking
    just the code.
  * `test_login_uses_configured_ttls` — reads the deltas between the
    session's expiries and its creation time, and asserts they match
    `settings.SESSION_IDLE_TTL_SECONDS` and
    `settings.SESSION_ABSOLUTE_TTL_SECONDS`. If someone hardcoded a
    TTL in the service, changing `.env` would have no effect; this
    test would catch it.
  * `test_logout_all_does_not_affect_other_users` — the
    identity-boundary check. A bug that forgot the WHERE clause would
    wipe every session in the DB; this test verifies Bob's survives.
EOF

# -----------------------------------------------------------------------------
# Tier 14 — Infrastructure (8 commits)
# -----------------------------------------------------------------------------

git add pyproject.toml
git commit -F- <<'EOF'
build: add project metadata and tool configuration

Single source of truth for dependencies, package discovery, and tool
config (pytest, ruff).

Runtime dependencies carry two non-obvious extras:

  * `psycopg[binary,pool]` — not just `psycopg`. `binary` bundles
    libpq so no system package is needed; `pool` provides
    `psycopg_pool`, which the async pool imports. Missing either extra
    produces "module not found" at runtime.
  * `pydantic[email]` — pulls in `email-validator`, which `EmailStr`
    requires. Without it, importing any schema that uses `EmailStr`
    fails at import time.

`[tool.setuptools.packages.find]` fixes the "Multiple top-level
packages discovered in a flat-layout" error: `app/`, `migrations/`,
and `scripts/` all look like packages to setuptools, and it refuses to
guess. The include/exclude lists say "only `app` and its
subpackages."

`[tool.pytest.ini_options]` sets `asyncio_mode = "auto"` (no
`@pytest.mark.asyncio` needed) and restricts discovery to `tests/`.

`[tool.ruff]` targets Python 3.11+, selects a minimal rule set
(`E`, `F`, `I`, `UP`, `B`), and ignores E501 because the formatter
handles line length.

Dev dependencies are declared under the `dev` extra, installable via
`pip install -e ".[dev]"`.
EOF

git add .env.example
git commit -F- <<'EOF'
chore(config): add environment template

Template for local `.env`. Every variable the app reads, with safe
defaults and no real secrets.

Copy to `.env` (`cp .env.example .env`) and adjust. The defaults match
`docker-compose.yml`, so a fresh clone works without editing anything.

Grouped by concern: application (ENV, LOG_LEVEL), database
(DATABASE_URL, pool sizes), sessions (idle and absolute TTLs), and
Argon2 tuning (time cost, memory cost, parallelism).

The Argon2 defaults are development-mild. Production should raise
`ARGON2_TIME_COST` and `ARGON2_MEMORY_COST` — the values here make
logins fast for development, which is the wrong trade in production.
EOF

git add .gitignore
git commit -F- <<'EOF'
chore: add gitignore

Standard Python ignores plus project-specific ones.

Notable entries:

  * `.env` — real secrets must never be committed. `.env.example` is
    tracked; `.env` is not.
  * `.idea/` and `.vscode/` — IDE project state. Different contributors
    use different editors; their local settings don't belong in the
    repo.
  * `.venv/` — the virtualenv. Reproducible from `pyproject.toml`;
    nothing in it is source.
  * `.pytest_cache/`, `.ruff_cache/`, `.coverage` — tool artifacts.

The pattern is: anything a tool can regenerate from source is ignored.
EOF

git add docker-compose.yml
git commit -F- <<'EOF'
chore(docker): add local development stack

Two services: Postgres and the application.

The Postgres service is the development database. Named volume
(`pgdata`) so data survives container restarts and `docker compose
down`; only `down -v` removes it. Healthcheck via `pg_isready` so the
app service can wait for readiness rather than just for "container
started."

The app service builds from the project's `Dockerfile` and depends on
Postgres being healthy. Its `DATABASE_URL` uses `postgres` as the
hostname — Docker's internal DNS resolves service names within a
compose file. Using `localhost` there would fail, because the app
container's loopback is not the database container.

Bind mounts (`./app`, `./migrations`, `./scripts`) enable live reload
during development. Not a production pattern — remove the mounts and
the `--reload` flag for a deployment image.

You can run just the database with `docker compose up -d postgres`
and run the app on the host with uvicorn. That's the recommended
development flow: Postgres in a container, app on the host for faster
reloads and easier debugging.
EOF

git add Dockerfile
git commit -F- <<'EOF'
build(docker): add application image

Multi-stage build: a builder stage installs the package into a
virtualenv, and a runtime stage copies just the venv and the source.
The result is a small image without build tools or pip caches.

Choices worth noting:

  * `pyproject.toml` is copied first, before the source, so the pip
    install layer stays cached until dependencies actually change.
    Editing `app/` doesn't invalidate the slow step.
  * Runtime dependencies only — no `[dev]` extras. Tests and linters
    belong in a CI image, not the production one.
  * A non-root user (`appuser`, uid 1000) owns the application files
    and runs the process. Running as root in a container is a
    well-known anti-pattern.
  * `PYTHONUNBUFFERED=1` — logs appear immediately rather than sitting
    in a buffer.
  * Default `CMD` runs uvicorn with no `--reload` and no workers. The
    compose file overrides for development; production would set
    `--workers` via environment or a different compose file.
EOF

git add .dockerignore
git commit -F- <<'EOF'
build(docker): add build context exclusions

Files not sent to the Docker daemon during a build. The build only
needs `pyproject.toml`, `app/`, `migrations/`, and `scripts/`;
everything else is dead weight that slows the build and bloats the
context.

Excluded: the venv, Python caches, test files, IDE directories, git
history, the README, the Makefile, and — critically — `.env`.

`.env.example` is re-included with `!.env.example` after `.env.*` was
excluded. That's a small courtesy: the example file is safe by
definition and useful to anyone inspecting the image.
EOF

git add Makefile
git commit -F- <<'EOF'
chore: add developer Makefile

Convenience targets for common commands. Not a build system — every
target is a thin shim over a command you could type yourself.

Naming conventions applied throughout:

  * Targets are verbs (`migrate`, `seed`, `test`), not nouns.
  * Multi-word targets use hyphens (`test-api`, `run-prod`), never
    underscores.
  * `help` is the default target — running `make` alone lists
    everything.
  * Every target has a `## comment` that appears in the help output.

The most important detail: **all Python commands use
`$(PY) -m <module>`** rather than calling `pytest`, `uvicorn`, or
`ruff` directly. The `-m` form resolves the module through the
interpreter named in `PY`, which means the tools are found whether or
not the venv's `bin/` directory is on `PATH`. This fixes the "module
not recognized" class of error, which was real.

Variables (`PY`, `COMPOSE`, `DB_NAME`, etc.) are all overridable on
the command line:

    make test PY=.venv/bin/python

`up` waits for Postgres to accept connections, not just for the
container to start. `init-test-db` combines creation and migration.
`env` prints the resolved configuration and confirms which tools the
current interpreter can actually see — the diagnostic target you'll
want when something isn't recognized.

Recipes use tabs, not spaces. Make requires it; the file's header
comment says so, because a copy-paste that converts tabs to spaces
produces a `missing separator` error that's cryptic if you don't know
to look for it.
EOF

git add README.md
git commit -F- <<'EOF'
docs: add project README

The front door. Covers features, architecture, setup, running,
testing, conventions, security notes, and troubleshooting.

Sections, in order:

  * **Features** — what this project does, in plain language.
  * **Architecture** — the layered design as an ASCII diagram, plus
    the data model and its invariants.
  * **Requirements and Quick Start** — from clone to running server
    in seven steps.
  * **Configuration** — every setting, its default, and its purpose.
  * **Database** — migrations, reset, and the test database.
  * **Running the App** — development and production invocations.
  * **API Overview** — every endpoint, grouped by resource, with
    auth requirements. Plus the error shape, the two auth transports,
    and the request-ID convention.
  * **Testing** — commands and a coverage map showing which test file
    covers which layer.
  * **Project Layout** — the file tree with one-line descriptions.
  * **Scripts** — what each script does and how to run it.
  * **Conventions** — URLs, naming, timestamps, IDs, errors,
    layering, logging, tests.
  * **Security Notes** — passwords, session tokens, expiry,
    revocation, anti-enumeration, authorization, CSRF, and an honest
    list of what's not covered.
  * **Troubleshooting** — the errors we actually hit, with the
    commands that fix them.
  * **License** — MIT.

Written for two audiences: someone cloning the repo for the first
time, and someone returning to it in six months. The troubleshooting
section is deliberately a list of real problems we encountered — the
Docker group permission issue, the email-validator dependency, the
`.local` TLD rejection, the transaction-abort error in tests.
EOF

# -----------------------------------------------------------------------------
# Done
# -----------------------------------------------------------------------------

echo ""
echo "All 62 commits created."
echo ""
git log --oneline | head -70
